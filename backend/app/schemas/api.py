from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.accounting.types import LedgerChoices
from app.models import UserRole
from app.schemas.invoice import NormalizedInvoice

# Deliberately loose: self-hosted offices often use internal domains (e.g. .local).
LoginEmail = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+$", max_length=255)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# -- auth & users ---------------------------------------------------------------------


class SetupStatus(BaseModel):
    needs_setup: bool


class SetupIn(BaseModel):
    email: LoginEmail
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: LoginEmail
    password: str


class UserCreate(SetupIn):
    role: UserRole = UserRole.PREPARER


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    role: UserRole
    is_active: bool


# -- companies ------------------------------------------------------------------------

_GSTIN = r"^[0-9]{2}[0-9A-Z]{13}$"


class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    external_company_name: str = Field(min_length=1, max_length=255)
    gstin: str | None = Field(default=None, pattern=_GSTIN)
    state: str | None = None
    connector_url: str | None = None
    auto_create_ledgers: bool = False
    always_review: bool = True
    review_above_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    auto_post: bool = False


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    external_company_name: str | None = Field(default=None, min_length=1, max_length=255)
    gstin: str | None = Field(default=None, pattern=_GSTIN)
    state: str | None = None
    connector_url: str | None = None
    auto_create_ledgers: bool | None = None
    always_review: bool | None = None
    review_above_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    auto_post: bool | None = None


class CompanyOut(ORM):
    id: int
    name: str
    external_company_name: str
    gstin: str | None
    state: str | None
    connector_type: str
    connector_url: str | None
    auto_create_ledgers: bool
    always_review: bool
    review_above_amount: Decimal | None
    auto_post: bool
    created_at: datetime


# -- ledgers & connectors -------------------------------------------------------------


class LedgerOut(ORM):
    id: int
    name: str
    parent: str | None
    gstin: str | None
    state: str | None
    aliases: list[str]
    synced_at: datetime


class GroupOut(ORM):
    name: str
    parent: str | None


class ConnectionStatusOut(BaseModel):
    ok: bool
    detail: str
    url: str


class ExternalCompanyOut(BaseModel):
    name: str
    state: str | None = None
    gstin: str | None = None


class SyncResultOut(BaseModel):
    ledgers: int
    groups: int
    added: int
    removed: int


class LedgerCreateResult(BaseModel):
    success: bool
    errors: list[str]


class PostingOutcomeOut(BaseModel):
    success: bool
    ledgers_created: list[str]
    errors: list[str]
    external_id: str | None


class AuditEventOut(ORM):
    id: int
    actor_id: int | None
    action: str
    entity_type: str
    entity_id: str
    company_id: int | None
    data: dict[str, Any]
    created_at: datetime


# -- documents ------------------------------------------------------------------------


class DocumentOut(ORM):
    id: int
    company_id: int
    original_filename: str
    mime_type: str
    kind: str
    size_bytes: int
    status: str
    error: str | None
    duplicate_of_id: int | None
    page_count: int
    has_text_layer: bool
    uploaded_by: int | None
    uploader_name: str | None = None
    created_at: datetime
    parsed_at: datetime | None
    voucher_id: int | None = None
    voucher_status: str | None = None
    party_name: str | None = None
    invoice_number: str | None = None
    grand_total: float | None = None


class DocumentDetail(DocumentOut):
    sha256: str
    text: str | None
    parsed: dict[str, Any]


class RejectedUpload(BaseModel):
    filename: str
    reason: str


class UploadResult(BaseModel):
    documents: list[DocumentOut]
    rejected: list[RejectedUpload]


# -- vouchers -------------------------------------------------------------------------


class VoucherOut(ORM):
    id: int
    company_id: int
    document_id: int
    voucher_uid: str
    status: str
    source: str
    extraction_error: str | None
    model: str | None
    cost_usd: float
    invoice: dict[str, Any]
    choices: dict[str, Any]
    accounting: dict[str, Any] | None
    issues: list[dict[str, Any]]
    confidence: float
    invoice_number: str | None
    party_name: str | None
    voucher_kind: str | None
    grand_total: float | None
    posted_at: datetime | None
    external_id: str | None
    post_error: str | None
    updated_at: datetime


class VoucherUpdate(BaseModel):
    invoice: NormalizedInvoice
    choices: LedgerChoices


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class BulkPostResult(BaseModel):
    posted: list[int]
    failed: list[dict[str, Any]]


# -- users & passwords ----------------------------------------------------------------


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: UserRole | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    """An administrator sets a new password for someone else."""

    password: str = Field(min_length=8, max_length=128)


class PasswordChange(BaseModel):
    """A user changes their own password."""

    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


# -- office settings ------------------------------------------------------------------


class AiServiceOut(BaseModel):
    """One AI service the office can read documents with."""

    id: str
    name: str
    key_name: str  # "Gemini API key"
    key_help: str
    key_optional: bool  # a local server may need no key
    custom_base_url: bool  # the office enters the service's address
    supports_effort: bool
    reads_images: bool  # False: text only, scans can't be read
    configured: bool  # ready to use: a key (if needed), a model, and an address (if needed)
    source: Literal["settings", "env"] | None  # where the key comes from
    key_hint: str | None
    model: str
    models: list[str]  # suggestions; any model name the service knows is accepted
    base_url: str | None


class AiSettingsOut(BaseModel):
    """configured, source, key_hint, model and models describe the service in use."""

    provider: str  # id of the service in use
    configured: bool  # ready to read: a key (if needed), a model, and an address (if needed)
    source: Literal["settings", "env"] | None
    key_hint: str | None  # e.g. "sk-ant-…4f2a"; never the key itself
    model: str
    effort: str  # Claude only
    models: list[str]  # choices offered on the settings screen
    efforts: list[str]
    services: list[AiServiceOut]


class AiModelsOut(BaseModel):
    models: list[str]
    detail: str | None = None  # why the list could not be fetched


class SettingsOut(BaseModel):
    tally_url: str
    tally_url_source: Literal["settings", "env"]
    ai: AiSettingsOut
    requeued: int = 0  # documents queued for reading by this change (PUT only)


class SettingsUpdate(BaseModel):
    """Only the fields sent are changed. anthropic_api_key "" removes the saved key.

    ai_api_key, ai_model and ai_base_url belong to the service named in ai_provider, or to
    the service in use when ai_provider is not sent; an ai_api_key sent without ai_provider
    goes to the service its format points to (else the one in use). Saving a key makes its
    service the one in use, and so does ai_provider sent alone; with a model, an address or
    a key removal, ai_provider only names their service and the service in use stays.
    "" resets: ai_provider to the automatic choice, ai_api_key removes the saved key,
    ai_model goes back to the service's default model, ai_base_url removes the "Other"
    service's address (it is fixed for every other service).
    """

    tally_url: str | None = Field(default=None, pattern=r"^https?://[^\s/]+(:\d+)?/?$")
    anthropic_api_key: str | None = Field(default=None, max_length=500)
    claude_model: str | None = None
    claude_effort: str | None = None
    ai_provider: str | None = Field(default=None, max_length=40)
    ai_api_key: str | None = Field(default=None, max_length=500)
    ai_model: str | None = Field(default=None, max_length=200)
    ai_base_url: str | None = Field(default=None, max_length=300)


class CheckResult(BaseModel):
    ok: bool
    detail: str


# -- backups --------------------------------------------------------------------------


class BackupOut(BaseModel):
    name: str  # file name inside the backup folder
    size_bytes: int
    created_at: datetime
    reason: Literal["scheduled", "manual", "before_migration"]


# -- activity & history ---------------------------------------------------------------


class ActivityItem(BaseModel):
    id: int
    created_at: datetime
    actor_id: int | None
    actor_name: str | None  # None means the system (worker, scheduled job)
    action: str
    entity_type: str
    entity_id: str
    company_id: int | None
    summary: str  # one plain sentence, e.g. 'Dev Admin posted SE/2026/0042 to Tally'
    document_id: int | None = None  # for linking to the document page, when known
    data: dict[str, Any]


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    next_before_id: int | None  # pass as before_id to get the next (older) page


class FieldChange(BaseModel):
    field: str  # path, e.g. "grand_total"
    label: str  # e.g. "Grand total"
    before: Any
    after: Any


class PostingRecord(BaseModel):
    id: int
    kind: str  # "ledger" | "voucher"
    reference: str
    success: bool
    error: str | None
    request_payload: str
    response_payload: str | None
    created_at: datetime


class HistoryItem(BaseModel):
    at: datetime
    actor_name: str | None
    action: str
    summary: str
    changes: list[FieldChange] = []
    posting: PostingRecord | None = None


class VoucherHistory(BaseModel):
    voucher_id: int
    document_id: int
    items: list[HistoryItem]  # oldest first
