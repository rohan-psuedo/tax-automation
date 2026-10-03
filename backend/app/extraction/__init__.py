"""Reads invoices with Claude.

CONTRACT (implementation pending), extractor.py:

class ExtractionError(Exception):
    message: str        # shown to the user
    retryable: bool     # True for rate limits, timeouts, 5xx, connection errors

class ExtractionNotConfigured(ExtractionError)   # no ANTHROPIC_API_KEY; retryable=False

@dataclass
class ExtractionOutcome:
    extraction: InvoiceExtraction
    model: str               # model that actually served the request (fallbacks may differ)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float

def extract_invoice(
    *,
    kind: str,                    # DocumentKind value: "pdf" | "image" | "docx"
    file_path: Path,              # original file (sent as a PDF document block for PDFs)
    page_images: list[Path],      # rendered pages (sent as image blocks for images)
    text: str | None,             # text layer / docx text (sent as text for docx)
    company_name: str,
    company_gstin: str | None,
    client=None,                  # anthropic.Anthropic-compatible; None -> built from settings
) -> ExtractionOutcome
"""
