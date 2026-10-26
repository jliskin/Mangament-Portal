"""Upload storage with validation.

Files are stored under opaque random keys (never the user-supplied name, which
prevents path traversal and filename collisions). The content type is decided
from magic bytes, not from the browser-supplied header.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import date

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Document

ALLOWED = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
}
EXT = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "text/csv": ".csv",
       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}


class UploadRejected(ValueError):
    pass


def sniff_mime(content: bytes, filename: str) -> str:
    for mime, sigs in ALLOWED.items():
        if any(content.startswith(s) for s in sigs):
            return mime
    if filename.lower().endswith((".csv", ".txt")) and b"\x00" not in content[:4096]:
        return "text/csv"
    raise UploadRejected("Tipo de archivo no permitido (PDF, JPG, PNG, XLSX o CSV)")


def save_upload(db: Session, content: bytes, filename: str, kind: str, user_id: int | None,
                allowed: set[str] | None = None) -> Document:
    settings = get_settings()
    if not content:
        raise UploadRejected("Archivo vacío")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise UploadRejected(f"Archivo mayor a {settings.max_upload_mb} MB")
    mime = sniff_mime(content, filename)
    if allowed and mime not in allowed:
        raise UploadRejected("Tipo de archivo no permitido para este campo")
    key = f"{kind}/{date.today():%Y/%m}/{secrets.token_hex(16)}{EXT.get(mime, '')}"
    path = settings.storage_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    doc = Document(kind=kind, original_filename=filename[:255], storage_key=key, mime_type=mime,
                   size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), uploaded_by=user_id)
    db.add(doc)
    db.flush()
    return doc


def document_path(doc: Document):
    return get_settings().storage_dir / doc.storage_key
