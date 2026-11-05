from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from database import DBConnection, get_db, now_iso

# Configuration
SECRET_KEY = os.environ.get(
    "JWT_SECRET_KEY",
    "treking_v2_enterprise_jwt_super_secret_key_2026_dushanbe_tajikistan_security_512",
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 24 hours

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


# --- Password Hashing (bcrypt / passlib standards) ---

def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt with standard 12 salt rounds."""
    # Truncate to 72 bytes as required by bcrypt standard
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        pwd_bytes = plain_password.encode("utf-8")[:72]
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(pwd_bytes, hash_bytes)
    except Exception:
        return False


# --- JWT Token Generation & Verification ---

def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Срок действия токена авторизации истек",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный токен авторизации",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --- Pydantic Auth Models ---

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=5, max_length=100)
    full_name: str = Field(min_length=2, max_length=100)
    role: str = Field(pattern=r"^(admin|manager|storekeeper|qc|worker)$")
    workshop_id: int | None = None
    is_active: bool = True


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: str | None = Field(default=None, pattern=r"^(admin|manager|storekeeper|qc|worker)$")
    workshop_id: int | None = None
    is_active: bool | None = None
    password: str | None = None


# --- FastAPI User Dependencies ---

def get_current_user(
    token: str | None = Depends(oauth2_scheme),
) -> dict[str, Any]:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Необходима авторизация (отсутствует Bearer токен)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    username: str | None = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Некорректный токен: отсутствует идентификатор пользователя",
            headers={"WWW-Authenticate": "Bearer"},
        )

    with get_db() as db:
        user = db.execute(
            """
            SELECT u.id, u.username, u.full_name, u.role, u.workshop_id, u.is_active, u.created_at,
                   l.name AS workshop_name, l.kind AS workshop_kind, l.city AS workshop_city
            FROM users u
            LEFT JOIN locations l ON l.id = u.workshop_id
            WHERE u.username = ?
            """,
            (username,),
        ).fetchone()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Пользователь не найден в системе",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return dict(user)


def get_current_active_user(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if not bool(current_user.get("is_active")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Учетная запись деактивирована администратором",
        )
    return current_user


class RoleChecker:
    """Universal RBAC dependency checking if user role is in allowed roles list."""

    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def __call__(
        self, current_user: dict[str, Any] = Depends(get_current_active_user)
    ) -> dict[str, Any]:
        user_role = current_user.get("role")
        if user_role not in self.allowed_roles:
            role_names = {
                "admin": "Администратор",
                "manager": "Менеджер",
                "storekeeper": "Завскладом",
                "qc": "Инспектор ОТК",
                "worker": "Мастер цеха",
            }
            allowed_names = [role_names.get(r, r) for r in self.allowed_roles]
            user_role_name = role_names.get(user_role, user_role)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Доступ запрещен для вашей роли «{user_role_name}» ({user_role}). "
                    f"Необходимы права: {', '.join(allowed_names)}."
                ),
            )
        return current_user


# --- Canonical JSON Helper ---

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --- Immutable Audit Trail (WORM / SHA-256 Chain) ---

def log_audit(
    db: DBConnection,
    user_id: int | None,
    action_type: str,
    target_entity: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Log an operation into the immutable audit trail table with SHA-256 chain integrity.
    Every entry links to the previous entry's current_hash to guarantee tamper-evidence.
    """
    last_log = db.execute(
        "SELECT current_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    prev_hash = last_log["current_hash"] if last_log else "0" * 64
    created_at = now_iso()
    payload_str = canonical_json(payload)

    # Cryptographic block hashing
    block_data = {
        "prev_hash": prev_hash,
        "user_id": user_id,
        "action_type": action_type,
        "target_entity": target_entity,
        "payload": payload_str,
        "created_at": created_at,
    }
    current_hash = hashlib.sha256(canonical_json(block_data).encode("utf-8")).hexdigest()

    cur = db.execute(
        """
        INSERT INTO audit_logs(user_id, action_type, target_entity, payload, prev_hash, current_hash, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, action_type, target_entity, payload_str, prev_hash, current_hash, created_at),
    )
    log_id = cur.lastrowid

    return {
        "id": log_id,
        "user_id": user_id,
        "action_type": action_type,
        "target_entity": target_entity,
        "payload": payload,
        "prev_hash": prev_hash,
        "current_hash": current_hash,
        "created_at": created_at,
    }


def verify_audit_chain(db: DBConnection) -> dict[str, Any]:
    """
    Audit Trail cryptographic verification engine.
    Traverses the entire ledger from genesis to head, recalculating SHA-256 hashes
    and validating cryptographic chain links.
    """
    rows = db.execute(
        "SELECT id, user_id, action_type, target_entity, payload, prev_hash, current_hash, created_at FROM audit_logs ORDER BY id ASC"
    ).fetchall()

    if not rows:
        return {
            "verified": True,
            "total_records": 0,
            "head_hash": "0" * 64,
            "message": "Журнал аудита пуст. Цепочка целостна.",
        }

    expected_prev_hash = "0" * 64
    for idx, r in enumerate(rows):
        # 1. Validate previous hash link
        if r["prev_hash"] != expected_prev_hash:
            return {
                "verified": False,
                "error": "РАЗРЫВ_ЦЕПОЧКИ_ХЕШЕЙ",
                "compromised_id": r["id"],
                "index": idx,
                "expected_prev_hash": expected_prev_hash,
                "found_prev_hash": r["prev_hash"],
                "message": f"Нарушение целостности в записи #{r['id']}: ссылка prev_hash не совпадает!",
            }

        # 2. Validate current block hash
        block_data = {
            "prev_hash": r["prev_hash"],
            "user_id": r["user_id"],
            "action_type": r["action_type"],
            "target_entity": r["target_entity"],
            "payload": r["payload"],
            "created_at": r["created_at"],
        }
        computed_hash = hashlib.sha256(canonical_json(block_data).encode("utf-8")).hexdigest()
        if computed_hash != r["current_hash"]:
            return {
                "verified": False,
                "error": "ПОДДЕЛКА_ДАННЫХ_БЛОКА",
                "compromised_id": r["id"],
                "index": idx,
                "computed_hash": computed_hash,
                "stored_hash": r["current_hash"],
                "message": f"Подделка содержимого в записи #{r['id']}: SHA-256 хеш блока не сходится!",
            }

        expected_prev_hash = r["current_hash"]

    return {
        "verified": True,
        "total_records": len(rows),
        "head_hash": expected_prev_hash,
        "message": f"Криптографическая проверка успешно пройдена. Проверено {len(rows)} неизменяемых записей.",
        "verified_at": now_iso(),
    }


# --- Seed Default Users ---

def seed_default_users(db: DBConnection) -> None:
    """Seed initial enterprise users if users table is empty."""
    cnt_row = db.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
    if cnt_row and cnt_row["cnt"] > 0:
        return

    users_to_seed = [
        ("admin", "admin123", "Главный Администратор", "admin", None),
        ("manager", "manager123", "Фарход Каримов", "manager", 1),
        ("storekeeper", "store123", "Сино Рахимов", "storekeeper", 1),
        ("qc", "qc123", "Нигина Алиева", "qc", 3),
        ("worker", "worker123", "Дилшод Назаров", "worker", 1),
    ]

    now_time = now_iso()
    for username, raw_password, full_name, role, workshop_id in users_to_seed:
        pwd_hash = hash_password(raw_password)
        db.execute(
            """
            INSERT INTO users(username, password_hash, full_name, role, workshop_id, is_active, created_at)
            VALUES(?, ?, ?, ?, ?, 1, ?)
            """,
            (username, pwd_hash, full_name, role, workshop_id, now_time),
        )

    # Log genesis block in audit log
    admin_row = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    admin_id = admin_row["id"] if admin_row else 1
    log_audit(
        db,
        user_id=admin_id,
        action_type="SYSTEM_INITIALIZE",
        target_entity="SYSTEM",
        payload={
            "description": "Инициализация корпоративной системы безопасности и ролевого доступа Treking-V2",
            "initial_roles_seeded": ["admin", "manager", "storekeeper", "qc", "worker"],
        },
    )
