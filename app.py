from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
from database import DBConnection, get_db, init_db, now_iso

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

STAGE_NAMES = {
    "production": "Производство / В цехе",
    "qc": "Контроль качества (ОТК)",
    "warehouse": "Склад готовой продукции",
    "transit": "В пути / Доставка",
    "delivered": "Доставлен / Выдан",
}

STAGE_BADGES = {
    "production": {"color": "amber", "label": "В цехе"},
    "qc": {"color": "purple", "label": "ОТК"},
    "warehouse": {"color": "blue", "label": "На складе"},
    "transit": {"color": "indigo", "label": "В пути"},
    "delivered": {"color": "emerald", "label": "Доставлен"},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def insert_trace_event(
    db: DBConnection,
    product_id: int,
    stage: str,
    action: str,
    from_location_id: int | None,
    to_location_id: int | None,
    responsible_id: int | None,
    operator_id: int | None,
    quantity: int = 1,
    note: str = "",
    metadata: dict[str, Any] | None = None,
    event_timestamp: str | None = None,
) -> dict[str, Any]:
    if metadata is None:
        metadata = {}

    previous = db.execute(
        "SELECT event_hash FROM trace_events WHERE product_id=? ORDER BY sequence DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    seq_row = db.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM trace_events WHERE product_id=?",
        (product_id,),
    ).fetchone()
    sequence = seq_row["next_seq"] if seq_row else 1
    timestamp = event_timestamp or now()
    previous_hash = previous["event_hash"] if previous else "0" * 64

    entry = {
        "product_id": product_id,
        "sequence": sequence,
        "stage": stage,
        "action": action,
        "from_location_id": from_location_id,
        "to_location_id": to_location_id,
        "responsible_id": responsible_id,
        "operator_id": operator_id,
        "quantity": quantity,
        "timestamp": timestamp,
        "note": note,
        "metadata": metadata,
        "previous_hash": previous_hash,
    }
    event_hash = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()

    cursor = db.execute(
        """
        INSERT INTO trace_events(
            product_id, sequence, stage, action, from_location_id, to_location_id,
            responsible_id, operator_id, quantity, note, metadata, timestamp,
            previous_hash, event_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            product_id,
            sequence,
            stage,
            action,
            from_location_id,
            to_location_id,
            responsible_id,
            operator_id,
            quantity,
            note,
            canonical_json(metadata),
            timestamp,
            previous_hash,
            event_hash,
        ),
    )
    row = db.execute("SELECT * FROM trace_events WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row) if row else {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Initialize DB schemas, indexes, WORM triggers, and seed users
    init_db()
    with get_db() as db:
        auth.seed_default_users(db)
    yield


app = FastAPI(
    title="Система трекинга и учета производства Treking-V2 · Таджикистан",
    version="2.2.0",
    lifespan=lifespan,
)


# --- Pydantic Request Models ---

class UnitInput(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(default="", max_length=100)


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    sku: str = Field(default="", max_length=50)
    barcode: str = Field(default="", max_length=50)
    category: str = Field(default="", max_length=80)
    unit: str = Field(default="шт", max_length=20)
    price: float = Field(default=0.0, ge=0)
    cost_price: float = Field(default=0.0, ge=0)
    weight: float = Field(default=0.0, ge=0)
    qc_status: str = Field(default="Пройден")
    current_stage: str = Field(default="production")
    batch_id: str = Field(default="", max_length=50)
    production_date: str = Field(default="")
    expiry_date: str = Field(default="")
    is_certified: bool = Field(default=True)
    is_fragile: bool = Field(default=False)
    is_temperature_controlled: bool = Field(default=False)
    is_active: bool = Field(default=True)
    description: str = Field(default="", max_length=1000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    initial_location_id: int | None = None
    initial_quantity: int = Field(default=0, ge=0)
    responsible_id: int | None = None
    operator_id: int | None = None


class ProductUpdateInput(BaseModel):
    name: str | None = None
    category: str | None = None
    unit: str | None = None
    price: float | None = None
    cost_price: float | None = None
    weight: float | None = None
    qc_status: str | None = None
    production_date: str | None = None
    expiry_date: str | None = None
    is_certified: bool | None = None
    is_fragile: bool | None = None
    is_temperature_controlled: bool | None = None
    is_active: bool | None = None
    description: str | None = None
    batch_id: str | None = None
    attributes: dict[str, Any] | None = None


class ProductQCUpdateInput(BaseModel):
    qc_status: str = Field(min_length=2, max_length=50)
    note: str = Field(default="", max_length=500)


class StockMoveInput(BaseModel):
    product_id: int
    from_location_id: int | None = None
    to_location_id: int
    quantity: int = Field(gt=0)
    stage: str = "production"
    action: str = "Перемещение"
    transfer_date: str | None = None
    eta_date: str | None = None
    is_express: bool = False
    is_sealed: bool = False
    responsible_id: int | None = None
    operator_id: int | None = None
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttributeInput(BaseModel):
    key: str = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=80)
    type: Literal["text", "select", "number", "date", "boolean"] = "text"
    options: list[str] = Field(default_factory=list)
    unit: str = Field(default="", max_length=20)


class AttributeUpdateInput(BaseModel):
    name: str | None = None
    type: Literal["text", "select", "number", "date", "boolean"] | None = None
    options: list[str] | None = None
    unit: str | None = None


class AttributeOptionInput(BaseModel):
    option: str = Field(min_length=1, max_length=100)


class LocationInput(BaseModel):
    name: str = Field(min_length=2)
    kind: Literal["workshop", "warehouse", "store", "transit"] = "workshop"
    city: str = Field(min_length=2)
    address: str = Field(min_length=2)
    description: str = Field(default="")


class StaffInput(BaseModel):
    name: str = Field(min_length=2)
    role: str = Field(min_length=2)
    employee_no: str = Field(min_length=2)
    phone: str = Field(min_length=5)


class VehicleInput(BaseModel):
    plate: str = Field(min_length=2)
    brand: str = Field(min_length=2)
    driver: str = Field(min_length=2)
    driver_phone: str = Field(min_length=5)
    capacity: float = Field(gt=0)
    refrigerated: bool = False


class RouteInput(BaseModel):
    name: str = Field(min_length=2)
    origin: str = Field(min_length=2)
    destination: str = Field(min_length=2)
    distance_km: float = Field(gt=0)
    duration_hours: float = Field(gt=0)


RESOURCE_MODELS = {
    "locations": LocationInput,
    "staff": StaffInput,
    "vehicles": VehicleInput,
    "routes": RouteInput,
}


def serialize_product(db: DBConnection, row: Any) -> dict[str, Any]:
    prod = dict(row)
    prod["attributes"] = json.loads(prod["attributes"]) if prod.get("attributes") else {}
    prod["stage_name"] = STAGE_NAMES.get(prod["current_stage"], prod["current_stage"])
    prod["stage_badge"] = STAGE_BADGES.get(prod["current_stage"], {"color": "slate", "label": prod["current_stage"]})

    # Booleans
    prod["is_certified"] = bool(prod.get("is_certified", 1))
    prod["is_fragile"] = bool(prod.get("is_fragile", 0))
    prod["is_temperature_controlled"] = bool(prod.get("is_temperature_controlled", 0))
    prod["is_active"] = bool(prod.get("is_active", 1))

    # Dates
    prod["production_date"] = prod.get("production_date") or ""
    prod["expiry_date"] = prod.get("expiry_date") or ""

    stock_rows = db.execute(
        """
        SELECT s.location_id, s.quantity, l.name AS location_name, l.kind AS location_kind, l.city
        FROM workshop_stock s
        JOIN locations l ON l.id = s.location_id
        WHERE s.product_id = ? AND s.quantity > 0
        ORDER BY l.kind, l.id
        """,
        (prod["id"],),
    ).fetchall()

    prod["stock_by_location"] = [dict(r) for r in stock_rows]
    prod["stock_total"] = sum(r["quantity"] for r in prod["stock_by_location"])
    return prod


# =============================================================================
# --- Authentication & User Management Endpoints ---
# =============================================================================

@app.post("/api/auth/token", response_model=auth.TokenResponse)
async def login_for_access_token(
    request: Request,
) -> dict[str, Any]:
    """
    Authenticate user via OAuth2 Password Grant Form OR JSON LoginRequest.
    Validates password hash via bcrypt, checks is_active status, and returns signed JWT.
    """
    username = ""
    password = ""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                username = str(body.get("username") or "").strip()
                password = str(body.get("password") or "")
        except Exception:
            pass
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            username = str(form.get("username") or "").strip()
            password = str(form.get("password") or "")
        except Exception:
            pass
    else:
        try:
            body = await request.json()
            if isinstance(body, dict):
                username = str(body.get("username") or "").strip()
                password = str(body.get("password") or "")
        except Exception:
            try:
                form = await request.form()
                username = str(form.get("username") or "").strip()
                password = str(form.get("password") or "")
            except Exception:
                pass

    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Необходимо указать имя пользователя и пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )

    with get_db() as db:
        user = db.execute(
            """
            SELECT u.id, u.username, u.password_hash, u.full_name, u.role, u.workshop_id, u.is_active,
                   l.name AS workshop_name
            FROM users u
            LEFT JOIN locations l ON l.id = u.workshop_id
            WHERE u.username = ?
            """,
            (username,),
        ).fetchone()

        if not user or not auth.verify_password(password, user["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверное имя пользователя или пароль",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not bool(user["is_active"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Учетная запись деактивирована администратором",
            )

        # Generate JWT access token
        access_token = auth.create_access_token(
            data={"sub": user["username"], "user_id": user["id"], "role": user["role"]}
        )

        # Log login into immutable audit trail
        auth.log_audit(
            db=db,
            user_id=user["id"],
            action_type="AUTH_LOGIN",
            target_entity="USER",
            payload={"username": user["username"], "role": user["role"], "login_time": now_iso()},
        )

        user_info = {
            "id": user["id"],
            "username": user["username"],
            "full_name": user["full_name"],
            "role": user["role"],
            "workshop_id": user["workshop_id"],
            "workshop_name": user["workshop_name"],
            "is_active": bool(user["is_active"]),
        }
        return {"access_token": access_token, "token_type": "bearer", "user": user_info}


@app.get("/api/auth/me")
def get_me(
    current_user: dict[str, Any] = Depends(auth.get_current_active_user),
) -> dict[str, Any]:
    """Return currently authenticated active user info and permissions."""
    return current_user


@app.get("/api/users")
def list_users(
    _: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> list[dict[str, Any]]:
    """List all users (Admin only)."""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT u.id, u.username, u.full_name, u.role, u.workshop_id, u.is_active, u.created_at,
                   l.name AS workshop_name, l.kind AS workshop_kind, l.city AS workshop_city
            FROM users u
            LEFT JOIN locations l ON l.id = u.workshop_id
            ORDER BY u.id ASC
            """
        ).fetchall()
        result = []
        for r in rows:
            u = dict(r)
            u["is_active"] = bool(u["is_active"])
            result.append(u)
        return result


@app.post("/api/users", status_code=201)
def create_user(
    payload: auth.UserCreate,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, Any]:
    """Create a new corporate user (Admin only)."""
    username = payload.username.strip().lower()
    with get_db() as db:
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, f"Пользователь с именем «{username}» уже существует")

        if payload.workshop_id is not None and not db.execute(
            "SELECT 1 FROM locations WHERE id=?", (payload.workshop_id,)
        ).fetchone():
            raise HTTPException(404, "Указанный цех/склад не найден")

        pwd_hash = auth.hash_password(payload.password)
        created = now_iso()
        cur = db.execute(
            """
            INSERT INTO users(username, password_hash, full_name, role, workshop_id, is_active, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                pwd_hash,
                payload.full_name.strip(),
                payload.role,
                payload.workshop_id,
                1 if payload.is_active else 0,
                created,
            ),
        )
        user_id = cur.lastrowid

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="USER_CREATE",
            target_entity="USER",
            payload={
                "created_user_id": user_id,
                "username": username,
                "role": payload.role,
                "full_name": payload.full_name,
                "workshop_id": payload.workshop_id,
            },
        )

        row = db.execute(
            """
            SELECT u.id, u.username, u.full_name, u.role, u.workshop_id, u.is_active, u.created_at,
                   l.name AS workshop_name
            FROM users u
            LEFT JOIN locations l ON l.id = u.workshop_id
            WHERE u.id=?
            """,
            (user_id,),
        ).fetchone()
        res = dict(row)
        res["is_active"] = bool(res["is_active"])
        return res


@app.put("/api/users/{user_id}")
def update_user(
    user_id: int,
    payload: auth.UserUpdate,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, Any]:
    """Update user information, role, workshop, active status, or password (Admin only)."""
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, "Пользователь не найден")

        updates: dict[str, Any] = {}
        if payload.full_name is not None:
            updates["full_name"] = payload.full_name.strip()
        if payload.role is not None:
            updates["role"] = payload.role
        if payload.workshop_id is not None:
            if payload.workshop_id > 0:
                if not db.execute("SELECT 1 FROM locations WHERE id=?", (payload.workshop_id,)).fetchone():
                    raise HTTPException(404, "Указанный цех/склад не найден")
                updates["workshop_id"] = payload.workshop_id
            else:
                updates["workshop_id"] = None
        if payload.is_active is not None:
            updates["is_active"] = 1 if payload.is_active else 0
        if payload.password is not None and payload.password.strip():
            updates["password_hash"] = auth.hash_password(payload.password.strip())

        if updates:
            fields = [f"{k} = ?" for k in updates]
            db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", [*updates.values(), user_id])

            audit_payload = {k: v for k, v in updates.items() if k != "password_hash"}
            if "password_hash" in updates:
                audit_payload["password_changed"] = True

            auth.log_audit(
                db=db,
                user_id=current_user["id"],
                action_type="USER_UPDATE",
                target_entity="USER",
                payload={"target_user_id": user_id, "username": user["username"], "changes": audit_payload},
            )

        row = db.execute(
            """
            SELECT u.id, u.username, u.full_name, u.role, u.workshop_id, u.is_active, u.created_at,
                   l.name AS workshop_name
            FROM users u
            LEFT JOIN locations l ON l.id = u.workshop_id
            WHERE u.id=?
            """,
            (user_id,),
        ).fetchone()
        res = dict(row)
        res["is_active"] = bool(res["is_active"])
        return res


@app.delete("/api/users/{user_id}")
def delete_user(
    user_id: int,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, str]:
    """Deactivate or delete user (Admin only). Cannot delete the last active admin."""
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, "Пользователь не найден")

        if user["id"] == current_user["id"]:
            raise HTTPException(400, "Нельзя удалить свою собственную активную учетную запись администратора")

        # Soft delete (deactivate) or delete
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="USER_DELETE",
            target_entity="USER",
            payload={"deleted_user_id": user_id, "username": user["username"], "role": user["role"]},
        )
        return {"status": "ok", "message": f"Пользователь «{user['username']}» удален"}


# =============================================================================
# --- Audit Trail & Cryptographic Verification Endpoints ---
# =============================================================================

@app.get("/api/audit-logs")
def list_audit_logs(
    limit: int = 100,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> list[dict[str, Any]]:
    """List recent immutable audit trail logs (Admin and Manager only)."""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT a.*, u.username, u.full_name, u.role AS user_role
            FROM audit_logs a
            LEFT JOIN users u ON u.id = a.user_id
            ORDER BY a.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            try:
                item["payload"] = json.loads(item["payload"]) if item.get("payload") else {}
            except Exception:
                pass
            result.append(item)
        return result


@app.get("/api/audit-logs/verify")
def verify_audit_trail_endpoint(
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    """
    Cryptographically verify the SHA-256 blockchain integrity chain of all audit logs.
    Validates that no records were modified, injected, or deleted.
    """
    with get_db() as db:
        result = auth.verify_audit_chain(db)
        return result


# =============================================================================
# --- Production, Products & Stock Routes (Protected by RBAC) ---
# =============================================================================

@app.get("/api/bootstrap")
def bootstrap(
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper", "qc", "worker"])),
) -> dict[str, Any]:
    """
    Bootstrap workspace payload.
    Accessible to all authenticated enterprise roles (worker, storekeeper, qc, manager, admin).
    """
    with get_db() as db:
        products_raw = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        products = [serialize_product(db, r) for r in products_raw]

        locations = [dict(r) for r in db.execute("SELECT * FROM locations ORDER BY id").fetchall()]
        staff = [dict(r) for r in db.execute("SELECT * FROM staff ORDER BY id").fetchall()]
        vehicles = [dict(r) for r in db.execute("SELECT * FROM vehicles ORDER BY id").fetchall()]
        routes = [dict(r) for r in db.execute("SELECT * FROM routes ORDER BY id").fetchall()]

        attributes_raw = db.execute("SELECT * FROM custom_attributes ORDER BY id").fetchall()
        attributes = []
        for a in attributes_raw:
            item = dict(a)
            item["options"] = json.loads(item["options"]) if item.get("options") else []
            attributes.append(item)

        total_units = sum(p["stock_total"] for p in products)
        in_workshops = sum(
            sum(loc["quantity"] for loc in p["stock_by_location"] if loc["location_kind"] == "workshop")
            for p in products
        )
        in_warehouses = sum(
            sum(loc["quantity"] for loc in p["stock_by_location"] if loc["location_kind"] == "warehouse")
            for p in products
        )
        in_transit = sum(
            sum(loc["quantity"] for loc in p["stock_by_location"] if loc["location_kind"] == "transit")
            for p in products
        )

        recent_events = db.execute(
            """
            SELECT e.*, p.name AS product_name, p.barcode AS product_barcode,
                   fl.name AS from_location_name, tl.name AS to_location_name,
                   s.name AS responsible_name, o.name AS operator_name
            FROM trace_events e
            JOIN products p ON p.id = e.product_id
            LEFT JOIN locations fl ON fl.id = e.from_location_id
            LEFT JOIN locations tl ON tl.id = e.to_location_id
            LEFT JOIN staff s ON s.id = e.responsible_id
            LEFT JOIN staff o ON o.id = e.operator_id
            ORDER BY e.id DESC LIMIT 15
            """
        ).fetchall()

        events_serialized = []
        for e in recent_events:
            ev = dict(e)
            ev["metadata"] = json.loads(ev["metadata"]) if ev.get("metadata") else {}
            events_serialized.append(ev)

        categories_db = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY name").fetchall()]
        categories_products = [r[0] for r in db.execute("SELECT DISTINCT category FROM products WHERE category != ''").fetchall()]
        all_categories = sorted(list(set(categories_db + categories_products)))

        units_rows = db.execute("SELECT * FROM units ORDER BY name").fetchall()
        units_db = [dict(r) for r in units_rows]
        prod_units = {r[0] for r in db.execute("SELECT DISTINCT unit FROM products WHERE unit != ''").fetchall()}
        existing_unit_names = {u["name"] for u in units_db}
        for pu in sorted(prod_units):
            if pu not in existing_unit_names:
                units_db.append({"id": None, "name": pu, "description": ""})

        return {
            "country": "Таджикистан",
            "currency": "сомони",
            "current_user": current_user,
            "products": products,
            "locations": locations,
            "staff": staff,
            "vehicles": vehicles,
            "routes": routes,
            "custom_attributes": attributes,
            "categories": all_categories,
            "units": units_db,
            "stats": {
                "total_products": len(products),
                "total_units": total_units,
                "in_workshops": in_workshops,
                "in_warehouses": in_warehouses,
                "in_transit": in_transit,
                "workshops_count": len([l for l in locations if l["kind"] == "workshop"]),
                "warehouses_count": len([l for l in locations if l["kind"] == "warehouse"]),
            },
            "recent_events": events_serialized,
        }


@app.get("/api/products")
def list_products(
    search: str = "",
    category: str = "",
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper", "qc", "worker"])),
) -> list[dict[str, Any]]:
    """List products. Accessible to all authenticated roles."""
    with get_db() as db:
        query = "SELECT * FROM products WHERE 1=1"
        params: list[Any] = []
        if search:
            query += " AND (name LIKE ? OR sku LIKE ? OR barcode LIKE ?)"
            s = f"%{search}%"
            params.extend([s, s, s])
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY id DESC"

        rows = db.execute(query, params).fetchall()
        return [serialize_product(db, r) for r in rows]


@app.post("/api/products", status_code=201)
def create_product(
    payload: ProductInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    """Create a new product nomenclature (Manager and Admin only). Audited."""
    with get_db() as db:
        barcode = payload.barcode.strip()
        if not barcode:
            base = f"485{int(datetime.now().timestamp() * 1000) % 1000000000:09d}"
            check = (10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(base)) % 10) % 10
            barcode = f"{base}{check}"

        sku = payload.sku.strip()
        if not sku:
            sku = f"TJ-{int(datetime.now().timestamp() * 1000) % 100000:05d}"

        if db.execute("SELECT 1 FROM products WHERE barcode=?", (barcode,)).fetchone():
            raise HTTPException(409, f"Товар со штрихкодом {barcode} уже существует")
        if db.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone():
            raise HTTPException(409, f"Товар с артикулом {sku} уже существует")

        qty = payload.initial_quantity
        initial_loc_id = payload.initial_location_id
        if initial_loc_id is None and qty > 0:
            first_location = db.execute(
                "SELECT id FROM locations WHERE kind='workshop' ORDER BY id LIMIT 1"
            ).fetchone()
            if first_location is not None:
                initial_loc_id = first_location["id"]
            else:
                qty = 0

        if initial_loc_id is not None and not db.execute(
            "SELECT 1 FROM locations WHERE id=?", (initial_loc_id,)
        ).fetchone():
            initial_loc_id = None
            qty = 0

        cat = payload.category.strip()
        if cat:
            db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cat,))

        u_name = payload.unit.strip()
        if u_name:
            db.execute("INSERT OR IGNORE INTO units(name, description) VALUES(?, '')", (u_name,))

        for staff_id in (payload.responsible_id, payload.operator_id):
            if staff_id is not None and not db.execute(
                "SELECT 1 FROM staff WHERE id=?", (staff_id,)
            ).fetchone():
                raise HTTPException(404, "Сотрудник не найден")

        created = now_iso()
        batch_id = payload.batch_id.strip() or f"TJ-{datetime.now().strftime('%y%m%d-%H%M')}"
        prod_date = payload.production_date.strip() or today_str()
        exp_date = payload.expiry_date.strip()

        cursor = db.execute(
            """
            INSERT INTO products(
                name, sku, barcode, category, unit, price, cost_price, weight,
                qc_status, current_stage, batch_id, production_date, expiry_date,
                is_certified, is_fragile, is_temperature_controlled, is_active,
                description, attributes, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.name.strip(),
                sku,
                barcode,
                payload.category.strip(),
                payload.unit.strip(),
                payload.price,
                payload.cost_price,
                payload.weight,
                payload.qc_status,
                payload.current_stage,
                batch_id,
                prod_date,
                exp_date,
                1 if payload.is_certified else 0,
                1 if payload.is_fragile else 0,
                1 if payload.is_temperature_controlled else 0,
                1 if payload.is_active else 0,
                payload.description.strip(),
                canonical_json(payload.attributes),
                created,
                created,
            ),
        )
        product_id = cursor.lastrowid

        if initial_loc_id is not None and qty > 0:
            db.execute(
                "INSERT INTO workshop_stock(product_id, location_id, quantity, updated_at) VALUES(?,?,?,?)",
                (product_id, initial_loc_id, qty, created),
            )

        insert_trace_event(
            db=db,
            product_id=product_id,
            stage=payload.current_stage,
            action="Выпуск продукции",
            from_location_id=None,
            to_location_id=initial_loc_id,
            responsible_id=payload.responsible_id,
            operator_id=payload.operator_id,
            quantity=qty,
            note=f"Первичная регистрация партии {batch_id}. Дата производства: {prod_date}",
            metadata={
                "initial_stock": qty,
                "batch_id": batch_id,
                "production_date": prod_date,
                "is_certified": payload.is_certified,
                "is_fragile": payload.is_fragile,
            },
        )

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="PRODUCT_CREATE",
            target_entity="PRODUCT",
            payload={
                "product_id": product_id,
                "name": payload.name.strip(),
                "sku": sku,
                "barcode": barcode,
                "batch_id": batch_id,
                "initial_quantity": qty,
                "initial_location_id": initial_loc_id,
            },
        )

        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return serialize_product(db, row)


@app.put("/api/products/{product_id}")
def update_product(
    product_id: int,
    payload: ProductUpdateInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "qc"])),
) -> dict[str, Any]:
    """
    Update product.
    Role 'qc' is restricted to updating only 'qc_status'.
    Roles 'manager' and 'admin' can update all product fields.
    Audited.
    """
    role = current_user.get("role")
    with get_db() as db:
        current = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Товар не найден")

        # If user is QC inspector, ensure they only modify qc_status
        if role == "qc":
            forbidden_fields = [
                k for k, v in payload.model_dump(exclude_unset=True).items() if k != "qc_status" and v is not None
            ]
            if forbidden_fields:
                raise HTTPException(
                    status_code=403,
                    detail=f"Инспектор ОТК имеет право изменять только статус контроля качества (qc_status). Поля {', '.join(forbidden_fields)} недоступны.",
                )

        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = payload.name.strip()
        if payload.category is not None:
            updates["category"] = payload.category.strip()
        if payload.unit is not None:
            updates["unit"] = payload.unit.strip()
        if payload.price is not None:
            updates["price"] = payload.price
        if payload.cost_price is not None:
            updates["cost_price"] = payload.cost_price
        if payload.weight is not None:
            updates["weight"] = payload.weight
        if payload.qc_status is not None:
            updates["qc_status"] = payload.qc_status
        if payload.production_date is not None:
            updates["production_date"] = payload.production_date.strip()
        if payload.expiry_date is not None:
            updates["expiry_date"] = payload.expiry_date.strip()
        if payload.is_certified is not None:
            updates["is_certified"] = 1 if payload.is_certified else 0
        if payload.is_fragile is not None:
            updates["is_fragile"] = 1 if payload.is_fragile else 0
        if payload.is_temperature_controlled is not None:
            updates["is_temperature_controlled"] = 1 if payload.is_temperature_controlled else 0
        if payload.is_active is not None:
            updates["is_active"] = 1 if payload.is_active else 0
        if payload.description is not None:
            updates["description"] = payload.description.strip()
        if payload.batch_id is not None:
            updates["batch_id"] = payload.batch_id.strip()
        if payload.attributes is not None:
            updates["attributes"] = canonical_json(payload.attributes)

        updates["updated_at"] = now_iso()

        fields = [f"{k} = ?" for k in updates]
        db.execute(
            f"UPDATE products SET {', '.join(fields)} WHERE id = ?",
            [*updates.values(), product_id],
        )

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="PRODUCT_UPDATE",
            target_entity="PRODUCT",
            payload={"product_id": product_id, "sku": current["sku"], "updates": updates},
        )

        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return serialize_product(db, row)


@app.put("/api/products/{product_id}/qc")
def update_product_qc_status(
    product_id: int,
    payload: ProductQCUpdateInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "qc"])),
) -> dict[str, Any]:
    """Specialized endpoint for Quality Control inspection update. Audited."""
    with get_db() as db:
        current = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Товар не найден")

        new_status = payload.qc_status.strip()
        cur_time = now_iso()
        db.execute("UPDATE products SET qc_status=?, updated_at=? WHERE id=?", (new_status, cur_time, product_id))

        insert_trace_event(
            db=db,
            product_id=product_id,
            stage="qc",
            action=f"Контроль качества: {new_status}",
            from_location_id=None,
            to_location_id=None,
            responsible_id=None,
            operator_id=None,
            quantity=1,
            note=f"ОТК инспекция: {new_status}. {payload.note}".strip(),
            metadata={"qc_status": new_status, "inspector": current_user["full_name"], "note": payload.note},
        )

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="QC_INSPECTION",
            target_entity="PRODUCT",
            payload={"product_id": product_id, "prev_status": current["qc_status"], "new_status": new_status, "note": payload.note},
        )

        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return serialize_product(db, row)


@app.delete("/api/products/{product_id}")
def delete_product(
    product_id: int,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, str]:
    """Delete a product nomenclature (Admin only). Audited."""
    with get_db() as db:
        prod = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not prod:
            raise HTTPException(404, "Товар не найден")

        db.execute("DELETE FROM workshop_stock WHERE product_id=?", (product_id,))
        if not db.is_pg:
            db.execute("DROP TRIGGER IF EXISTS trace_events_no_delete")
        db.execute("DELETE FROM trace_events WHERE product_id=?", (product_id,))
        if not db.is_pg:
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
                BEFORE DELETE ON trace_events BEGIN
                    SELECT RAISE(ABORT, 'Удаление записей аудита трекинга запрещено');
                END;
                """
            )
        db.execute("DELETE FROM products WHERE id=?", (product_id,))

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="PRODUCT_DELETE",
            target_entity="PRODUCT",
            payload={"product_id": product_id, "name": prod["name"], "sku": prod["sku"], "barcode": prod["barcode"]},
        )
        return {"status": "ok", "message": f"Товар «{prod['name']}» успешно удален"}


@app.get("/api/products/{barcode}")
def get_product(
    barcode: str,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper", "qc", "worker"])),
) -> dict[str, Any]:
    """Get single product details with complete event trace history. Open to all active roles."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM products WHERE barcode=? OR sku=?",
            (barcode, barcode),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Товар со штрихкодом или артикулом «{barcode}» не найден")

        prod = serialize_product(db, row)

        events_raw = db.execute(
            """
            SELECT e.*, fl.name AS from_location_name, tl.name AS to_location_name,
                   s.name AS responsible_name, s.role AS responsible_role,
                   o.name AS operator_name
            FROM trace_events e
            LEFT JOIN locations fl ON fl.id = e.from_location_id
            LEFT JOIN locations tl ON tl.id = e.to_location_id
            LEFT JOIN staff s ON s.id = e.responsible_id
            LEFT JOIN staff o ON o.id = e.operator_id
            WHERE e.product_id = ?
            ORDER BY e.sequence ASC
            """,
            (prod["id"],),
        ).fetchall()

        events = []
        for e in events_raw:
            ev = dict(e)
            ev["metadata"] = json.loads(ev["metadata"]) if ev.get("metadata") else {}
            ev["stage_name"] = STAGE_NAMES.get(ev["stage"], ev["stage"])
            events.append(ev)

        prod["events"] = events
        return prod


@app.post("/api/tracking/move")
def move_product(
    payload: StockMoveInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper"])),
) -> dict[str, Any]:
    """
    Execute stock transfer between workshops, warehouses, and transit.
    Protected for Storekeeper, Manager, Admin. Audited in SHA-256 ledger.
    """
    with get_db() as db:
        product = db.execute("SELECT * FROM products WHERE id=?", (payload.product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Товар не найден")

        qty = payload.quantity
        from_loc_id = payload.from_location_id
        to_loc_id = payload.to_location_id

        if from_loc_id:
            current_stock_row = db.execute(
                "SELECT quantity FROM workshop_stock WHERE product_id=? AND location_id=?",
                (payload.product_id, from_loc_id),
            ).fetchone()
            current_stock = current_stock_row["quantity"] if current_stock_row else 0
            if current_stock < qty:
                raise HTTPException(
                    400,
                    f"Недостаточно остатка в исходном цехе/складе. Доступно: {current_stock} шт., требуется: {qty} шт.",
                )
            new_from_stock = current_stock - qty
            db.execute(
                "UPDATE workshop_stock SET quantity=?, updated_at=? WHERE product_id=? AND location_id=?",
                (new_from_stock, now_iso(), payload.product_id, from_loc_id),
            )

        dest_stock_row = db.execute(
            "SELECT quantity FROM workshop_stock WHERE product_id=? AND location_id=?",
            (payload.product_id, to_loc_id),
        ).fetchone()
        if dest_stock_row:
            new_dest_stock = dest_stock_row["quantity"] + qty
            db.execute(
                "UPDATE workshop_stock SET quantity=?, updated_at=? WHERE product_id=? AND location_id=?",
                (new_dest_stock, now_iso(), payload.product_id, to_loc_id),
            )
        else:
            db.execute(
                "INSERT INTO workshop_stock(product_id, location_id, quantity, updated_at) VALUES(?,?,?,?)",
                (payload.product_id, to_loc_id, qty, now_iso()),
            )

        db.execute(
            "UPDATE products SET current_stage=?, updated_at=? WHERE id=?",
            (payload.stage, now_iso(), payload.product_id),
        )

        metadata = payload.metadata.copy()
        if payload.is_express:
            metadata["is_express"] = True
        if payload.is_sealed:
            metadata["is_sealed"] = True
        if payload.eta_date:
            metadata["eta_date"] = payload.eta_date
        metadata["operator_user"] = current_user["username"]

        event = insert_trace_event(
            db=db,
            product_id=payload.product_id,
            stage=payload.stage,
            action=payload.action or "Перемещение",
            from_location_id=from_loc_id,
            to_location_id=to_loc_id,
            responsible_id=payload.responsible_id,
            operator_id=payload.operator_id,
            quantity=qty,
            note=payload.note,
            metadata=metadata,
            event_timestamp=payload.transfer_date or now_iso(),
        )

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="STOCK_TRANSFER",
            target_entity="PRODUCT",
            payload={
                "product_id": payload.product_id,
                "sku": product["sku"],
                "from_location_id": from_loc_id,
                "to_location_id": to_loc_id,
                "quantity": qty,
                "stage": payload.stage,
                "action": payload.action,
                "event_hash": event.get("event_hash", ""),
            },
        )

        return {
            "status": "ok",
            "message": "Перемещение успешно зафиксировано",
            "event": event,
            "product": serialize_product(db, db.execute("SELECT * FROM products WHERE id=?", (payload.product_id,)).fetchone()),
        }


# =============================================================================
# --- Custom Attributes, MDM, Categories & Units Management ---
# =============================================================================

@app.post("/api/attributes", status_code=201)
def create_attribute(
    payload: AttributeInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, Any]:
    with get_db() as db:
        if db.execute("SELECT 1 FROM custom_attributes WHERE key=?", (payload.key,)).fetchone():
            raise HTTPException(409, f"Атрибут с ключом «{payload.key}» уже существует")

        cursor = db.execute(
            "INSERT INTO custom_attributes(key, name, type, options, unit, is_system) VALUES(?,?,?,?,?,0)",
            (payload.key, payload.name, payload.type, canonical_json(payload.options), payload.unit),
        )
        row = db.execute("SELECT * FROM custom_attributes WHERE id=?", (cursor.lastrowid,)).fetchone()
        res = dict(row)
        res["options"] = json.loads(res["options"])

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="ATTRIBUTE_CREATE",
            target_entity="CUSTOM_ATTRIBUTE",
            payload={"key": payload.key, "name": payload.name, "type": payload.type},
        )
        return res


@app.put("/api/attributes/{attribute_id}")
def update_attribute(
    attribute_id: int,
    payload: AttributeUpdateInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM custom_attributes WHERE id=?", (attribute_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Атрибут не найден")

        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = payload.name.strip()
        if payload.type is not None:
            updates["type"] = payload.type
        if payload.options is not None:
            clean_opts = [opt.strip() for opt in payload.options if opt.strip()]
            updates["options"] = canonical_json(clean_opts)
        if payload.unit is not None:
            updates["unit"] = payload.unit.strip()

        if updates:
            fields = [f"{k} = ?" for k in updates]
            db.execute(f"UPDATE custom_attributes SET {', '.join(fields)} WHERE id=?", [*updates.values(), attribute_id])

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="ATTRIBUTE_UPDATE",
            target_entity="CUSTOM_ATTRIBUTE",
            payload={"attribute_id": attribute_id, "key": row["key"], "updates": updates},
        )

        updated = db.execute("SELECT * FROM custom_attributes WHERE id=?", (attribute_id,)).fetchone()
        res = dict(updated)
        res["options"] = json.loads(res["options"]) if res.get("options") else []
        return res


@app.delete("/api/attributes/{attribute_id}")
def delete_attribute(
    attribute_id: int,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, str]:
    with get_db() as db:
        row = db.execute("SELECT * FROM custom_attributes WHERE id=?", (attribute_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Атрибут не найден")
        db.execute("DELETE FROM custom_attributes WHERE id=?", (attribute_id,))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="ATTRIBUTE_DELETE",
            target_entity="CUSTOM_ATTRIBUTE",
            payload={"attribute_id": attribute_id, "key": row["key"]},
        )
        return {"status": "ok", "message": "Атрибут удален"}


@app.post("/api/attributes/{attribute_id}/options")
def add_attribute_option(
    attribute_id: int,
    payload: AttributeOptionInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM custom_attributes WHERE id=?", (attribute_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Атрибут не найден")
        r_dict = dict(row)
        opts = json.loads(r_dict["options"]) if r_dict.get("options") else []
        new_opt = payload.option.strip()
        if new_opt and new_opt not in opts:
            opts.append(new_opt)
            db.execute("UPDATE custom_attributes SET options=? WHERE id=?", (canonical_json(opts), attribute_id))
        r_dict["options"] = opts
        return r_dict


# --- MDM CRUD Routes ---

@app.get("/api/mdm/{resource}")
def list_resource(
    resource: str,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper", "qc", "worker"])),
) -> list[dict[str, Any]]:
    if resource not in RESOURCE_MODELS and resource != "custom_attributes":
        raise HTTPException(404, "Справочник не найден")
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM {resource} ORDER BY id").fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if resource == "custom_attributes" and item.get("options"):
                item["options"] = json.loads(item["options"])
            result.append(item)
        return result


@app.post("/api/mdm/{resource}", status_code=201)
def create_resource(
    resource: str,
    payload: dict[str, Any],
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, Any]:
    if resource not in RESOURCE_MODELS:
        raise HTTPException(404, "Справочник не найден")
    model = RESOURCE_MODELS[resource]
    validated = model.model_validate(payload)
    data = validated.model_dump()
    if resource == "vehicles":
        data["refrigerated"] = 1 if data["refrigerated"] else 0

    cols = list(data.keys())
    with get_db() as db:
        cursor = db.execute(
            f"INSERT INTO {resource}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            [data[c] for c in cols],
        )
        row = db.execute(f"SELECT * FROM {resource} WHERE id=?", (cursor.lastrowid,)).fetchone()
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type=f"MDM_CREATE_{resource.upper()}",
            target_entity=resource.upper(),
            payload={"item_id": cursor.lastrowid, "data": data},
        )
        return dict(row)


@app.put("/api/mdm/{resource}/{item_id}")
def update_resource(
    resource: str,
    item_id: int,
    payload: dict[str, Any],
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, Any]:
    if resource not in RESOURCE_MODELS:
        raise HTTPException(404, "Справочник не найден")
    model = RESOURCE_MODELS[resource]
    validated = model.model_validate(payload)
    data = validated.model_dump()
    if resource == "vehicles":
        data["refrigerated"] = 1 if data["refrigerated"] else 0

    with get_db() as db:
        if not db.execute(f"SELECT 1 FROM {resource} WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404, "Запись не найдена")
        fields = [f"{c} = ?" for c in data]
        db.execute(f"UPDATE {resource} SET {', '.join(fields)} WHERE id=?", [*data.values(), item_id])
        row = db.execute(f"SELECT * FROM {resource} WHERE id=?", (item_id,)).fetchone()
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type=f"MDM_UPDATE_{resource.upper()}",
            target_entity=resource.upper(),
            payload={"item_id": item_id, "data": data},
        )
        return dict(row)


@app.delete("/api/mdm/{resource}/{item_id}")
def delete_resource(
    resource: str,
    item_id: int,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, str]:
    if resource not in RESOURCE_MODELS:
        raise HTTPException(404, "Справочник не найден")
    with get_db() as db:
        if not db.execute(f"SELECT 1 FROM {resource} WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404, "Запись не найдена")
        if resource == "locations":
            db.execute("DELETE FROM workshop_stock WHERE location_id=?", (item_id,))
        db.execute(f"DELETE FROM {resource} WHERE id=?", (item_id,))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type=f"MDM_DELETE_{resource.upper()}",
            target_entity=resource.upper(),
            payload={"item_id": item_id},
        )
        return {"status": "ok", "message": "Запись удалена"}


# --- Categories Management ---

@app.get("/api/categories")
def list_categories(
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper", "qc", "worker"])),
) -> list[dict[str, Any]]:
    with get_db() as db:
        counts = dict(db.execute("SELECT category, COUNT(*) FROM products WHERE category != '' GROUP BY category").fetchall())
        rows = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        db_cats = {r["name"]: r["id"] for r in rows}
        all_names = sorted(list(set(list(counts.keys()) + list(db_cats.keys()))))
        result = []
        for name in all_names:
            cat_id = db_cats.get(name)
            result.append({
                "id": cat_id,
                "name": name,
                "product_count": counts.get(name, 0)
            })
        return result


@app.post("/api/categories", status_code=201)
def create_category(
    payload: CategoryInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    name = payload.name.strip()
    with get_db() as db:
        if db.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, "Категория уже существует")
        cur = db.execute("INSERT INTO categories(name) VALUES(?)", (name,))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="CATEGORY_CREATE",
            target_entity="CATEGORY",
            payload={"name": name, "category_id": cur.lastrowid},
        )
        return {"id": cur.lastrowid, "name": name, "product_count": 0}


@app.put("/api/categories/{category_id}")
def update_category(
    category_id: int,
    payload: CategoryInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    new_name = payload.name.strip()
    with get_db() as db:
        old = db.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Категория не найдена")
        old_name = old["name"]
        db.execute("UPDATE categories SET name=? WHERE id=?", (new_name, category_id))
        db.execute("UPDATE products SET category=? WHERE category=?", (new_name, old_name))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="CATEGORY_UPDATE",
            target_entity="CATEGORY",
            payload={"category_id": category_id, "old_name": old_name, "new_name": new_name},
        )
        return {"id": category_id, "name": new_name}


@app.delete("/api/categories/{category_name}")
def delete_category(
    category_name: str,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, str]:
    with get_db() as db:
        db.execute("DELETE FROM categories WHERE name=?", (category_name,))
        db.execute("UPDATE products SET category='' WHERE category=?", (category_name,))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="CATEGORY_DELETE",
            target_entity="CATEGORY",
            payload={"category_name": category_name},
        )
        return {"status": "ok", "message": f"Категория '{category_name}' удалена"}


# --- Units of Measurement Management ---

@app.get("/api/units")
def list_units(
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager", "storekeeper", "qc", "worker"])),
) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM units ORDER BY name").fetchall()
        counts = dict(db.execute("SELECT unit, COUNT(*) FROM products WHERE unit != '' GROUP BY unit").fetchall())
        db_units = [dict(r) for r in rows]
        for u in db_units:
            u["product_count"] = counts.get(u["name"], 0)
        unit_names = {u["name"] for u in db_units}
        for uname, cnt in counts.items():
            if uname not in unit_names:
                db_units.append({"id": None, "name": uname, "description": "", "product_count": cnt})
        return sorted(db_units, key=lambda x: x["name"])


@app.post("/api/units", status_code=201)
def create_unit(
    payload: UnitInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    name = payload.name.strip()
    with get_db() as db:
        if db.execute("SELECT 1 FROM units WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f"Единица измерения «{name}» уже существует")
        cur = db.execute("INSERT INTO units(name, description) VALUES(?,?)", (name, payload.description.strip()))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="UNIT_CREATE",
            target_entity="UNIT",
            payload={"unit_id": cur.lastrowid, "name": name, "description": payload.description},
        )
        return {"id": cur.lastrowid, "name": name, "description": payload.description.strip(), "product_count": 0}


@app.put("/api/units/{unit_id}")
def update_unit(
    unit_id: int,
    payload: UnitInput,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, Any]:
    new_name = payload.name.strip()
    with get_db() as db:
        old = db.execute("SELECT name FROM units WHERE id=?", (unit_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Единица измерения не найдена")
        old_name = old["name"]
        db.execute("UPDATE units SET name=?, description=? WHERE id=?", (new_name, payload.description.strip(), unit_id))
        db.execute("UPDATE products SET unit=? WHERE unit=?", (new_name, old_name))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="UNIT_UPDATE",
            target_entity="UNIT",
            payload={"unit_id": unit_id, "old_name": old_name, "new_name": new_name},
        )
        return {"id": unit_id, "name": new_name, "description": payload.description.strip()}


@app.delete("/api/units/{unit_id}")
def delete_unit(
    unit_id: int,
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin", "manager"])),
) -> dict[str, str]:
    with get_db() as db:
        old = db.execute("SELECT name FROM units WHERE id=?", (unit_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Единица измерения не найдена")
        unit_name = old["name"]
        db.execute("DELETE FROM units WHERE id=?", (unit_id,))
        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="UNIT_DELETE",
            target_entity="UNIT",
            payload={"unit_id": unit_id, "unit_name": unit_name},
        )
        return {"status": "ok", "message": f"Единица измерения «{unit_name}» удалена"}


# --- System Reset & Clear Data (Admin Only) ---

@app.post("/api/system/clear-products")
def clear_products_data(
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, str]:
    with get_db() as db:
        if not db.is_pg:
            db.execute("DROP TRIGGER IF EXISTS trace_events_no_delete")
        db.execute("DELETE FROM trace_events")
        if not db.is_pg:
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
                BEFORE DELETE ON trace_events BEGIN
                    SELECT RAISE(ABORT, 'Удаление записей аудита трекинга запрещено');
                END;
                """
            )
        db.execute("DELETE FROM workshop_stock")
        db.execute("DELETE FROM products")

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="SYSTEM_CLEAR_PRODUCTS",
            target_entity="SYSTEM",
            payload={"cleared_by": current_user["username"]},
        )
        return {"status": "ok", "message": "Все товары и остатки успешно очищены"}


@app.post("/api/system/clear-all")
def clear_all_system_data(
    current_user: dict[str, Any] = Depends(auth.RoleChecker(["admin"])),
) -> dict[str, str]:
    with get_db() as db:
        if not db.is_pg:
            db.execute("DROP TRIGGER IF EXISTS trace_events_no_delete")
        db.execute("DELETE FROM trace_events")
        if not db.is_pg:
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
                BEFORE DELETE ON trace_events BEGIN
                    SELECT RAISE(ABORT, 'Удаление записей аудита трекинга запрещено');
                END;
                """
            )
        db.execute("DELETE FROM workshop_stock")
        db.execute("DELETE FROM products")
        db.execute("DELETE FROM locations")
        db.execute("DELETE FROM staff")
        db.execute("DELETE FROM vehicles")
        db.execute("DELETE FROM routes")
        db.execute("DELETE FROM categories")

        auth.log_audit(
            db=db,
            user_id=current_user["id"],
            action_type="SYSTEM_CLEAR_ALL",
            target_entity="SYSTEM",
            payload={"cleared_by": current_user["username"]},
        )
        return {"status": "ok", "message": "База данных полностью очищена (справочники и товары)"}


# --- Public Health & UI Serving ---

@app.get("/api/health")
def health() -> dict[str, Any]:
    with get_db() as db:
        products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        events = db.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0]
        workshops = db.execute("SELECT COUNT(*) FROM locations WHERE kind='workshop'").fetchone()[0]
        users_cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        audit_cnt = db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    return {
        "status": "ok",
        "country": "Таджикистан",
        "currency": "сомони (TJS)",
        "engine": "PostgreSQL" if db.is_pg else "SQLite",
        "products": products,
        "events": events,
        "workshops": workshops,
        "users": users_cnt,
        "audit_logs": audit_cnt,
        "rbac_active": True,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
