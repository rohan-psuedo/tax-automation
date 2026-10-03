from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import UTCDateTime, utcnow


class Ledger(Base):
    """Local cache of ledger masters from the accounting system, used for matching."""

    __tablename__ = "ledgers"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    parent: Mapped[str | None] = mapped_column(String(255))
    gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    state: Mapped[str | None] = mapped_column(String(100))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    external_id: Mapped[str | None] = mapped_column(String(100))  # Tally GUID
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LedgerGroup(Base):
    __tablename__ = "ledger_groups"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    parent: Mapped[str | None] = mapped_column(String(255))
