"""Turns raw extracted strings into typed, checked values.

CONTRACT (implementation pending), modules:

gst.py
    STATE_CODES: dict[str, str]                 # "29" -> "Karnataka" (all GST state/UT codes)
    is_valid_gstin(gstin: str | None) -> bool   # 15-char format + mod-36 checksum
    clean_gstin(raw: str | None) -> str | None  # upper-case, strip spaces/dashes; None if empty
    state_code_from_gstin(gstin: str | None) -> str | None
    state_code_from_name(name: str | None) -> str | None  # tolerant: "Delhi", "NCT of Delhi",
                                                          # "Orissa", "29-Karnataka", etc.
    GST_RATES: frozenset[Decimal]               # valid total GST rates in percent

values.py
    parse_amount(raw: str | None) -> Decimal | None   # "₹1,23,456.50", "Rs. 500/-", "(100)"
    parse_date(raw: str | None) -> date | None        # ISO, DD/MM/YYYY, DD-MM-YY, 28-Sep-2026

normalize.py
    CONFIDENCE: dict[str, float]  # {"high": 0.95, "medium": 0.7, "low": 0.4}
    normalize(extraction: InvoiceExtraction) -> NormalizedInvoice
"""
