"""Content-addressed file storage on the office's own disk:
<storage_dir>/<company_id>/<yyyy>/<sha256>.<ext>, with rendered pages alongside in
<sha256>.pages/page-<n>.png."""

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings


def storage_root() -> Path:
    root = get_settings().storage_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_original(company_id: int, sha256: str, ext: str, data: bytes) -> str:
    """Writes the file (if not already present) and returns its path relative to the root."""
    rel = Path(str(company_id)) / str(datetime.now(UTC).year) / f"{sha256}.{ext}"
    path = storage_root() / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return rel.as_posix()


def absolute(rel_path: str) -> Path:
    root = storage_root().resolve()
    path = (root / rel_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes storage root")
    return path


def pages_dir(rel_path: str) -> Path:
    original = absolute(rel_path)
    return original.with_name(original.stem + ".pages")


def page_image(rel_path: str, number: int) -> Path:
    return pages_dir(rel_path) / f"page-{number}.png"


def delete_files(rel_path: str, keep_original: bool = False) -> None:
    shutil.rmtree(pages_dir(rel_path), ignore_errors=True)
    if not keep_original:
        absolute(rel_path).unlink(missing_ok=True)
