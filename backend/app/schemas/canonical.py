"""System-agnostic accounting model.

Everything upstream of a connector (extraction, accounting engine, validation, review)
works only with these types. Connectors translate them into their own wire format.
"""

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, model_validator

CENT = Decimal("0.01")


def _money(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


Money = Annotated[Decimal, AfterValidator(_money)]


class Side(StrEnum):
    DR = "dr"
    CR = "cr"


class VoucherKind(StrEnum):
    PURCHASE = "purchase"
    SALES = "sales"
    PAYMENT = "payment"
    RECEIPT = "receipt"
    JOURNAL = "journal"
    CONTRA = "contra"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"


class ProposedLedger(BaseModel):
    """A ledger that does not exist yet in the accounting system."""

    name: str = Field(min_length=1, max_length=255)
    parent_group: str = Field(min_length=1)
    gstin: str | None = Field(default=None, pattern=r"^[0-9A-Z]{15}$")
    gst_registration_type: str | None = None  # Regular / Composition / Unregistered / Consumer
    state: str | None = None
    country: str = "India"
    address_lines: list[str] = Field(default_factory=list)
    pincode: str | None = None
    bill_wise: bool = False


class LedgerRef(BaseModel):
    """Points at an existing ledger by name, or carries a ledger to be created first."""

    name: str = Field(min_length=1)
    proposed: ProposedLedger | None = None

    @model_validator(mode="after")
    def _names_agree(self) -> "LedgerRef":
        if self.proposed and self.proposed.name != self.name:
            raise ValueError("proposed ledger name must equal ref name")
        return self


class GstDetails(BaseModel):
    """Who the voucher is with and where the supply happens, for GST returns built from the
    books. State names are the canonical ones from app.normalization.gst."""

    party_gstin: str | None = Field(default=None, pattern=r"^[0-9A-Z]{15}$")
    party_registration_type: str | None = None  # Regular / Unregistered
    party_state: str | None = None
    place_of_supply: str | None = None
    company_gstin: str | None = Field(default=None, pattern=r"^[0-9A-Z]{15}$")


class Entry(BaseModel):
    ledger: LedgerRef
    side: Side
    amount: Money = Field(gt=0)
    is_party: bool = False
    bill_ref: str | None = None  # bill-wise reference (usually the invoice number)


class CanonicalTransaction(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    voucher_kind: VoucherKind
    date: date
    reference_no: str | None = None  # supplier/customer invoice number
    reference_date: date | None = None
    voucher_number: str | None = None
    narration: str | None = None
    gst: GstDetails | None = None
    entries: list[Entry] = Field(min_length=2)

    @model_validator(mode="after")
    def _balanced(self) -> "CanonicalTransaction":
        dr, cr = self.total(Side.DR), self.total(Side.CR)
        if dr != cr:
            raise ValueError(f"transaction does not balance: Dr {dr} != Cr {cr}")
        if sum(1 for e in self.entries if e.is_party) > 1:
            raise ValueError("at most one party entry is allowed")
        return self

    def total(self, side: Side) -> Decimal:
        return sum((e.amount for e in self.entries if e.side == side), Decimal("0.00"))

    @property
    def party_entry(self) -> Entry | None:
        return next((e for e in self.entries if e.is_party), None)

    def ledgers_to_create(self) -> list[ProposedLedger]:
        seen: dict[str, ProposedLedger] = {}
        for e in self.entries:
            if e.ledger.proposed and e.ledger.name not in seen:
                seen[e.ledger.name] = e.ledger.proposed
        return list(seen.values())
