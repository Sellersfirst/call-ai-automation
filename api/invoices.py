import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from core.auth import get_invoice_user, require_admin
from services.invoice_service import (
    normalize_payment_method,
    add_invoice_sheet, append_to_materials, delete_invoice_sheet, extract_invoice,
    get_invoice_sheet, list_invoice_sheets,
)

logger = logging.getLogger("invoices_api")
router = APIRouter(prefix="/api/invoices", tags=["Invoices"])

ALLOWED = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
MAX_BYTES = 5 * 1024 * 1024
# File signatures: the declared content-type must match the real bytes
SIGNATURES = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),
}


class SheetAdd(BaseModel):
    url: str


@router.get("/sheets")
def sheets(_: dict = Depends(get_invoice_user)):
    return {"sheets": list_invoice_sheets()}


@router.post("/sheets")
def create_sheet(data: SheetAdd, _: dict = Depends(require_admin)):
    try:
        return add_invoice_sheet(data.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/sheets/{row_id}")
def remove_sheet(row_id: int, _: dict = Depends(require_admin)):
    if not delete_invoice_sheet(row_id):
        raise HTTPException(status_code=404, detail="Sheet not found")
    return {"message": "Sheet removed"}


class InvoiceFields(BaseModel):
    tag: str = ""
    date: str = ""
    vendor: str = ""
    invoice_number: str = ""
    items: str = ""
    amount: float | None = None
    payment_method: str = ""
    notes: str = ""


class InvoiceConfirm(BaseModel):
    sheet_id: int   # invoice_sheets.id
    fields: InvoiceFields


@router.post("/extract")
async def extract(file: UploadFile = File(...), _: dict = Depends(get_invoice_user)):
    """Step 1: analyse the upload and return the fields for the user to review. Writes nothing."""
    ctype = (file.content_type or "").lower()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype not in ALLOWED:
        raise HTTPException(status_code=400, detail="Upload a PDF or an image (PNG, JPG, WEBP)")
    content = await file.read(MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="File is empty")
    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File is larger than 5MB")
    if not content.startswith(SIGNATURES[ctype]) or (ctype == "image/webp" and content[8:12] != b"WEBP"):
        raise HTTPException(status_code=400, detail="File content doesn't match its type")
    try:
        data = extract_invoice(content, ctype, file.filename or "invoice")
    except Exception as exc:
        logger.exception("Invoice extraction failed")
        raise HTTPException(status_code=502, detail=f"Invoice analysis failed: {exc}") from exc
    data["payment_method"] = normalize_payment_method(data.get("payment_method"))
    return {"fields": InvoiceFields(**{k: (data.get(k) if data.get(k) is not None else d)
                                       for k, d in [("tag", ""), ("date", ""), ("vendor", ""),
                                                    ("invoice_number", ""), ("items", ""),
                                                    ("payment_method", ""), ("notes", "")]},
                                    amount=_amount(data.get("amount"))).model_dump()}


def _amount(v):
    try:
        return None if v in (None, "") else float(str(v).replace("$", "").replace(",", ""))
    except ValueError:
        return None


@router.post("/add")
def add_invoice(data: InvoiceConfirm, user: dict = Depends(get_invoice_user)):
    """Step 2: write the (user-reviewed) fields to the selected sheet's Materials tab."""
    sheet = get_invoice_sheet(data.sheet_id)
    if not sheet:
        raise HTTPException(status_code=400, detail="Unknown sheet")
    try:
        result = append_to_materials(sheet["spreadsheet_id"], data.fields.model_dump(), user["email"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sheet write failed")
        raise HTTPException(
            status_code=502,
            detail=f"Could not write to the sheet (is it shared with the service account as Editor?): {exc}",
        ) from exc
    return result
