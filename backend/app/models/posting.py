from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import TimestampMixin


class PostingAttempt(TimestampMixin, Base):
    """Every request sent to an accounting system, with its raw response."""

    __tablename__ = "posting_attempts"
    # IDs are referenced by the audit trail, so SQLite must never reuse one.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))  # "ledger" | "voucher"
    reference: Mapped[str] = mapped_column(String(255), index=True)  # tx id / ledger name
    request_payload: Mapped[str] = mapped_column(Text)
    response_payload: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
