from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import TimestampMixin


class Company(TimestampMixin, Base):
    """A client company the CA office keeps books for. Maps to one company in the
    connected accounting system (e.g. a Tally company)."""

    __tablename__ = "companies"
    # IDs are referenced by the audit trail, so SQLite must never reuse one.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    gstin: Mapped[str | None] = mapped_column(String(15))
    state: Mapped[str | None] = mapped_column(String(100))

    connector_type: Mapped[str] = mapped_column(String(30), default="tally")
    connector_url: Mapped[str | None] = mapped_column(String(255))  # None -> settings default
    external_company_name: Mapped[str] = mapped_column(String(255))  # e.g. Tally company name

    # Policies (PDF §10/§11)
    auto_create_ledgers: Mapped[bool] = mapped_column(default=False)
    always_review: Mapped[bool] = mapped_column(default=True)
    # Entries with a grand total above this always go to review, whatever else is true.
    review_above_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Post entries that pass every check to Tally without waiting for a click.
    auto_post: Mapped[bool] = mapped_column(default=False)
