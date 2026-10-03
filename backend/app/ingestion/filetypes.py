"""Decides what an uploaded file is from its bytes, not just its name, so a renamed or
corrupt file is rejected at upload time instead of failing later in the pipeline."""

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

from app.models import DocumentKind


class UnsupportedFile(ValueError):
    pass


@dataclass(frozen=True)
class FileType:
    kind: DocumentKind
    mime: str
    ext: str


_PDF = FileType(DocumentKind.PDF, "application/pdf", "pdf")
_PNG = FileType(DocumentKind.IMAGE, "image/png", "png")
_JPEG = FileType(DocumentKind.IMAGE, "image/jpeg", "jpg")
_WEBP = FileType(DocumentKind.IMAGE, "image/webp", "webp")
_DOCX = FileType(
    DocumentKind.DOCX,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "docx",
)
_XLSX = FileType(
    DocumentKind.SHEET,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsx",
)
_CSV = FileType(DocumentKind.SHEET, "text/csv", "csv")

SUPPORTED_DESCRIPTION = "PDF, JPG, PNG, WEBP, DOCX, XLSX or CSV"


def _zip_office_type(data: bytes) -> FileType | None:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return _DOCX
    if "xl/workbook.xml" in names:
        return _XLSX
    return None


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # Possibly cut mid-character, or a legacy Windows encoding; allow cp1252 text.
        try:
            sample.decode("cp1252")
            return True
        except UnicodeDecodeError:
            return False


def detect(filename: str, data: bytes) -> FileType:
    ext = PurePath(filename).suffix.lower().lstrip(".")
    if data.startswith(b"%PDF-"):
        return _PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _PNG
    if data.startswith(b"\xff\xd8\xff"):
        return _JPEG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _WEBP
    if data.startswith(b"PK\x03\x04"):
        if office := _zip_office_type(data):
            return office
        raise UnsupportedFile("This archive is not a Word or Excel file.")
    if ext == "csv" and _looks_like_text(data):
        return _CSV
    if ext in {"doc", "xls"}:
        raise UnsupportedFile(
            f"Old .{ext} files aren't supported. Save it as .{ext}x in Office and upload again."
        )
    raise UnsupportedFile(f"Unsupported file type. Upload {SUPPORTED_DESCRIPTION}.")
