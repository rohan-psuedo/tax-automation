"""What every AI service adapter shares: errors, the reply it returns, and how a document is
prepared for sending (whole PDF, page images or text, within the service's size limits).

An adapter (app.extraction.adapters.*) turns a Prepared document into one request to its
service and answers with a Reply; app.extraction.extractor drives the requests, retries a
cut-off answer once with a larger budget, and turns failures into messages for people.
"""

import base64
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from app.extraction import services as catalog
from app.extraction.services import Service
from app.schemas.extraction import InvoiceExtraction

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ExtractionError(Exception):
    """Reading the document failed. The message is shown to the user.

    retryable: the same request may work later (rate limit, timeout, server error).
    fix_in_settings: it will work once an administrator changes the AI settings (key, model,
    address, billing); the document is read again automatically when they do.
    """

    def __init__(
        self, message: str, retryable: bool = False, *, fix_in_settings: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.fix_in_settings = fix_in_settings


class ExtractionNotConfigured(ExtractionError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or not_configured_message(catalog.DEFAULT),
            retryable=False,
            fix_in_settings=True,
        )


def not_configured_message(service: Service) -> str:
    env = service.env_keys[0].upper() if service.env_keys else "the key"
    return (
        f"Invoice reading is not set up because no {service.key_name} was found. An "
        f"administrator can add one in Settings (or set {env} in backend/.env and restart the "
        "server)."
    )


@dataclass(frozen=True)
class Limits:
    """What one request to a service may carry."""

    max_pdf_bytes: int  # a larger PDF is sent as page images
    pdf_label: str  # max_pdf_bytes for people, e.g. "20 MB"
    max_image_payload: int  # base64 characters across all page images
    max_images: int  # pages sent when a PDF goes as images (always including the last)


@dataclass
class Prepared:
    """A document ready to send: a PDF, or images, and text that goes before the
    instructions (a Word document's text, a PDF's text layer, or a note about left-out
    pages)."""

    pdf: Path | None = None
    images: list[Path] = field(default_factory=list)
    lead: str | None = None

    def instructions(self, instructions: str) -> str:
        return f"{self.lead}\n\n{instructions}" if self.lead else instructions


Stop = Literal["done", "cut_off", "refused"]


@dataclass
class Reply:
    """One answer from a service. extraction is None when the answer could not be read as
    an InvoiceExtraction (cut off, refused, or not valid against the schema)."""

    extraction: InvoiceExtraction | None
    stop: Stop
    model: str  # the model that actually answered
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: Decimal = Decimal("0")
    refusal_reason: str | None = None


@dataclass
class Check:
    ok: bool
    detail: str


class Adapter(Protocol):
    """One AI service's protocol. Built with the service, its key, model and endpoint."""

    service: Service
    # Output budgets: the first request uses the first; a cut-off answer is asked again with
    # the next. None leaves it to the service.
    budgets: tuple[int | None, ...]

    def limits(self) -> Limits: ...

    def request(self, doc: Prepared, instructions: str, *, budget: int | None) -> Reply:
        """Raises ExtractionError for API failures (with retryable set for rate limits,
        timeouts, 5xx and connection errors)."""
        ...

    def check(self) -> Check:
        """Proves the key and model work, spending no tokens where the service allows."""
        ...

    def list_models(self) -> list[str]:
        """Models this key can use for reading documents, best guess first; [] if unknown."""
        ...


# -- preparing a document ----------------------------------------------------------------


def prepare(
    kind: str,
    file_path: Path,
    page_images: list[Path],
    text: str | None,
    *,
    service: Service,
    limits: Limits,
) -> Prepared:
    match kind:
        case "pdf":
            if service.reads_pdf and file_size(file_path) <= limits.max_pdf_bytes:
                return Prepared(pdf=file_path)
            if not service.reads_images:
                return _text_only(text, service)
            if not page_images:
                raise ExtractionError(
                    f"This PDF is larger than {limits.pdf_label} and its pages could not be "
                    "prepared for reading. Split it into smaller files and upload them again."
                    if service.reads_pdf
                    else "This PDF's pages could not be prepared for reading. Split it into "
                    "smaller files and upload them again.",
                    retryable=False,
                )
            prepared = _pages_as_images(page_images, service, limits)
            if not service.reads_pdf and (text or "").strip():
                # The text layer helps services that only see the page images.
                layer = (
                    "<document_text>\n"
                    f"{text.strip()}\n"
                    "</document_text>\n"
                    "The text above is the PDF's own text layer; it can be incomplete or out "
                    "of order. Where it disagrees with the page images, the images are right."
                )
                prepared.lead = f"{layer}\n\n{prepared.lead}" if prepared.lead else layer
            return prepared
        case "image":
            if not service.reads_images:
                raise ExtractionError(
                    f"{service.short} can only read text, so it can't read photos or scans. "
                    "Choose another AI service in Settings, or enter this document manually.",
                    retryable=False,
                )
            return Prepared(images=page_images or [file_path])
        case "docx":
            if not (text or "").strip():
                raise ExtractionError(
                    "This Word document has no readable text. Save it as a PDF and upload "
                    "that instead.",
                    retryable=False,
                )
            return Prepared(lead=f"<document>\n{text}\n</document>")
    raise ExtractionError("This kind of file can't be read as an invoice.", retryable=False)


def _text_only(text: str | None, service: Service) -> Prepared:
    if not (text or "").strip():
        raise ExtractionError(
            f"This PDF is a scan with no text in it, and {service.short} can only read text. "
            "Choose another AI service in Settings, or enter this document manually.",
            retryable=False,
        )
    return Prepared(lead=f"<document>\n{text}\n</document>")


def _pages_as_images(page_images: list[Path], service: Service, limits: Limits) -> Prepared:
    total = len(page_images)
    if total == 1:
        if encoded_size(page_images[0]) > limits.max_image_payload:
            raise ExtractionError(too_large(service), retryable=False)
        return Prepared(images=list(page_images))
    lead = _leading_pages_that_fit(page_images, service, limits)
    # Totals are usually on the last page, so it is always sent, in place of middle pages.
    images = [*page_images[:lead], page_images[-1]]
    if lead == total - 1:
        return Prepared(images=images)
    return Prepared(images=images, lead=_left_out_notice(lead, total))


def _leading_pages_that_fit(page_images: list[Path], service: Service, limits: Limits) -> int:
    """How many pages from the start can be sent along with the last page, within both the
    page limit and the request size limit."""
    sizes = [encoded_size(p) for p in page_images]
    budget = limits.max_image_payload - sizes[-1]
    limit = min(len(page_images) - 1, limits.max_images - 1)
    lead = 0
    while lead < limit and sizes[lead] <= budget:
        budget -= sizes[lead]
        lead += 1
    # The first page carries the parties and the invoice number, so it is never dropped.
    if budget < 0 or lead == 0:
        raise ExtractionError(too_large(service), retryable=False)
    return lead


def _left_out_notice(lead: int, total: int) -> str:
    sent = "page 1" if lead == 1 else f"pages 1 to {lead}"
    if lead + 1 == total - 1:
        missing = f"Page {lead + 1} is"
    else:
        missing = f"Pages {lead + 1} to {total - 1} are"
    return (
        f"The PDF is too large to send whole, so only page images are attached: {sent} and "
        f"page {total} of {total}, in that order. {missing} left out; say so in notes."
    )


def too_large(service: Service) -> str:
    return (
        f"This document is too large to send to {service.short} in one request. Split it "
        "into smaller files and upload them again, or enter it manually."
    )


# -- files ---------------------------------------------------------------------------------


def encoded_size(path: Path) -> int:
    return 4 * -(-file_size(path) // 3)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise unreadable_file() from exc


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise unreadable_file() from exc


def b64(path: Path) -> str:
    return base64.standard_b64encode(read_bytes(path)).decode("ascii")


def image_media_type(path: Path) -> str:
    return IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "image/png")


def unreadable_file() -> ExtractionError:
    return ExtractionError(
        "The uploaded file could not be read from storage. Upload it again.", retryable=False
    )
