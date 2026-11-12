import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from core.auth import get_invoice_user, require_admin
from services import dropbox_service
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


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
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
    return content, ctype


@router.post("/extract")
async def extract(file: UploadFile = File(...), _: dict = Depends(get_invoice_user)):
    """Step 1: analyse the upload and return the fields for the user to review. Writes nothing."""
    content, ctype = await _read_upload(file)
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
async def add_invoice(
    sheet_id: int = Form(...),            # invoice_sheets.id
    fields: str = Form(...),              # JSON of the reviewed InvoiceFields
    file: UploadFile = File(...),
    user: dict = Depends(get_invoice_user),
):
    """Step 2: write the reviewed fields to the sheet, then save the receipt file to Dropbox."""
    try:
        parsed = InvoiceFields(**json.loads(fields))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid invoice fields") from exc
    sheet = get_invoice_sheet(sheet_id)
    if not sheet:
        raise HTTPException(status_code=400, detail="Unknown sheet")
    content, ctype = await _read_upload(file)

    try:
        result = append_to_materials(sheet["spreadsheet_id"], parsed.model_dump(), user["email"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sheet write failed")
        raise HTTPException(
            status_code=502,
            detail=f"Could not write to the sheet (is it shared with the service account as Editor?): {exc}",
        ) from exc

    # The row is already in the sheet; a Dropbox problem is reported but does not undo it.
    receipt = {"saved": False, "path": None, "error": None}
    if not dropbox_service.is_configured():
        receipt["error"] = "Dropbox is not configured on the server"
    else:
        try:
            receipt["path"] = dropbox_service.save_receipt(
                content, ctype, parsed.vendor, parsed.date, parsed.invoice_number
            )
            receipt["saved"] = True
        except Exception as exc:
            logger.exception("Dropbox save failed")
            receipt["error"] = str(exc)
    return {**result, "receipt": receipt}
