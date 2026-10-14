import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from config import settings

ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def save_upload(file: UploadFile) -> Path:
    """Validate and save an uploaded image; returns the saved path."""
    ext = ALLOWED_TYPES.get(file.content_type or "")
    if ext is None:
        raise HTTPException(415, "Upload a JPG, PNG or WebP image.")

    path = settings.upload_dir / f"{uuid.uuid4().hex}{ext}"
    with path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    if path.stat().st_size > settings.max_upload_mb * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise HTTPException(413, f"Image is larger than {settings.max_upload_mb} MB.")
    return path
