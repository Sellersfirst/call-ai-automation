import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core.auth import get_current_user
from services.invoice_service import (
    append_to_materials, configured_sheet_ids, extract_invoice, list_invoice_sheets,
)

logger = logging.getLogger("invoices_api")
router = APIRouter(prefix="/api/invoices", tags=["Invoices"])

ALLOWED = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_BYTES = 20 * 1024 * 1024


@router.get("/sheets")
def sheets(_: dict = Depends(get_current_user)):
    return {"sheets": list_invoice_sheets()}


@router.post("/add")
async def add_invoice(
    sheet_id: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    if sheet_id not in configured_sheet_ids():
        raise HTTPException(status_code=400, detail="Unknown sheet")
    ctype = (file.content_type or "").lower()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype not in ALLOWED:
        raise HTTPException(status_code=400, detail="Upload a PDF or an image (PNG, JPG, WEBP)")
    content = await file.read()
    if not content or len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File is empty or larger than 20MB")

    try:
        data = extract_invoice(content, ctype, file.filename or "invoice")
    except Exception as exc:
        logger.exception("Invoice extraction failed")
        raise HTTPException(status_code=502, detail=f"Invoice analysis failed: {exc}") from exc

    try:
        result = append_to_materials(sheet_id, data, user["email"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sheet write failed")
        raise HTTPException(
            status_code=502,
            detail=f"Could not write to the sheet (is it shared with the service account as Editor?): {exc}",
        ) from exc
    return {"extracted": data, **result}
