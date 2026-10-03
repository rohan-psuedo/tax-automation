from pathlib import Path

from app.config import get_settings
from app.models import DocumentKind
from app.parsing.base import ParsedDocument, ParseError
from app.parsing.image import parse_image
from app.parsing.office import parse_csv, parse_docx, parse_xlsx
from app.parsing.pdf import parse_pdf


def parse_file(path: Path, kind: str, pages_out: Path) -> ParsedDocument:
    settings = get_settings()
    match kind:
        case DocumentKind.PDF:
            return parse_pdf(
                path, pages_out, dpi=settings.page_render_dpi, max_pages=settings.max_pages
            )
        case DocumentKind.IMAGE:
            return parse_image(path, pages_out)
        case DocumentKind.DOCX:
            return parse_docx(path)
        case DocumentKind.SHEET:
            return parse_csv(path) if path.suffix.lower() == ".csv" else parse_xlsx(path)
    raise ParseError(f"No reader for '{kind}' files.")
