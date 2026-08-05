from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

from config.config import JWT_SECRET_KEY
from config.database import get_connection

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: int, role: str) -> str:
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ")
    payload = _decode_token(token)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, role FROM users WHERE id=%s",
            (payload["user_id"],),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="User no longer exists")

    return dict(row)


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_accessible_sheet_ids(user: dict) -> list[int] | None:
    """None means unrestricted (admin). A list (possibly empty) scopes a regular user."""
    if user["role"] == "admin":
        return None
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT sheet_id FROM user_job_access WHERE user_id=%s",
            (user["id"],),
        ).fetchall()
    return [r["sheet_id"] for r in rows]


def get_accessible_prompt_ids(user: dict) -> list[int] | None:
    """
    None means unrestricted (admin). A list (possibly empty) scopes a regular user —
    the union of directly-granted prompts and prompts referenced by extraction_prompt_id
    on any job (sheet) the user has access to.
    """
    if user["role"] == "admin":
        return None

    with get_connection() as conn:
        direct = conn.execute(
            "SELECT prompt_id FROM user_prompt_access WHERE user_id=%s",
            (user["id"],),
        ).fetchall()
        via_jobs = conn.execute(
            """
            SELECT DISTINCT s.extraction_prompt_id AS prompt_id
            FROM sheets s
            JOIN user_job_access uja ON uja.sheet_id = s.id
            WHERE uja.user_id=%s AND s.extraction_prompt_id IS NOT NULL
            """,
            (user["id"],),
        ).fetchall()

    ids = {r["prompt_id"] for r in direct} | {r["prompt_id"] for r in via_jobs}
    return list(ids)
