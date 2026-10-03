from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._common import TimestampMixin, UTCDateTime


class DocumentKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    DOCX = "docx"
    SHEET = "sheet"  # xlsx / csv


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"  # stored, waiting for the worker
    PARSING = "parsing"  # claimed by a worker
    PARSED = "parsed"  # text / page images / rows extracted
    FAILED = "failed"  # could not be read; see error
    DUPLICATE = "duplicate"  # identical file already uploaded for this company


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_company_status", "company_id", "status"),
        Index("ix_documents_company_sha", "company_id", "sha256"),
        {"sqlite_autoincrement": True},  # IDs are in the audit trail; never reuse one
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))  # relative to settings.storage_dir
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int]
    kind: Mapped[str] = mapped_column(String(10))

    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.UPLOADED)
    error: Mapped[str | None] = mapped_column(Text)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    attempts: Mapped[int] = mapped_column(default=0)

    # Filled in by the parser
    page_count: Mapped[int] = mapped_column(default=0)
    has_text_layer: Mapped[bool] = mapped_column(default=False)
    text: Mapped[str | None] = mapped_column(Text)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # pages, sheets, warnings
    parsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
