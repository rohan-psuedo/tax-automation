from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import TimestampMixin


class AuditEvent(TimestampMixin, Base):
    """Append-only audit trail. Rows are never updated or deleted."""

    __tablename__ = "audit_events"
    # Event IDs must stay unique forever, so SQLite must never reuse one.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # None = system
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)  # e.g. a ledger name
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
