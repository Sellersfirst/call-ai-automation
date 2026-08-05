import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from config.database import get_connection
from core.auth import get_current_user, require_admin, get_accessible_prompt_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prompts", tags=["Prompts"])


def _assert_prompt_access(prompt_id: int, user: dict) -> None:
    accessible = get_accessible_prompt_ids(user)
    if accessible is not None and prompt_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this prompt")


class PromptCreate(BaseModel):
    prompt_text: str
    type: str = "rubrics"
    active: bool = True


class PromptUpdate(BaseModel):
    prompt_text: str | None = None
    type: str | None = None
    active: bool | None = None


class PromptResponse(BaseModel):
    id: int
    prompt_text: str
    type: str
    active: bool
    created_at: str
    updated_at: str


@router.get("", response_model=list[PromptResponse])
async def get_prompts(user: dict = Depends(get_current_user)):
    """Get all prompts accessible to the current user (all of them, for admins)."""
    try:
        accessible_ids = get_accessible_prompt_ids(user)
        with get_connection() as conn:
            if accessible_ids is None:
                rows = conn.execute(
                    "SELECT id, prompt_text, type, active, created_at, updated_at FROM prompts ORDER BY id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, prompt_text, type, active, created_at, updated_at FROM prompts WHERE id = ANY(%s) ORDER BY id DESC",
                    (accessible_ids,),
                ).fetchall()

            return [
                PromptResponse(
                    id=row["id"],
                    prompt_text=row["prompt_text"],
                    type=row["type"],
                    active=row["active"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]
    except Exception as e:
        logger.error(f"Error fetching prompts: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch prompts")


@router.get("/active", response_model=PromptResponse)
async def get_active_prompt(prompt_id: int, user: dict = Depends(get_current_user)):
    """Get a prompt by ID."""
    _assert_prompt_access(prompt_id, user)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, prompt_text, type, active, created_at, updated_at FROM prompts WHERE id=%s",
                (prompt_id,),
            ).fetchone()
            
            if not row:
                raise HTTPException(status_code=404, detail="Prompt not found")
            
            return PromptResponse(
                id=row["id"],
                prompt_text=row["prompt_text"],
                type=row["type"],
                active=row["active"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching prompt: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch prompt")


@router.post("", response_model=PromptResponse, dependencies=[Depends(require_admin)])
async def create_prompt(data: PromptCreate):
    """Create a new prompt."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "INSERT INTO prompts (prompt_text, type, active) VALUES (%s, %s, %s) RETURNING id, prompt_text, type, active, created_at, updated_at",
                (data.prompt_text, data.type, data.active)
            ).fetchone()
            
            conn.commit()
            
            return PromptResponse(
                id=row["id"],
                prompt_text=row["prompt_text"],
                type=row["type"],
                active=row["active"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
    except Exception as e:
        logger.error(f"Error creating prompt: {e}")
        raise HTTPException(status_code=500, detail="Failed to create prompt")


@router.delete("/{prompt_id}", dependencies=[Depends(require_admin)])
async def delete_prompt(prompt_id: int):
    """Delete a prompt by ID."""
    try:
        with get_connection() as conn:
            cur = conn.execute("DELETE FROM prompts WHERE id=%s", (prompt_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Prompt not found")
            conn.commit()
            return {"deleted": prompt_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting prompt: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete prompt")


@router.put("/{prompt_id}", response_model=PromptResponse)
async def update_prompt(prompt_id: int, data: PromptUpdate, user: dict = Depends(get_current_user)):
    """Update a prompt by ID."""
    _assert_prompt_access(prompt_id, user)
    try:
        with get_connection() as conn:
            # Check if prompt exists
            existing = conn.execute(
                "SELECT id FROM prompts WHERE id=%s",
                (prompt_id,)
            ).fetchone()
            
            if not existing:
                raise HTTPException(status_code=404, detail="Prompt not found")
            
            # Build update query
            updates = []
            params = []
            
            if data.prompt_text is not None:
                updates.append("prompt_text=%s")
                params.append(data.prompt_text)
            
            if data.type is not None:
                updates.append("type=%s")
                params.append(data.type)
            
            if data.active is not None:
                updates.append("active=%s")
                params.append(data.active)
            
            if not updates:
                raise HTTPException(status_code=400, detail="No fields to update")
            
            updates.append("updated_at=CURRENT_TIMESTAMP")
            params.append(prompt_id)
            
            query = f"UPDATE prompts SET {', '.join(updates)} WHERE id=%s RETURNING id, prompt_text, type, active, created_at, updated_at"
            
            row = conn.execute(query, params).fetchone()
            conn.commit()
            
            return PromptResponse(
                id=row["id"],
                prompt_text=row["prompt_text"],
                type=row["type"],
                active=row["active"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating prompt: {e}")
        raise HTTPException(status_code=500, detail="Failed to update prompt")
