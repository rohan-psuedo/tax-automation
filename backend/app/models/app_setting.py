from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import UTCDateTime, utcnow


class AppSetting(Base):
    """Office-wide settings changed from the Settings screen. A row overrides the matching
    environment setting; no row means the environment (or built-in default) applies.

    Known keys: "tally_url", "anthropic_api_key" (stored encrypted, see app.security_box),
    "claude_model", "claude_effort".
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
