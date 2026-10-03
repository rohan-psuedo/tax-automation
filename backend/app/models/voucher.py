from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import TimestampMixin, UTCDateTime, utcnow


class VoucherStatus(StrEnum):
    PENDING = "pending"  # waiting for AI extraction
    EXTRACTING = "extracting"  # claimed by the worker
    NEEDS_REVIEW = "needs_review"  # has issues, or the company reviews everything
    READY = "ready"  # no issues; can be posted
    POSTING = "posting"  # being sent to the accounting system
    POSTED = "posted"
    POST_FAILED = "post_failed"
    REJECTED = "rejected"

    @classmethod
    def editable(cls) -> set["VoucherStatus"]:
        return {cls.NEEDS_REVIEW, cls.READY, cls.POST_FAILED}


class Voucher(TimestampMixin, Base):
    """The accounting entry proposed for one document, from extraction to posting."""

    __tablename__ = "vouchers"
    __table_args__ = (
        Index("ix_vouchers_company_status", "company_id", "status"),
        Index("ix_vouchers_company_invoice", "company_id", "invoice_number"),
        {"sqlite_autoincrement": True},  # ids are in the audit trail; never reuse one
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True
    )
    # Stable id of the CanonicalTransaction: posting is idempotent on it (Tally REMOTEID).
    voucher_uid: Mapped[str] = mapped_column(String(36), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=VoucherStatus.PENDING)
    source: Mapped[str] = mapped_column(String(10), default="ai")  # "ai" | "manual"

    # AI extraction
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Why reading failed, for retrying it without a person: "settings" (fixed by changing the
    # AI settings), "temporary" (the service was busy or unreachable), "document" (this file).
    extraction_error_kind: Mapped[str | None] = mapped_column(String(20))
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # retry not before
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0"))

    # Review state (JSON dumps of NormalizedInvoice, LedgerChoices, AccountingResult, [Issue])
    invoice: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    choices: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    accounting: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(default=0.0)

    # Denormalised for lists and duplicate checks
    invoice_number: Mapped[str | None] = mapped_column(String(100))
    party_name: Mapped[str | None] = mapped_column(String(255))
    party_gstin: Mapped[str | None] = mapped_column(String(15))
    voucher_kind: Mapped[str | None] = mapped_column(String(20))
    grand_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    # Posting
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    external_id: Mapped[str | None] = mapped_column(String(100))
    post_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class LedgerMapping(Base):
    """Learned from approved postings: which item ledger a party's invoices go to."""

    __tablename__ = "ledger_mappings"
    __table_args__ = (UniqueConstraint("company_id", "party_ledger"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    party_ledger: Mapped[str] = mapped_column(String(255))
    item_ledger: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
