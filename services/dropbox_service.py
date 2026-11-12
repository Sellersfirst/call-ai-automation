"""Save reviewed invoice files to Dropbox as <Receipts>/<Vendor>/<Year>/<file>."""

import json
import logging
import os
import re
import time
from datetime import datetime

import httpx

logger = logging.getLogger("dropbox_service")

_token = {"value": None, "expires": 0.0}
EXT = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def is_configured() -> bool:
    return all(os.getenv(k) for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"))


def _access_token() -> str:
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    resp = httpx.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"]},
        auth=(os.environ["DROPBOX_APP_KEY"], os.environ["DROPBOX_APP_SECRET"]),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Dropbox login failed (HTTP {resp.status_code})")
    data = resp.json()
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 14400))
    return _token["value"]


def _clean_name(text: str, fallback: str, limit: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].strip(" .") or fallback


def _parse_date(text: str) -> datetime:
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*", text or "")
    if m:
        month, day, year = int(m[1]), int(m[2]), int(m[3])
        year += 2000 if year < 100 else 0
        try:
            return datetime(year, month, day)
        except ValueError:
            pass
    return datetime.now()


def build_path(vendor: str, date_text: str, invoice_number: str, content_type: str) -> str:
    root = "/" + (os.getenv("DROPBOX_RECEIPTS_PATH", "/Receipts").strip("/ ") or "Receipts")
    d = _parse_date(date_text)
    vendor_dir = _clean_name(vendor, "Unknown Vendor")
    parts = [d.strftime("%Y-%m-%d"), vendor_dir]
    number = _clean_name(invoice_number, "", 40)
    if number:
        parts.append(number)
    filename = "_".join(parts).replace(" ", "-") + EXT.get(content_type, "")
    return f"{root}/{vendor_dir}/{d.year}/{filename}"


def save_receipt(content: bytes, content_type: str, vendor: str, date_text: str, invoice_number: str) -> str:
    """Upload the file and return its Dropbox path. Never overwrites (autorename)."""
    path = build_path(vendor, date_text, invoice_number, content_type)
    resp = httpx.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add", "autorename": True, "mute": True}),
            "Content-Type": "application/octet-stream",
        },
        content=content,
        timeout=120,
    )
    if resp.status_code != 200:
        logger.error("Dropbox upload failed %s: %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"Dropbox upload failed (HTTP {resp.status_code})")
    return resp.json().get("path_display", path)
