from pathlib import Path

import pytest
from PIL import Image

from app.ingestion import filetypes
from app.models import DocumentKind
from app.parsing import ParseError, parse_file
from tests import samples


@pytest.mark.parametrize(
    ("name", "data", "kind", "ext"),
    [
        ("inv.pdf", samples.text_pdf(), DocumentKind.PDF, "pdf"),
        ("scan.png", samples.png(), DocumentKind.IMAGE, "png"),
        ("photo.jpeg", samples.rotated_jpeg(), DocumentKind.IMAGE, "jpg"),
        ("inv.docx", samples.docx_file(), DocumentKind.DOCX, "docx"),
        ("sales.xlsx", samples.xlsx_file(), DocumentKind.SHEET, "xlsx"),
        ("bank.csv", samples.csv_file(), DocumentKind.SHEET, "csv"),
        # Misleading extension: the content decides.
        ("invoice.png", samples.text_pdf(), DocumentKind.PDF, "pdf"),
    ],
    ids=["pdf", "png", "jpeg", "docx", "xlsx", "csv", "pdf-named-png"],
)
def test_detect(name, data, kind, ext):
    ftype = filetypes.detect(name, data)
    assert (ftype.kind, ftype.ext) == (kind, ext)


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("setup.exe", b"MZ\x90\x00binary", "Unsupported file type"),
        ("old.doc", b"\xd0\xcf\x11\xe0legacy", "Old .doc files"),
        ("bundle.zip", samples.docx_file()[:0] + b"PK\x03\x04garbage", "not a Word or Excel"),
        ("notes.txt", b"hello", "Unsupported file type"),
    ],
    ids=["exe", "legacy-doc", "plain-zip", "txt"],
)
def test_detect_rejects(name, data, message):
    with pytest.raises(filetypes.UnsupportedFile, match=message):
        filetypes.detect(name, data)


def _parse(tmp_path: Path, name: str, data: bytes, kind: str):
    src = tmp_path / name
    src.write_bytes(data)
    return parse_file(src, kind, tmp_path / "pages")


def test_text_pdf(tmp_path):
    result = _parse(tmp_path, "a.pdf", samples.text_pdf(pages=2), DocumentKind.PDF)
    assert result.page_count == 2 and len(result.pages) == 2
    assert result.has_text_layer
    assert "SE/2026/0042" in result.text and "--- Page 2 ---" in result.text
    assert (tmp_path / "pages" / "page-2.png").exists()
    assert result.warnings == []


def test_scanned_pdf_is_flagged(tmp_path):
    result = _parse(tmp_path, "a.pdf", samples.scanned_pdf(), DocumentKind.PDF)
    assert not result.has_text_layer
    assert result.pages[0].has_text is False
    assert any("scan" in w for w in result.warnings)


def test_password_pdf(tmp_path):
    with pytest.raises(ParseError, match="password-protected"):
        _parse(tmp_path, "a.pdf", samples.password_pdf(), DocumentKind.PDF)


def test_damaged_pdf(tmp_path):
    with pytest.raises(ParseError, match="damaged"):
        _parse(tmp_path, "a.pdf", b"%PDF-1.7\nthis is not really a pdf", DocumentKind.PDF)


def test_page_limit(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_pages", 2)
    result = _parse(tmp_path, "a.pdf", samples.text_pdf(pages=4), DocumentKind.PDF)
    assert result.page_count == 4 and len(result.pages) == 2
    assert "first 2 of 4 pages" in result.warnings[0]


def test_image_is_rotated_upright(tmp_path):
    result = _parse(tmp_path, "a.jpg", samples.rotated_jpeg(), DocumentKind.IMAGE)
    page = result.pages[0]
    assert (page.width, page.height) == (1200, 1600)  # portrait after EXIF rotation
    with Image.open(tmp_path / "pages" / "page-1.png") as img:
        assert img.size == (1200, 1600)


def test_large_image_is_downscaled(tmp_path):
    result = _parse(tmp_path, "a.png", samples.png(size=(4000, 3000)), DocumentKind.IMAGE)
    assert max(result.pages[0].width, result.pages[0].height) == 2400


def test_docx(tmp_path):
    result = _parse(tmp_path, "a.docx", samples.docx_file(), DocumentKind.DOCX)
    assert "Sharma Electronics" in result.text
    assert result.sheets[0].rows == [["Item", "Qty", "Amount"], ["Network switch", "2", "10000"]]


def test_xlsx(tmp_path):
    result = _parse(tmp_path, "a.xlsx", samples.xlsx_file(), DocumentKind.SHEET)
    sheet = result.sheets[0]
    assert sheet.name == "Sales"
    assert sheet.rows[1] == ["INV-1", "Gupta Retail", "1000"]
    assert "--- Sheet Sales ---" in result.text


def test_xlsx_row_cap(tmp_path, monkeypatch):
    from app.parsing import office

    monkeypatch.setattr(office, "MAX_SHEET_ROWS", 10)
    result = _parse(tmp_path, "a.xlsx", samples.xlsx_file(rows=25), DocumentKind.SHEET)
    assert len(result.sheets[0].rows) == 10 and result.sheets[0].total_rows == 26
    assert result.warnings


def test_csv_with_semicolons(tmp_path):
    result = _parse(tmp_path, "bank.csv", samples.csv_file(), DocumentKind.SHEET)
    assert result.sheets[0].rows[1] == ["01-09-2026", "NEFT Sharma Electronics", "11800", ""]
