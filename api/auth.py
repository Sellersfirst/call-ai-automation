import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config.database import get_connection
from core.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    role: str
    email: str


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "user"
    job_ids: list[int] = []
    prompt_ids: list[int] = []


class UserAccessUpdate(BaseModel):
    role: str | None = None
    job_ids: list[int] | None = None
    prompt_ids: list[int] | None = None


#  LOGIN / SESSION

@router.post("/login", response_model=LoginResponse)
def login(data: LoginRequest):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, role FROM users WHERE email=%s",
            (data.email,),
        ).fetchone()

    if not row or not verify_password(data.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user_id=row["id"], role=row["role"])
    return LoginResponse(access_token=token, role=row["role"], email=row["email"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return user


#  USER MANAGEMENT (admin only)

@router.get("/users", dependencies=[Depends(require_admin)])
def list_users():
    with get_connection() as conn:
        users = conn.execute("SELECT id, email, role, created_at FROM users ORDER BY id").fetchall()
        job_rows = conn.execute("SELECT user_id, sheet_id FROM user_job_access").fetchall()
        prompt_rows = conn.execute("SELECT user_id, prompt_id FROM user_prompt_access").fetchall()

    jobs_by_user: dict[int, list[int]] = {}
    for r in job_rows:
        jobs_by_user.setdefault(r["user_id"], []).append(r["sheet_id"])
    prompts_by_user: dict[int, list[int]] = {}
    for r in prompt_rows:
        prompts_by_user.setdefault(r["user_id"], []).append(r["prompt_id"])

    return [
        {
            "id": u["id"],
            "email": u["email"],
            "role": u["role"],
            "created_at": str(u["created_at"]),
            "job_ids": jobs_by_user.get(u["id"], []),
            "prompt_ids": prompts_by_user.get(u["id"], []),
        }
        for u in users
    ]


@router.post("/users", dependencies=[Depends(require_admin)])
def create_user(data: UserCreate):
    if data.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")

    with get_connection() as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE email=%s", (data.email,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="A user with this email already exists")

        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
            (data.email, hash_password(data.password), data.role),
        )
        user_id = cursor.fetchone()[0]

        for sheet_id in data.job_ids:
            conn.execute(
                "INSERT INTO user_job_access (user_id, sheet_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, sheet_id),
            )
        for prompt_id in data.prompt_ids:
            conn.execute(
                "INSERT INTO user_prompt_access (user_id, prompt_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, prompt_id),
            )
        conn.commit()

    return {"id": user_id, "message": "User created"}


@router.put("/users/{user_id}/access", dependencies=[Depends(require_admin)])
def update_user_access(user_id: int, data: UserAccessUpdate):
    with get_connection() as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE id=%s", (user_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="User not found")

        if data.role is not None:
            if data.role not in ("admin", "user"):
                raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
            conn.execute("UPDATE users SET role=%s WHERE id=%s", (data.role, user_id))

        if data.job_ids is not None:
            conn.execute("DELETE FROM user_job_access WHERE user_id=%s", (user_id,))
            for sheet_id in data.job_ids:
                conn.execute(
                    "INSERT INTO user_job_access (user_id, sheet_id) VALUES (%s, %s)",
                    (user_id, sheet_id),
                )

        if data.prompt_ids is not None:
            conn.execute("DELETE FROM user_prompt_access WHERE user_id=%s", (user_id,))
            for prompt_id in data.prompt_ids:
                conn.execute(
                    "INSERT INTO user_prompt_access (user_id, prompt_id) VALUES (%s, %s)",
                    (user_id, prompt_id),
                )

        conn.commit()

    return {"message": "Access updated"}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, current: dict = Depends(require_admin)):
    if current["id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    with get_connection() as conn:
        cur = conn.execute("DELETE FROM users WHERE id=%s", (user_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()

    return {"message": "User deleted"}
