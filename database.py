from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "supply_chain.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

IS_POSTGRES = HAS_POSTGRES and (
    DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
)

_pg_pool: pool.ThreadedConnectionPool | None = None


def get_pg_pool() -> pool.ThreadedConnectionPool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=20,
            dsn=DATABASE_URL,
        )
    return _pg_pool


class DictRow(dict):
    """Dictionary-like row wrapper providing both dict['col'] and dict.col access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'DictRow' object has no attribute '{name}'")

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, int):
            return list(self.values())[item]
        return super().__getitem__(item)


class CursorWrapper:
    """Cursor wrapper standardizing SQLite and PostgreSQL cursors."""

    def __init__(self, cursor: Any, is_pg: bool = False):
        self._cursor = cursor
        self._is_pg = is_pg
        self.lastrowid: int | None = getattr(cursor, "lastrowid", None)

    def fetchone(self) -> DictRow | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._is_pg:
            return DictRow(row)
        return DictRow(dict(row))

    def fetchall(self) -> list[DictRow]:
        rows = self._cursor.fetchall()
        if self._is_pg:
            return [DictRow(r) for r in rows]
        return [DictRow(dict(r)) for r in rows]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def __iter__(self):
        return iter(self.fetchall())


class DBConnection:
    """Unified DB connection wrapper supporting SQLite and PostgreSQL."""

    def __init__(self, raw_conn: Any, is_pg: bool = False):
        self.conn = raw_conn
        self.is_pg = is_pg

    def _prepare_sql(self, sql: str) -> str:
        if not self.is_pg:
            return sql
        # Translate '?' placeholders to '%s' for PostgreSQL
        # Avoid changing question marks that might be escaped or in string literals
        pattern = r"\?"
        translated = re.sub(pattern, "%s", sql)
        # Adapt INSERT OR IGNORE to ON CONFLICT DO NOTHING for PostgreSQL
        if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", translated, re.IGNORECASE):
            translated = re.sub(
                r"INSERT\s+OR\s+IGNORE\s+INTO",
                "INSERT INTO",
                translated,
                flags=re.IGNORECASE,
            )
            if "ON CONFLICT" not in translated.upper():
                translated += " ON CONFLICT DO NOTHING"
        return translated

    def execute(self, sql: str, params: Any = None) -> CursorWrapper:
        prepared_sql = self._prepare_sql(sql)
        params_tuple = tuple(params) if params is not None else ()

        if self.is_pg:
            cur = self.conn.cursor(cursor_factory=RealDictCursor)
            trimmed = prepared_sql.strip().rstrip(";")
            is_insert = trimmed.upper().startswith("INSERT INTO")
            has_returning = "RETURNING" in trimmed.upper()

            if is_insert and not has_returning:
                trimmed_returning = f"{trimmed} RETURNING id"
                try:
                    cur.execute(trimmed_returning, params_tuple)
                    ret = cur.fetchone()
                    wrapper = CursorWrapper(cur, is_pg=True)
                    if ret and "id" in ret:
                        wrapper.lastrowid = ret["id"]
                    return wrapper
                except Exception:
                    self.conn.rollback()
                    # Fallback to plain query
                    cur = self.conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute(prepared_sql, params_tuple)
                    return CursorWrapper(cur, is_pg=True)
            else:
                cur.execute(prepared_sql, params_tuple)
                wrapper = CursorWrapper(cur, is_pg=True)
                return wrapper
        else:
            cur = self.conn.cursor()
            if params is not None:
                cur.execute(sql, params_tuple)
            else:
                cur.execute(sql)
            return CursorWrapper(cur, is_pg=False)

    def executemany(self, sql: str, seq_of_params: Any) -> CursorWrapper:
        prepared_sql = self._prepare_sql(sql)
        if self.is_pg:
            cur = self.conn.cursor(cursor_factory=RealDictCursor)
            cur.executemany(prepared_sql, seq_of_params)
            return CursorWrapper(cur, is_pg=True)
        else:
            cur = self.conn.cursor()
            cur.executemany(sql, seq_of_params)
            return CursorWrapper(cur, is_pg=False)

    def executescript(self, script: str) -> None:
        if self.is_pg:
            cur = self.conn.cursor()
            cur.execute(script)
        else:
            self.conn.executescript(script)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()


@contextmanager
def get_db() -> Generator[DBConnection, None, None]:
    if IS_POSTGRES:
        pool_inst = get_pg_pool()
        raw_conn = pool_inst.getconn()
        wrapped = DBConnection(raw_conn, is_pg=True)
        try:
            yield wrapped
            raw_conn.commit()
        except Exception:
            raw_conn.rollback()
            raise
        finally:
            pool_inst.putconn(raw_conn)
    else:
        raw_conn = sqlite3.connect(DB_PATH)
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute("PRAGMA foreign_keys = ON")
        wrapped = DBConnection(raw_conn, is_pg=False)
        try:
            yield wrapped
            raw_conn.commit()
        except Exception:
            raw_conn.rollback()
            raise
        finally:
            raw_conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    """Initialize database tables, indexes, triggers, and seed users."""
    with get_db() as db:
        if db.is_pg:
            # PostgreSQL Schema
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(150) NOT NULL,
                    kind VARCHAR(50) NOT NULL,
                    city VARCHAR(100) NOT NULL,
                    address VARCHAR(255) NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS staff (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(150) NOT NULL,
                    role VARCHAR(100) NOT NULL,
                    employee_no VARCHAR(50) NOT NULL,
                    phone VARCHAR(50) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vehicles (
                    id SERIAL PRIMARY KEY,
                    plate VARCHAR(50) NOT NULL,
                    brand VARCHAR(100) NOT NULL,
                    driver VARCHAR(150) NOT NULL,
                    driver_phone VARCHAR(50) NOT NULL,
                    capacity NUMERIC(10,2) NOT NULL,
                    refrigerated INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS routes (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(150) NOT NULL,
                    origin VARCHAR(100) NOT NULL,
                    destination VARCHAR(100) NOT NULL,
                    distance_km NUMERIC(10,2) NOT NULL,
                    duration_hours NUMERIC(10,2) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS custom_attributes (
                    id SERIAL PRIMARY KEY,
                    key VARCHAR(50) UNIQUE NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    type VARCHAR(30) NOT NULL,
                    options TEXT NOT NULL DEFAULT '[]',
                    unit VARCHAR(20) NOT NULL DEFAULT '',
                    is_system INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    barcode VARCHAR(50) UNIQUE NOT NULL,
                    sku VARCHAR(50) UNIQUE NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    category VARCHAR(100) NOT NULL DEFAULT '',
                    unit VARCHAR(20) NOT NULL DEFAULT 'шт',
                    price NUMERIC(12,2) NOT NULL DEFAULT 0.0,
                    cost_price NUMERIC(12,2) NOT NULL DEFAULT 0.0,
                    weight NUMERIC(10,3) NOT NULL DEFAULT 0.0,
                    qc_status VARCHAR(50) NOT NULL DEFAULT 'Пройден',
                    current_stage VARCHAR(50) NOT NULL DEFAULT 'production',
                    batch_id VARCHAR(50) NOT NULL DEFAULT '',
                    production_date VARCHAR(50) NOT NULL DEFAULT '',
                    expiry_date VARCHAR(50) NOT NULL DEFAULT '',
                    is_certified INTEGER NOT NULL DEFAULT 1,
                    is_fragile INTEGER NOT NULL DEFAULT 0,
                    is_temperature_controlled INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    description TEXT NOT NULL DEFAULT '',
                    attributes TEXT NOT NULL DEFAULT '{}',
                    created_at VARCHAR(50) NOT NULL,
                    updated_at VARCHAR(50) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workshop_stock (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    updated_at VARCHAR(50) NOT NULL,
                    UNIQUE(product_id, location_id)
                );

                CREATE TABLE IF NOT EXISTS trace_events (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    stage VARCHAR(50) NOT NULL,
                    action VARCHAR(100) NOT NULL,
                    from_location_id INTEGER REFERENCES locations(id),
                    to_location_id INTEGER REFERENCES locations(id),
                    responsible_id INTEGER REFERENCES staff(id),
                    operator_id INTEGER REFERENCES staff(id),
                    quantity INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    timestamp VARCHAR(50) NOT NULL,
                    previous_hash VARCHAR(64) NOT NULL,
                    event_hash VARCHAR(64) NOT NULL,
                    UNIQUE(product_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL
                );

                CREATE TABLE IF NOT EXISTS units (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(50) UNIQUE NOT NULL,
                    description VARCHAR(100) NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    full_name VARCHAR(100) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    workshop_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at VARCHAR(50) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action_type VARCHAR(50) NOT NULL,
                    target_entity VARCHAR(50) NOT NULL,
                    payload TEXT NOT NULL,
                    prev_hash VARCHAR(64) NOT NULL,
                    current_hash VARCHAR(64) NOT NULL,
                    created_at VARCHAR(50) NOT NULL
                );
                """
            )
        else:
            # SQLite Schema
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    city TEXT NOT NULL,
                    address TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS staff (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    employee_no TEXT NOT NULL,
                    phone TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vehicles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plate TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    driver TEXT NOT NULL,
                    driver_phone TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    refrigerated INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    distance_km REAL NOT NULL,
                    duration_hours REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS custom_attributes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    options TEXT NOT NULL DEFAULT '[]',
                    unit TEXT NOT NULL DEFAULT '',
                    is_system INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT UNIQUE NOT NULL,
                    sku TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    unit TEXT NOT NULL DEFAULT 'шт',
                    price REAL NOT NULL DEFAULT 0.0,
                    cost_price REAL NOT NULL DEFAULT 0.0,
                    weight REAL NOT NULL DEFAULT 0.0,
                    qc_status TEXT NOT NULL DEFAULT 'Пройден',
                    current_stage TEXT NOT NULL DEFAULT 'production',
                    batch_id TEXT NOT NULL DEFAULT '',
                    production_date TEXT NOT NULL DEFAULT '',
                    expiry_date TEXT NOT NULL DEFAULT '',
                    is_certified INTEGER NOT NULL DEFAULT 1,
                    is_fragile INTEGER NOT NULL DEFAULT 0,
                    is_temperature_controlled INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    description TEXT NOT NULL DEFAULT '',
                    attributes TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workshop_stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(product_id, location_id)
                );

                CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_location_id INTEGER REFERENCES locations(id),
                    to_location_id INTEGER REFERENCES locations(id),
                    responsible_id INTEGER REFERENCES staff(id),
                    operator_id INTEGER REFERENCES staff(id),
                    quantity INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    timestamp TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    UNIQUE(product_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                );

                CREATE TABLE IF NOT EXISTS units (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    workshop_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action_type TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    current_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS trace_events_no_update
                BEFORE UPDATE ON trace_events BEGIN
                    SELECT RAISE(ABORT, 'События трекинга неизменяемы (WORM audit trail)');
                END;

                CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
                BEFORE DELETE ON trace_events BEGIN
                    SELECT RAISE(ABORT, 'Удаление записей аудита трекинга запрещено');
                END;

                CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
                BEFORE UPDATE ON audit_logs BEGIN
                    SELECT RAISE(ABORT, 'Записи аудита неизменяемы (WORM audit trail)');
                END;

                CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
                BEFORE DELETE ON audit_logs BEGIN
                    SELECT RAISE(ABORT, 'Удаление записей аудита запрещено (WORM audit trail)');
                END;
                """
            )

        # High-performance indexes (supporting 500k ops/month)
        db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_user_date ON audit_logs(created_at, user_id);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_target ON audit_logs(target_entity, action_type);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_prod_seq ON trace_events(product_id, sequence);")

        # Seed initial units if empty
        row = db.execute("SELECT COUNT(*) AS cnt FROM units").fetchone()
        if row and row["cnt"] == 0:
            default_units = [
                ("шт", "Штука"),
                ("компл", "Комплект"),
                ("пар", "Пара"),
                ("упак", "Упаковка"),
                ("м", "Метр"),
                ("пог. м", "Погонный метр"),
                ("кг", "Килограмм"),
                ("рулон", "Рулон"),
                ("бобина", "Бобина"),
                ("л", "Литр"),
                ("т", "Тонна"),
            ]
            for name, desc in default_units:
                db.execute("INSERT INTO units(name, description) VALUES(?,?)", (name, desc))

        # Seed initial switch flags in custom_attributes if empty
        default_switches = [
            ("is_certified", "Сертификат качества", "boolean"),
            ("is_active", "Активен для продажи", "boolean"),
            ("is_fragile", "Хрупкий груз", "boolean"),
            ("is_temperature_controlled", "Температурный режим", "boolean"),
        ]
        for key, name, attr_type in default_switches:
            if not db.execute("SELECT 1 FROM custom_attributes WHERE key=?", (key,)).fetchone():
                db.execute(
                    "INSERT INTO custom_attributes(key, name, type, options, unit, is_system) VALUES(?,?,?,'[]','',0)",
                    (key, name, attr_type),
                )
