from pathlib import Path

import pymupdf

from app.parsing.base import ParsedDocument, ParsedPage, ParseError

# Pages with fewer characters than this are treated as scanned (image-only).
_MIN_TEXT_CHARS = 20


def parse_pdf(path: Path, out_dir: Path, *, dpi: int, max_pages: int) -> ParsedDocument:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises several types for damaged files
        raise ParseError("This PDF is damaged and can't be opened.") from exc

    with doc:
        if doc.needs_pass:
            raise ParseError(
                "This PDF is password-protected. Remove the password and upload again."
            )
        if doc.page_count == 0:
            raise ParseError("This PDF has no pages.")

        result = ParsedDocument(page_count=doc.page_count)
        out_dir.mkdir(parents=True, exist_ok=True)
        texts = []
        for index, page in enumerate(doc):
            if index >= max_pages:
                result.warnings.append(
                    f"Only the first {max_pages} of {doc.page_count} pages were read."
                )
                break
            text = page.get_text("text").strip()
            has_text = len(text) >= _MIN_TEXT_CHARS
            if text:
                texts.append(f"--- Page {index + 1} ---\n{text}")
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.save(out_dir / f"page-{index + 1}.png")
            result.pages.append(
                ParsedPage(number=index + 1, width=pix.width, height=pix.height, has_text=has_text)
            )

        result.text = "\n\n".join(texts)
        result.has_text_layer = any(p.has_text for p in result.pages)
        if not result.has_text_layer:
            result.warnings.append("No text layer found. This looks like a scan.")
        return result
