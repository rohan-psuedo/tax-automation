from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.parsing.base import ParsedDocument, ParsedPage, ParseError

# Long side of the normalised page image. Large enough for small print on an A4 phone
# photo, small enough to keep vision-model requests cheap.
_MAX_SIDE = 2400

Image.MAX_IMAGE_PIXELS = 80_000_000  # guard against decompression bombs


def parse_image(path: Path, out_dir: Path) -> ParsedDocument:
    try:
        with Image.open(path) as img:
            img.load()
            # Phone photos carry their rotation in EXIF; apply it so the page is upright.
            page = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ParseError("This image is damaged or in an unsupported format.") from exc

    page.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
    out_dir.mkdir(parents=True, exist_ok=True)
    page.save(out_dir / "page-1.png", optimize=True)

    result = ParsedDocument(page_count=1)
    result.pages.append(ParsedPage(number=1, width=page.width, height=page.height, has_text=False))
    if min(page.size) < 600:
        result.warnings.append("Low-resolution image; small print may be hard to read.")
    return result
