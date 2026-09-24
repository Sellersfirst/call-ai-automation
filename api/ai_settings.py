from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import require_admin
from services.llm_service import get_provider_setting, save_ai_provider

router = APIRouter(prefix="/api/ai-settings", tags=["AI Settings"])


class ProviderUpdate(BaseModel):
    provider: Literal["claude", "openai"]


@router.get("/provider")
def read_provider(_: dict = Depends(require_admin)):
    try:
        return {"provider": get_provider_setting()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Failed to load AI provider setting") from exc


@router.put("/provider")
def update_provider(data: ProviderUpdate, _: dict = Depends(require_admin)):
    try:
        return {"provider": save_ai_provider(data.provider)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Failed to save AI provider setting") from exc
