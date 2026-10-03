"""What Claude returns when it reads an invoice (structured output schema).

Kept deliberately simple for the model: amounts and dates are strings exactly as the
model read them, so nothing is lost to float rounding or a wrong date format. All
interpretation (Decimal parsing, DD/MM dates, GSTIN checks) happens in app.normalization.

Structured outputs don't support numeric/length constraints, so none are used here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # -> additionalProperties: false


class ExtractedValue(_Strict):
    """One field read from the document."""

    value: str | None = Field(
        description=(
            "The value, or null if it is not on the document. Amounts: plain number with "
            "a dot for decimals and no currency symbol or thousands separators "
            "(e.g. 123456.50). Dates: YYYY-MM-DD (Indian invoices print day first)."
        )
    )
    confidence: Confidence = Field(
        description="high: printed clearly; medium: partly legible or inferred; low: a guess."
    )
    source_text: str | None = Field(
        default=None, description="The text exactly as printed, for a reviewer to find it."
    )
    page: int | None = Field(default=None, description="1-based page where it appears.")


class ExtractedParty(_Strict):
    name: ExtractedValue
    gstin: ExtractedValue = Field(description="15-character GSTIN, or null if not printed.")
    address: str | None = None
    state: str | None = Field(default=None, description="State name as printed, if any.")


class ExtractedLineItem(_Strict):
    description: str
    hsn_sac: str | None = None
    quantity: str | None = None
    unit: str | None = None
    rate: str | None = None
    discount: str | None = None
    taxable_value: str | None = Field(
        default=None, description="Line amount after discount, before tax."
    )
    gst_rate: str | None = Field(
        default=None, description="Total GST rate in percent for this line, e.g. 18."
    )


class ExtractedTotals(_Strict):
    taxable_value: ExtractedValue = Field(description="Total taxable value before tax.")
    cgst: ExtractedValue
    sgst: ExtractedValue = Field(description="SGST or UTGST amount.")
    igst: ExtractedValue
    cess: ExtractedValue
    round_off: ExtractedValue = Field(description="Signed: negative when rounded down.")
    grand_total: ExtractedValue = Field(description="Final amount payable.")


class InvoiceExtraction(_Strict):
    is_invoice: bool = Field(
        description="False if this is not a bill/invoice/credit or debit note (e.g. a letter)."
    )
    document_type: Literal[
        "tax_invoice",
        "bill_of_supply",
        "credit_note",
        "debit_note",
        "receipt",
        "proforma",
        "other",
    ]
    invoice_number: ExtractedValue
    invoice_date: ExtractedValue
    seller: ExtractedParty = Field(description="The supplier who issued the document.")
    buyer: ExtractedParty = Field(description="The recipient (billed-to party).")
    place_of_supply: str | None = Field(
        default=None, description="Place of supply state as printed, if any."
    )
    reverse_charge: bool | None = None
    line_items: list[ExtractedLineItem]
    totals: ExtractedTotals
    notes: list[str] = Field(
        default_factory=list,
        description="Anything a reviewer should know: illegible parts, conflicting totals.",
    )
