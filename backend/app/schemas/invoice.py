"""The cleaned-up invoice: typed values parsed from an InvoiceExtraction (or typed in by a
reviewer). This is what the review screen edits, and what the accounting engine and the
validation engine read. Field confidence is 0..1, keyed by field path (e.g. "grand_total",
"seller.gstin"); fields a person entered or corrected get 1.0.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field

DocumentType = Literal[
    "tax_invoice", "bill_of_supply", "credit_note", "debit_note", "receipt", "proforma", "other"
]


# 0..1 and finite: a NaN would compare as neither low nor high, and can't be sent as JSON.
Confidence = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class Party(BaseModel):
    name: str | None = None
    gstin: str | None = None  # upper-cased, spaces removed
    gstin_valid: bool = False  # format + checksum
    state_code: str | None = None  # 2-digit GST state code, from GSTIN or state name
    state: str | None = None  # canonical state name for state_code
    address: str | None = None


class InvoiceLine(BaseModel):
    description: str = ""
    hsn_sac: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    rate: Decimal | None = None
    taxable_value: Decimal | None = None
    gst_rate: Decimal | None = None  # total GST percent, e.g. 18


class NormalizedInvoice(BaseModel):
    is_invoice: bool = True
    document_type: DocumentType = "tax_invoice"
    invoice_number: str | None = None
    invoice_date: date | None = None
    seller: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    place_of_supply_code: str | None = None
    reverse_charge: bool = False
    lines: list[InvoiceLine] = Field(default_factory=list)

    taxable_value: Decimal | None = None
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")  # SGST or UTGST
    igst: Decimal = Decimal("0")
    cess: Decimal = Decimal("0")
    round_off: Decimal = Decimal("0")  # signed
    grand_total: Decimal | None = None

    confidence: dict[str, Confidence] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def total_tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst + self.cess
