"""The voucher pipeline for one document:

    read document -> AI extraction -> NormalizedInvoice -> accounting engine -> validation
    -> needs_review / ready -> (reviewer edits) -> post to the accounting system

Accounting and validation are pure functions, so `evaluate` re-runs them whenever the
invoice, the reviewer's ledger choices or the company's ledgers change.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import audit
from app.accounting import AccountingContext, AccountingResult, LedgerChoices, LedgerInfo
from app.accounting import build_voucher as build_accounting
from app.connectors.base import AccountingConnector, ConnectorError
from app.ingestion import storage
from app.models import (
    AuditEvent,
    Company,
    Document,
    DocumentKind,
    DocumentStatus,
    Ledger,
    LedgerMapping,
    User,
    Voucher,
    VoucherStatus,
)
from app.models._common import utcnow
from app.normalization.gst import is_valid_gstin, state_code_from_gstin, state_code_from_name
from app.normalization.normalize import normalize, refresh_derived
from app.schemas.invoice import NormalizedInvoice
from app.services import posting
from app.services.connectors import ConnectorFactory, connector_url, default_connector_factory
from app.validation import Issue, ValidationReport, validate
from app.validation.engine import format_inr

log = logging.getLogger(__name__)

EXTRACTABLE_KINDS = {DocumentKind.PDF, DocumentKind.IMAGE, DocumentKind.DOCX}
MAX_EXTRACTION_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=15)

# Invoice fields whose confidence is shown and checked; a person's edit sets them to 1.0.
KEY_FIELDS = (
    "invoice_number",
    "invoice_date",
    "seller.name",
    "seller.gstin",
    "buyer.name",
    "buyer.gstin",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "cess",
    "round_off",
    "grand_total",
)


class VoucherError(Exception):
    """An action isn't allowed in the voucher's current state. Shown to the user."""


class VoucherBlocked(VoucherError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("Fix these before posting: " + " ".join(messages))
        self.messages = messages


# -- creation & evaluation -------------------------------------------------------------


def create_for_document(db: Session, doc: Document) -> Voucher | None:
    """Queues AI extraction for a document that was just read. Spreadsheets are skipped:
    they hold many entries and are handled by bank/register import (later phase)."""
    if doc.kind not in EXTRACTABLE_KINDS:
        return None
    existing = db.scalar(select(Voucher).where(Voucher.document_id == doc.id))
    if existing:
        return existing
    voucher = Voucher(
        company_id=doc.company_id,
        document_id=doc.id,
        voucher_uid=str(uuid.uuid4()),
        status=VoucherStatus.PENDING,
        invoice=NormalizedInvoice().model_dump(mode="json"),
        choices=LedgerChoices().model_dump(mode="json"),
    )
    db.add(voucher)
    return voucher


def queue_missing(db: Session) -> int:
    """Queues vouchers for documents read before vouchers existed (an upgrade), or whose
    voucher was never created. Idempotent; run at worker start."""
    has_voucher = select(Voucher.document_id)
    docs = db.scalars(
        select(Document).where(
            Document.status == DocumentStatus.PARSED,
            Document.kind.in_(EXTRACTABLE_KINDS),
            Document.id.not_in(has_voucher),
        )
    )
    created = [create_for_document(db, doc) for doc in docs]
    db.commit()
    return len(created)


def accounting_context(db: Session, company: Company) -> AccountingContext:
    ledgers = tuple(
        LedgerInfo(
            name=led.name, parent=led.parent, gstin=led.gstin, aliases=tuple(led.aliases or ())
        )
        for led in db.scalars(select(Ledger).where(Ledger.company_id == company.id))
    )
    learned = dict(
        db.execute(
            select(LedgerMapping.party_ledger, LedgerMapping.item_ledger).where(
                LedgerMapping.company_id == company.id
            )
        ).all()
    )
    return AccountingContext(
        company_name=company.external_company_name,
        company_gstin=company.gstin,
        company_state_code=state_code_from_gstin(company.gstin)
        or state_code_from_name(company.state),
        ledgers=ledgers,
        learned_item_ledgers=learned,
    )


_NOTES = {"credit_note", "debit_note"}


def _financial_year(d: date | None) -> int | None:
    """Indian financial year (April-March), named by its starting year."""
    return None if d is None else (d.year if d.month >= 4 else d.year - 1)


def _invoice_date(data: dict) -> date | None:
    try:
        return date.fromisoformat(data.get("invoice_date") or "")
    except ValueError:
        return None


def _duplicate_lookup(db: Session, voucher: Voucher, invoice: NormalizedInvoice, direction: str):
    """Invoice numbers restart every financial year (Rule 46) and notes have their own
    series, so a number only repeats within one year and one series. A company's own sales
    numbers are unique across all its customers; a supplier's only for that supplier."""
    year = _financial_year(invoice.invoice_date)
    is_note = invoice.document_type in _NOTES

    def find(gstin: str | None, name: str | None, number: str) -> list[str]:
        others = db.scalars(
            select(Voucher).where(
                Voucher.company_id == voucher.company_id,
                Voucher.id != voucher.id,
                Voucher.status != VoucherStatus.REJECTED,
                func.lower(Voucher.invoice_number) == number.strip().lower(),
            )
        )
        valid_gstin = gstin if is_valid_gstin(gstin) else None
        found = []
        for other in others:
            other_invoice = other.invoice or {}
            other_direction = (other.accounting or {}).get("direction")
            if other_direction != direction:
                continue
            if (other_invoice.get("document_type") in _NOTES) != is_note:
                continue
            other_year = _financial_year(_invoice_date(other_invoice))
            if year is not None and other_year is not None and other_year != year:
                continue
            if direction == "sales":
                same_party = True  # our own series: the number alone identifies the invoice
            elif valid_gstin and other.party_gstin:
                same_party = other.party_gstin == valid_gstin
            else:
                same_party = (other.party_name or "").casefold() == (name or "").casefold()
            if same_party:
                state = "already posted" if other.status == VoucherStatus.POSTED else "in review"
                found.append(f"document #{other.document_id} ({state})")
        return found

    return find


def _with_review_limit(
    report: ValidationReport, invoice: NormalizedInvoice, company: Company
) -> ValidationReport:
    limit, total = company.review_above_amount, invoice.grand_total
    if limit is None or total is None:
        return report
    if any(i.code == "not_invoice" for i in report.issues):
        return report  # as in validate: nothing else is said until it is confirmed an invoice
    # Notes and some invoices print minus signs; Tally is sent the amount either way.
    if total.copy_abs() <= limit:
        return report
    issue = Issue(
        code="above_review_limit",
        severity="warning",
        message=f"Entries above {format_inr(limit)} are always reviewed for this company.",
        field="grand_total",
    )
    return report.model_copy(update={"issues": [*report.issues, issue], "route": "needs_review"})


# A failed post is not among them: it keeps its status until a person edits it.
_ROUTED = frozenset({VoucherStatus.EXTRACTING, VoucherStatus.NEEDS_REVIEW, VoucherStatus.READY})


def _route(voucher: Voucher, report: ValidationReport) -> None:
    if voucher.status in _ROUTED:
        voucher.status = (
            VoucherStatus.READY if report.route == "ready" else VoucherStatus.NEEDS_REVIEW
        )


def evaluate(db: Session, voucher: Voucher, company: Company) -> ValidationReport:
    """Re-runs the accounting engine and validation, and updates status (when open)."""
    invoice = NormalizedInvoice.model_validate(voucher.invoice or {})
    choices = LedgerChoices.model_validate(voucher.choices or {})
    result = build_accounting(
        invoice,
        accounting_context(db, company),
        choices,
        voucher_id=uuid.UUID(voucher.voucher_uid),
    )
    report = validate(
        invoice,
        result,
        always_review=company.always_review,
        auto_create_ledgers=company.auto_create_ledgers,
        find_duplicates=_duplicate_lookup(db, voucher, invoice, result.direction),
        today=date.today(),
    )
    report = _with_review_limit(report, invoice, company)
    party = invoice.buyer if result.direction == "sales" else invoice.seller
    voucher.accounting = result.model_dump(mode="json")
    voucher.issues = [issue.model_dump(mode="json") for issue in report.issues]
    voucher.confidence = report.confidence
    voucher.invoice_number = (invoice.invoice_number or "").strip() or None
    voucher.party_name = party.name or result.party.ledger
    voucher.party_gstin = party.gstin if party.gstin_valid else None
    voucher.voucher_kind = result.voucher_kind
    voucher.grand_total = invoice.grand_total
    _route(voucher, report)
    return report


def reevaluate_open(db: Session, company: Company) -> int:
    """After ledgers change (sync, a ledger created by posting), refresh every open voucher
    so matches and 'ledger missing' issues reflect the current masters."""
    vouchers = list(
        db.scalars(
            select(Voucher).where(
                Voucher.company_id == company.id,
                Voucher.status.in_(VoucherStatus.editable()),
            )
        )
    )
    for voucher in vouchers:
        evaluate(db, voucher, company)
    db.commit()
    return len(vouchers)


# -- AI extraction (run by the worker) -------------------------------------------------


def run_extraction(db: Session, voucher: Voucher) -> None:
    from app.extraction.extractor import ExtractionError, ExtractionNotConfigured, extract_invoice

    doc = db.get(Document, voucher.document_id)
    company = db.get(Company, voucher.company_id)
    if doc is None or company is None:
        return
    pages = [storage.page_image(doc.stored_path, p["number"]) for p in doc.parsed.get("pages", [])]
    try:
        outcome = extract_invoice(
            kind=doc.kind,
            file_path=storage.absolute(doc.stored_path),
            page_images=pages,
            text=doc.text,
            company_name=company.external_company_name,
            company_gstin=company.gstin,
        )
    except ExtractionError as exc:
        message = getattr(exc, "message", None) or str(exc)
        voucher.extraction_error = message
        if getattr(exc, "retryable", False) and voucher.attempts < MAX_EXTRACTION_ATTEMPTS:
            voucher.status = VoucherStatus.PENDING
            db.commit()
            return
        # Not recoverable automatically: hand over to a person to type it in.
        voucher.source = "manual"
        evaluate(db, voucher, company)
        audit.record(
            db,
            action="voucher.extraction_skipped"
            if isinstance(exc, ExtractionNotConfigured)
            else "voucher.extraction_failed",
            entity_type="voucher",
            entity_id=voucher.id,
            company_id=voucher.company_id,
            data={"error": message, "attempt": voucher.attempts},
        )
        db.commit()
        return

    invoice = normalize(outcome.extraction)
    voucher.extraction = outcome.extraction.model_dump(mode="json")
    voucher.invoice = invoice.model_dump(mode="json")
    voucher.extraction_error = None
    voucher.source = "ai"
    voucher.model = outcome.model
    voucher.input_tokens = outcome.input_tokens
    voucher.output_tokens = outcome.output_tokens
    voucher.cost_usd = Decimal(str(round(outcome.cost_usd, 4)))
    report = evaluate(db, voucher, company)
    audit.record(
        db,
        action="voucher.extracted",
        entity_type="voucher",
        entity_id=voucher.id,
        company_id=voucher.company_id,
        data={
            "model": outcome.model,
            "tokens": [outcome.input_tokens, outcome.output_tokens],
            "cost_usd": outcome.cost_usd,
            "route": report.route,
            "issues": [i.code for i in report.issues],
        },
    )
    db.commit()


def requeue_stale(db: Session) -> int:
    """Recovers vouchers whose worker or request died mid-way (e.g. a server restart)."""
    cutoff = utcnow() - STALE_AFTER
    extracting = db.execute(
        update(Voucher)
        .where(Voucher.status == VoucherStatus.EXTRACTING, Voucher.claimed_at < cutoff)
        .values(status=VoucherStatus.PENDING)
    )
    posting_ = db.execute(
        update(Voucher)
        .where(Voucher.status == VoucherStatus.POSTING, Voucher.updated_at < cutoff)
        .values(
            status=VoucherStatus.POST_FAILED,
            post_error=(
                "Posting was interrupted. Check in Tally whether this voucher arrived before "
                "posting it again."
            ),
        )
    )
    db.commit()
    return extracting.rowcount + posting_.rowcount


def process_pending(db: Session, limit: int = 1) -> int:
    ids = list(
        db.scalars(
            select(Voucher.id)
            .where(Voucher.status == VoucherStatus.PENDING)
            .order_by(Voucher.id)
            .limit(limit)
        )
    )
    done = 0
    for voucher_id in ids:
        claimed = db.execute(
            update(Voucher)
            .where(Voucher.id == voucher_id, Voucher.status == VoucherStatus.PENDING)
            .values(
                status=VoucherStatus.EXTRACTING,
                claimed_at=utcnow(),
                attempts=Voucher.attempts + 1,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            continue
        voucher = db.get(Voucher, voucher_id, populate_existing=True)
        if voucher is not None:
            run_extraction(db, voucher)
            done += 1
    return done


# -- reviewer actions -----------------------------------------------------------------


def _get_path(data: dict, path: str):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _same(a, b) -> bool:
    """Equal as values: "0" and "0.00" are the same amount, so not an edit."""
    if a == b:
        return True
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (ArithmeticError, ValueError):
        return False


def _describe(status: str | None) -> str:
    if status is None:
        return "deleted"
    return "being posted" if status == VoucherStatus.POSTING else status.replace("_", " ")


def _lock_for_edit(db: Session, voucher: Voucher) -> None:
    """Takes the database's write lock and re-reads the voucher, so an edit can't land on
    one that has started posting: Tally would get the old amounts while the app shows the
    new ones. Posting claims the voucher under the same lock."""
    locked = db.execute(
        update(Voucher)
        .where(Voucher.id == voucher.id, Voucher.status.in_(VoucherStatus.editable()))
        .values(updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if locked.rowcount != 1:
        status = db.scalar(select(Voucher.status).where(Voucher.id == voucher.id))
        db.rollback()
        raise VoucherError(f"This voucher is {_describe(status)} and can't be edited.")
    db.refresh(voucher)


def update_voucher(
    db: Session,
    voucher: Voucher,
    company: Company,
    invoice: NormalizedInvoice,
    choices: LedgerChoices,
    user: User,
) -> ValidationReport:
    if voucher.status not in VoucherStatus.editable():
        raise VoucherError(f"This voucher is {_describe(voucher.status)} and can't be edited.")
    _lock_for_edit(db, voucher)
    invoice = refresh_derived(invoice)
    old = voucher.invoice or {}
    new = invoice.model_dump(mode="json")
    changed = [path for path in KEY_FIELDS if not _same(_get_path(old, path), _get_path(new, path))]
    confidence = dict(invoice.confidence)
    for path in changed:
        confidence[path] = 1.0  # a person typed or confirmed it
    invoice = invoice.model_copy(update={"confidence": confidence})
    voucher.invoice = invoice.model_dump(mode="json")
    old_choices = voucher.choices
    voucher.choices = choices.model_dump(mode="json")
    if voucher.status == VoucherStatus.POST_FAILED:
        voucher.status = VoucherStatus.NEEDS_REVIEW
    report = evaluate(db, voucher, company)
    audit.record(
        db,
        action="voucher.edited",
        entity_type="voucher",
        entity_id=voucher.id,
        company_id=voucher.company_id,
        actor_id=user.id,
        data={
            "changed_fields": changed,
            "changes": {p: [_get_path(old, p), _get_path(new, p)] for p in changed},
            "choices": [old_choices, voucher.choices] if old_choices != voucher.choices else None,
        },
    )
    db.commit()
    return report


@dataclass
class PostOutcome:
    voucher: Voucher
    success: bool
    errors: list[str] = field(default_factory=list)
    # Tally could not be reached or gave no usable answer, rather than refusing this voucher.
    unreachable: bool = False


def _learn(db: Session, company: Company, result: AccountingResult) -> None:
    tx = result.transaction
    party = tx.party_entry if tx else None
    if party is None or not result.item.ledger:
        return
    mapping = db.scalar(
        select(LedgerMapping).where(
            LedgerMapping.company_id == company.id,
            LedgerMapping.party_ledger == party.ledger.name,
        )
    )
    if mapping is None:
        db.add(
            LedgerMapping(
                company_id=company.id,
                party_ledger=party.ledger.name,
                item_ledger=result.item.ledger,
            )
        )
    else:
        mapping.item_ledger = result.item.ledger


def _claim(db: Session, voucher: Voucher, only_ready: bool) -> str:
    """Marks the voucher as being posted, so a double click, a second reviewer or an edit
    can't post or change it meanwhile, and re-reads it. Returns the status it had."""
    current = db.scalar(select(Voucher.status).where(Voucher.id == voucher.id))
    if current == VoucherStatus.POSTING:
        raise VoucherError("This voucher is already being posted.")
    if current not in VoucherStatus.editable():
        raise VoucherError(f"This voucher is {_describe(current)} and can't be posted.")
    if only_ready and current != VoucherStatus.READY:
        raise VoucherError("This voucher needs review before it can be posted.")
    claimed = db.execute(
        update(Voucher)
        .where(Voucher.id == voucher.id, Voucher.status == current)
        .values(status=VoucherStatus.POSTING)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if claimed.rowcount != 1:
        raise VoucherError("This voucher is already being posted.")
    db.refresh(voucher)
    return current


def post_voucher(
    db: Session,
    voucher: Voucher,
    company: Company,
    connector: AccountingConnector,
    user: User | None,
    *,
    only_ready: bool = False,
) -> PostOutcome:
    """Posts as `user`, or as the system when None. With only_ready, a voucher that no
    longer passes every check is left for a person instead."""
    actor_id = user.id if user else None
    status_before = _claim(db, voucher, only_ready)

    # Checked after claiming: edits are refused from here on, so what passes is what is sent.
    report = evaluate(db, voucher, company)
    errors = [i.message for i in report.issues if i.severity == "error"]
    result = AccountingResult.model_validate(voucher.accounting)
    blocked = errors or result.transaction is None
    if blocked or (only_ready and report.route != "ready"):
        voucher.status = status_before
        _route(voucher, report)
        db.commit()
        if blocked:
            problems = [p.message for p in result.problems]
            raise VoucherBlocked(errors or problems or ["The voucher could not be built."])
        raise VoucherError("This voucher needs review before it can be posted.")

    unreachable = False
    if posting.already_posted(db, voucher.voucher_uid):
        outcome = posting.PostingOutcome(success=True)
    else:
        try:
            outcome = posting.post_transaction(db, company, connector, result.transaction, actor_id)
        except posting.PostingError as exc:
            outcome = posting.PostingOutcome(success=False, errors=[str(exc)])
            unreachable = isinstance(exc.__cause__, ConnectorError)

    if outcome.success:
        voucher.status = VoucherStatus.POSTED
        voucher.posted_at = utcnow()
        voucher.posted_by = actor_id
        voucher.external_id = outcome.external_id
        voucher.post_error = None
        _learn(db, company, result)
    else:
        voucher.status = VoucherStatus.POST_FAILED
        voucher.post_error = "; ".join(outcome.errors) or "The accounting system rejected it."
    audit.record(
        db,
        action="voucher.posted" if outcome.success else "voucher.post_failed",
        entity_type="voucher",
        entity_id=voucher.id,
        company_id=voucher.company_id,
        actor_id=actor_id,
        data={
            "voucher_uid": voucher.voucher_uid,
            "ledgers_created": outcome.ledgers_created,
            "errors": outcome.errors,
            "external_id": outcome.external_id,
        },
    )
    db.commit()
    if outcome.ledgers_created:
        reevaluate_open(db, company)  # other drafts may now match the new ledger
    return PostOutcome(
        voucher=voucher, success=outcome.success, errors=outcome.errors, unreachable=unreachable
    )


_INTERRUPTED = (
    "Posting stopped unexpectedly. Check in Tally whether this voucher arrived before "
    "posting it again."
)


def _mark_interrupted(db: Session, voucher_id: int, company_id: int) -> None:
    stuck = db.execute(
        update(Voucher)
        .where(Voucher.id == voucher_id, Voucher.status == VoucherStatus.POSTING)
        .values(status=VoucherStatus.POST_FAILED, post_error=_INTERRUPTED)
    )
    if stuck.rowcount == 1:
        audit.record(
            db,
            action="voucher.post_failed",
            entity_type="voucher",
            entity_id=voucher_id,
            company_id=company_id,
            data={"errors": [_INTERRUPTED]},
        )
    db.commit()


@dataclass
class _Tally:
    connector: AccountingConnector
    open_books: set[str]  # companies open in it, as _book() names them


def _book(name: str) -> str:
    return name.strip().casefold()


def _open_books(connector: AccountingConnector, url: str) -> set[str]:
    try:
        return {_book(c.name) for c in connector.list_companies()}
    except ConnectorError as exc:
        log.info("Auto-post is waiting for Tally at %s: %s", url, exc)
    except Exception:
        log.exception("Auto-post could not ask Tally at %s which companies are open", url)
    return set()


def _auto_post_one(
    db: Session, voucher_id: int, company: Company, connector: AccountingConnector
) -> PostOutcome | None:
    voucher = db.get(Voucher, voucher_id, populate_existing=True)
    if voucher is None or voucher.status != VoucherStatus.READY:
        return None  # changed since it was listed (edited, posted by a person, ...)
    try:
        return post_voucher(db, voucher, company, connector, None, only_ready=True)
    except VoucherError as exc:
        log.info("Auto-post skipped voucher %s: %s", voucher_id, exc)
    except Exception:
        log.exception("Auto-posting voucher %s failed", voucher_id)
        db.rollback()
        _mark_interrupted(db, voucher_id, company.id)
    return None


def _auto_post_company(db: Session, company: Company, tally: _Tally, ready: list[int]) -> int:
    posted = 0
    for voucher_id in ready:
        db.refresh(company)  # an admin may change the rules, or turn auto posting off, meanwhile
        if not company.auto_post:
            break
        outcome = _auto_post_one(db, voucher_id, company, tally.connector)
        if outcome is not None and outcome.unreachable:
            tally.open_books.clear()  # Tally went away: the rest wait for a later round
            break
        posted += bool(outcome and outcome.success)
    return posted


def auto_post_ready(db: Session, connector_factory: ConnectorFactory | None = None) -> int:
    """Posts, as the system, every READY voucher of the companies that turned on auto
    posting, oldest first. Run by the worker; one failure never stops the rest. TallyPrime
    is often closed overnight or open on another client's books: that company's entries
    then stay ready for a later round instead of each failing. Returns how many were posted."""
    factory = connector_factory or default_connector_factory
    companies = list(
        db.scalars(select(Company).where(Company.auto_post.is_(True)).order_by(Company.id))
    )
    tallies: dict[str, _Tally] = {}  # asked once per round which companies are open
    posted = 0
    for company in companies:
        ready = list(
            db.scalars(
                select(Voucher.id)
                .where(Voucher.company_id == company.id, Voucher.status == VoucherStatus.READY)
                .order_by(Voucher.id)
            )
        )
        if not ready:
            continue
        url = connector_url(company)
        if url not in tallies:
            connector = factory(url)
            tallies[url] = _Tally(connector, _open_books(connector, url))
        if _book(company.external_company_name) not in tallies[url].open_books:
            log.info("Auto-post for company %s waits until its books are open in Tally", company.id)
            continue
        posted += _auto_post_company(db, company, tallies[url], ready)
    return posted


def reject_voucher(db: Session, voucher: Voucher, user: User, reason: str | None) -> None:
    if voucher.status in {VoucherStatus.POSTED, VoucherStatus.POSTING}:
        raise VoucherError("A posted voucher can't be rejected here. Delete it in Tally instead.")
    voucher.status = VoucherStatus.REJECTED
    audit.record(
        db,
        action="voucher.rejected",
        entity_type="voucher",
        entity_id=voucher.id,
        company_id=voucher.company_id,
        actor_id=user.id,
        data={"reason": reason},
    )
    db.commit()


def reopen_voucher(db: Session, voucher: Voucher, company: Company, user: User) -> None:
    if voucher.status != VoucherStatus.REJECTED:
        raise VoucherError("Only rejected vouchers can be reopened.")
    voucher.status = VoucherStatus.NEEDS_REVIEW
    evaluate(db, voucher, company)
    audit.record(
        db,
        action="voucher.reopened",
        entity_type="voucher",
        entity_id=voucher.id,
        company_id=voucher.company_id,
        actor_id=user.id,
    )
    db.commit()


def requeue_waiting_for_ai(db: Session, actor: User | None) -> int:
    """Queues documents that could not be read because AI reading wasn't set up (or its key
    was rejected), or because the service in use reads only text and they are scans, and
    that nobody has typed in since. Run when the AI settings change, so adding a key (or
    choosing a service that reads scans) is enough for the waiting documents to be read."""
    from app.extraction import services as catalog
    from app.extraction.extractor import waiting_for_ai
    from app.services import app_settings

    service = catalog.get(app_settings.current().ai_provider) or catalog.DEFAULT

    def waiting_for_this(error: str | None) -> bool:
        if waiting_for_ai(error):
            return True
        # A scan that a text-only service could not read (app.extraction.base): "DeepSeek
        # can only read text ... Choose another AI service in Settings".
        return service.reads_images and "can only read text" in (error or "")

    candidates = [
        v
        for v in db.scalars(
            select(Voucher).where(
                Voucher.source == "manual",
                Voucher.status.in_(VoucherStatus.editable()),
                Voucher.extraction_error.is_not(None),
            )
        )
        if v.extraction is None and waiting_for_this(v.extraction_error)
    ]
    # A person's edit, even of the ledger choices only, means they took the document over.
    edited = set(
        db.scalars(
            select(AuditEvent.entity_id).where(
                AuditEvent.entity_type == "voucher",
                AuditEvent.action == "voucher.edited",
                AuditEvent.entity_id.in_([str(v.id) for v in candidates]),
            )
        )
    )
    waiting = [v for v in candidates if str(v.id) not in edited]
    for voucher in waiting:
        voucher.status = VoucherStatus.PENDING
        voucher.attempts = 0
        voucher.extraction_error = None
        audit.record(
            db,
            action="voucher.reextract",
            entity_type="voucher",
            entity_id=voucher.id,
            company_id=voucher.company_id,
            actor_id=actor.id if actor else None,
            data={"reason": "ai_settings_changed"},
        )
    db.commit()
    return len(waiting)


def request_extraction(db: Session, voucher: Voucher, user: User) -> None:
    if voucher.status not in VoucherStatus.editable() | {VoucherStatus.REJECTED}:
        raise VoucherError("This voucher can't be read again now.")
    voucher.status = VoucherStatus.PENDING
    voucher.attempts = 0
    voucher.extraction_error = None
    audit.record(
        db,
        action="voucher.reextract",
        entity_type="voucher",
        entity_id=voucher.id,
        company_id=voucher.company_id,
        actor_id=user.id,
    )
    db.commit()
