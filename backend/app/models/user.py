from enum import StrEnum

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import TimestampMixin


class UserRole(StrEnum):
    ADMIN = "admin"  # settings, users, auto-create policy
    REVIEWER = "reviewer"  # approve and post
    PREPARER = "preparer"  # upload and edit


class User(TimestampMixin, Base):
    __tablename__ = "users"
    # IDs are referenced by the audit trail, so SQLite must never reuse one.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.PREPARER)
    is_active: Mapped[bool] = mapped_column(default=True)
    # Part of every login token. Bumping it (password reset or change, deactivation) ends
    # all sessions issued before.
    session_version: Mapped[int] = mapped_column(default=0, server_default="0")
