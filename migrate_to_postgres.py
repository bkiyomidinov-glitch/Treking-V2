#!/usr/bin/env python3
"""
Скрипт миграции данных Treking-V2 из SQLite в PostgreSQL.
Переносит все справочники, товары, остатки, пользователей и криптографическую цепочку аудита.

Использование:
  python migrate_to_postgres.py --postgres-url "postgresql://user:password@localhost:5432/treking_db"
Или задать переменную окружения DATABASE_URL и запустить:
  python migrate_to_postgres.py
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import execute_batch
except ImportError:
    print("Ошибка: библиотека psycopg2 не найдена. Установите ее: pip install psycopg2-binary")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_PATH = BASE_DIR / "supply_chain.sqlite3"
SCHEMA_SQL_PATH = BASE_DIR / "schema_postgres.sql"

TABLES_ORDER = [
    "locations",
    "staff",
    "vehicles",
    "routes",
    "custom_attributes",
    "categories",
    "units",
    "users",
    "products",
    "workshop_stock",
    "trace_events",
    "audit_logs",
]


def run_migration(sqlite_path: Path, postgres_url: str) -> None:
    if not sqlite_path.exists():
        print(f"Ошибка: файл SQLite {sqlite_path} не существует.")
        sys.exit(1)

    print(f"[*] Подключение к SQLite: {sqlite_path}")
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    print(f"[*] Подключение к PostgreSQL: {postgres_url.split('@')[-1] if '@' in postgres_url else postgres_url}")
    pg_conn = psycopg2.connect(postgres_url)
    pg_conn.autocommit = False

    try:
        cur_pg = pg_conn.cursor()

        # 1. Применяем схему PostgreSQL
        if SCHEMA_SQL_PATH.exists():
            print("[*] Создание таблиц, индексов и триггеров в PostgreSQL...")
            with open(SCHEMA_SQL_PATH, "r", encoding="utf-8") as f:
                schema_sql = f.read()
            # Временно отключаем триггеры immutability для миграции
            cur_pg.execute(schema_sql)
            pg_conn.commit()

        print("\n=== Начало переноса данных ===")

        # Временно отключаем триггеры на время миграции
        cur_pg.execute("ALTER TABLE trace_events DISABLE TRIGGER ALL;")
        cur_pg.execute("ALTER TABLE audit_logs DISABLE TRIGGER ALL;")
        pg_conn.commit()

        for table in TABLES_ORDER:
            # Читаем данные из SQLite
            cur_sq = sqlite_conn.execute(f"SELECT * FROM {table}")
            rows = cur_sq.fetchall()
            if not rows:
                print(f"  [-] Таблица {table}: 0 записей (пропущено)")
                continue

            cols = [col[0] for col in cur_sq.description]
            cols_str = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["%s"] * len(cols))
            insert_query = f'INSERT INTO "{table}" ({cols_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

            data = [tuple(r[c] for c in cols) for r in rows]
            execute_batch(cur_pg, insert_query, data, page_size=1000)

            # Обновляем автоинкремент (sequence)
            if "id" in cols:
                cur_pg.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1)) FROM {table};"
                )

            print(f"  [+] Таблица {table}: успешно перенесено {len(rows)} записей")

        # Включаем триггеры обратно
        cur_pg.execute("ALTER TABLE trace_events ENABLE TRIGGER ALL;")
        cur_pg.execute("ALTER TABLE audit_logs ENABLE TRIGGER ALL;")

        pg_conn.commit()
        print("\n=== Верификация целостности данных ===")
        for table in TABLES_ORDER:
            c_sq = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            cur_pg.execute(f"SELECT COUNT(*) FROM {table}")
            c_pg = cur_pg.fetchone()[0]
            status = "OK" if c_sq <= c_pg else "РАСХОЖДЕНИЕ"
            print(f"  {table:20}: SQLite = {c_sq:5} | Postgres = {c_pg:5} [{status}]")

        print("\nМиграция в PostgreSQL успешно завершена!")

    except Exception as e:
        pg_conn.rollback()
        print(f"\nОшибка в процессе миграции: {e}")
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Миграция данных Treking-V2 из SQLite в PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=DEFAULT_SQLITE_PATH,
        help="Путь к supply_chain.sqlite3",
    )
    parser.add_argument(
        "--postgres-url",
        type=str,
        default=os.environ.get("DATABASE_URL", ""),
        help="URL подключения PostgreSQL (или переменная DATABASE_URL)",
    )
    args = parser.parse_args()

    if not args.postgres_url:
        print("Внимание: URL базы данных PostgreSQL не указан.")
        print("Укажите --postgres-url или установите переменную окружения DATABASE_URL.")
        print("Пример: python migrate_to_postgres.py --postgres-url 'postgresql://postgres:secret@localhost:5432/treking'")
        sys.exit(1)

    run_migration(args.sqlite_path, args.postgres_url)


if __name__ == "__main__":
    main()
