"""Human-readable activity feed and per-voucher history built from audit_events and
posting_attempts.

CONTRACT (module A):

def company_activity(db, company_id, *, entity_type=None, action_prefix=None,
                     actor_id=None, before_id=None, limit=50) -> ActivityPage
    Newest first; keyset pagination on id (before_id). summary is one plain sentence per
    event, for every action the app records (see `grep -rn "action=" app`), with a generic
    fallback for unknown actions. Never includes secrets (settings events only name fields).
def voucher_history(db, voucher_id) -> VoucherHistory
    Oldest first: the document's events (uploaded, duplicate, parsed, parse_failed, retry),
    the voucher's events (extracted, extraction_skipped/failed, edited with FieldChange list,
    posted, post_failed, rejected, reopened, reextract), ledgers created while posting it,
    and every posting attempt for it (and for ledgers it created) with request/response XML.

company_activity(db, None, ...) lists the events that belong to no company (team members,
office settings, backups), which the company feed leaves out.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from app.extraction import services
from app.models import AuditEvent, Company, Document, PostingAttempt, User, Voucher
from app.schemas.api import (
    ActivityItem,
    ActivityPage,
    FieldChange,
    HistoryItem,
    PostingRecord,
    VoucherHistory,
)

MAX_PAGE = 200
SYSTEM = "The system"
POST_ACTIONS = ("voucher.posted", "voucher.post_failed")
LEDGER_ACTIONS = ("ledger.created", "ledger.create_failed")
_NAMED_DOCUMENT_ACTIONS = ("document.uploaded", "document.duplicate", "document.deleted")

FIELD_LABELS = {
    "document_type": "Document type",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "seller.name": "Seller name",
    "seller.gstin": "Seller GSTIN",
    "buyer.name": "Buyer name",
    "buyer.gstin": "Buyer GSTIN",
    "place_of_supply_code": "Place of supply",
    "reverse_charge": "Reverse charge",
    "taxable_value": "Taxable value",
    "cgst": "CGST",
    "sgst": "SGST",
    "igst": "IGST",
    "cess": "Cess",
    "round_off": "Round-off",
    "grand_total": "Grand total",
    "choices.direction": "Direction",
    "choices.party_ledger": "Party ledger",
    "choices.item_ledger": "Item ledger",
    "choices.create_party_ledger": "Create new ledger",
}
SETTINGS_LABELS = {
    "tally_url": "Tally address",
    "anthropic_api_key": "Claude API key",
    "claude_model": "Claude model",
    "claude_effort": "Claude effort level",
    "ai_provider": "AI service",
    "custom_base_url": "AI service address",
}
for _svc in services.SERVICES[1:]:
    # A label, not a sentence: "AI service model", not "The AI service model".
    _name = _svc.short.removeprefix("The ")
    _key = _svc.key_name
    SETTINGS_LABELS[f"{_svc.id}_api_key"] = f"{_name} {_key}" if _key == "API key" else _key
    SETTINGS_LABELS[f"{_svc.id}_model"] = f"{_name} model"
_ACRONYMS = {"gst", "gstin", "cgst", "sgst", "igst", "hsn", "sac", "pan", "url", "api", "id"}
_MONEY_FIELDS = {"taxable_value", "cgst", "sgst", "igst", "cess", "round_off", "grand_total"}
_CHOICE_KEYS = ("direction", "party_ledger", "item_ledger", "create_party_ledger")
_AUTOMATIC_CHOICES = {"choices.direction", "choices.party_ledger", "choices.item_ledger"}
_DOCUMENT_KINDS = {"credit_note": "credit note", "debit_note": "debit note"}
_ROLES = {"admin": "an administrator", "reviewer": "a reviewer", "preparer": "a preparer"}
_COMPANY_FIELDS = {
    "name": "name",
    "external_company_name": "Tally company name",
    "gstin": "GSTIN",
    "state": "state",
    "connector_url": "Tally address",
}

# Defence in depth: writers should never log secrets, but the feed must not leak one if
# they do. "tokens" (token counts) and "sha256" (file hash) are deliberately not matched.
_SECRET_KEY = re.compile(
    r"password|passwd|secret|api_?key|credential|(^|_)token$|(^|_)hash$", re.IGNORECASE
)
# API keys of the AI services an office can use. A service's error can quote a key that was
# pasted into the wrong field (e.g. as the model), and the feed is open to every user.
_SECRET_VALUE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{16,}"  # OpenAI, Anthropic (sk-ant-), OpenRouter (sk-or-), DeepSeek
    r"|gsk_[A-Za-z0-9]{20,}"  # Groq
    r"|xai-[A-Za-z0-9]{20,}"  # xAI
    r"|AIza[A-Za-z0-9_-]{30,}"  # Google (Gemini)
    r"|AQ\.[A-Za-z0-9_.-]{20,}"  # Google's newer Gemini keys
    # No prefix (Mistral, Together, ...): 32+ letters and digits in mixed case, with no
    # separator. Invoice numbers, GSTINs, model names and hex file hashes never look so.
    r"|(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]{32,}"
    r"(?![A-Za-z0-9])"
    r")"
)
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


# -- formatting -----------------------------------------------------------------------


def _indian(digits: str) -> str:
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + groups + [tail])


def rupees(amount: Any) -> str:
    """'₹1,23,456.00': Indian digit grouping, two decimals."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, fraction = f"{abs(value):f}".split(".")
    return f"{'-' if value < 0 else ''}₹{_indian(whole)}.{fraction}"


def _number(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return ("-" if n < 0 else "") + _indian(str(abs(n)))


def _cost(usd: Any) -> str | None:
    try:
        value = float(usd)
    except (TypeError, ValueError):
        return None
    if not value > 0:
        return None
    return "less than $0.01" if value < 0.005 else f"about ${value:,.2f}"


def _join(parts: Sequence[str]) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _join_clauses(clauses: Sequence[str]) -> str:
    """Like _join, but keeps 'changed the A and B, and approved C' readable."""
    if len(clauses) == 2 and " and " in clauses[0]:
        return f"{clauses[0]}, and {clauses[1]}"
    return _join(clauses)


def _sentence(text: str) -> str:
    text = _SECRET_VALUE.sub("[hidden]", " ".join(text.split()))
    return text if text.endswith((".", "!", "?")) else text + "."


def _errors(errors: Any) -> str:
    if isinstance(errors, str):
        errors = [errors]
    if not isinstance(errors, list):
        return ""
    return "; ".join(str(e).strip().rstrip(".") for e in errors if str(e).strip())


def _failed(prefix: str, errors: Any) -> str:
    message = _errors(errors)
    return f"{prefix}: {message}." if message else f"{prefix}."


def _plural(count: Any, word: str) -> str:
    return f"{_number(count)} {word}{'' if str(count) == '1' else 's'}"


def _same(a: Any, b: Any) -> bool:
    if a == b:
        return True
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (ArithmeticError, ValueError):
        return False


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _data(event: AuditEvent) -> dict[str, Any]:
    return _dict(event.data)


def _new_value(value: Any) -> Any:
    """Change logs store either the new value or a [before, after] pair."""
    return value[1] if isinstance(value, list) and len(value) == 2 else value


def _after(data: dict[str, Any]) -> dict[str, Any]:
    after = data.get("after")
    if not isinstance(after, dict):
        after = data.get("changes")
    if not isinstance(after, dict):
        after = data
    return {key: _new_value(value) for key, value in after.items()}


def _names(value: Any) -> list[str]:
    return [str(v) for v in value if v] if isinstance(value, list) else []


def _ledger_phrase(names: Sequence[str]) -> str:
    return f"the ledger{'s' if len(names) > 1 else ''} {_join(names)}"


def _external_id(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if text and text != "0" else None


def _utc(moment: datetime) -> datetime:
    """SQLite returns naive datetimes. They were stored in UTC, so say so, or a browser
    would read them as local time."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


# -- field changes --------------------------------------------------------------------


def field_label(path: str) -> str:
    """Readable label for an invoice field path, e.g. 'seller.gstin' -> 'Seller GSTIN'."""
    if label := FIELD_LABELS.get(path):
        return label
    words = [
        w.upper() if w.lower() in _ACRONYMS else w.lower() for w in re.split(r"[._\s]+", path) if w
    ]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else path


def _in_sentence(label: str) -> str:
    first, _, rest = label.partition(" ")
    first = first if first.isupper() else first.lower()
    return f"{first} {rest}" if rest else first


def _display(path: str, value: Any) -> Any:
    if path == "choices.create_party_ledger":
        return "Yes" if value else "No"
    if value is None or value == "":
        return "Automatic" if path in _AUTOMATIC_CHOICES else None
    if path == "choices.direction":
        return str(value).capitalize()
    if path in _MONEY_FIELDS:
        try:
            return rupees(value)
        except (ArithmeticError, ValueError):
            return value
    if path.endswith("_date"):
        try:
            day = date.fromisoformat(str(value))
        except ValueError:
            return value
        return f"{day.day} {day:%b %Y}"
    return value


def _change(path: str, before: Any, after: Any) -> FieldChange:
    return FieldChange(
        field=path,
        label=field_label(path),
        before=_display(path, before),
        after=_display(path, after),
    )


def field_changes(data: dict[str, Any]) -> list[FieldChange]:
    """FieldChange items for a voucher.edited event: invoice fields, then ledger choices."""
    out = [
        _change(path, *pair)
        for path, pair in _dict(data.get("changes")).items()
        if isinstance(pair, list) and len(pair) == 2
    ]
    choices = data.get("choices")
    if isinstance(choices, list) and len(choices) == 2:
        old, new = _dict(choices[0]), _dict(choices[1])
        extra = sorted((old.keys() | new.keys()) - set(_CHOICE_KEYS))
        for key in (*_CHOICE_KEYS, *extra):
            change = _change(f"choices.{key}", old.get(key), new.get(key))
            if change.before != change.after:
                out.append(change)
    return out


# -- lookups --------------------------------------------------------------------------


@dataclass(frozen=True)
class _VoucherInfo:
    id: int
    uid: str
    document_id: int
    invoice_number: str | None
    party_name: str | None
    document_type: str | None
    direction: str | None

    @property
    def kind(self) -> str:
        return _DOCUMENT_KINDS.get(self.document_type or "", "invoice")

    def ref(self) -> str:
        """e.g. 'invoice SE/2026/0042 from Sharma Electronics'."""
        text = f"{self.kind} {self.invoice_number}" if self.invoice_number else f"the {self.kind}"
        if self.party_name:
            text += f" {'to' if self.direction == 'sales' else 'from'} {self.party_name}"
        return text


@dataclass
class _Context:
    """Names and links for one page of events, loaded with a fixed number of queries."""

    users: dict[int, str]
    documents: dict[int, str]  # documents that still exist: id -> file name
    # When each existing document was created. Before 2026-10-02 SQLite could hand a deleted
    # document's id to a new upload, so an event about id N is about today's document N only
    # if it happened after that document was created.
    doc_created: dict[int, datetime]
    deleted: dict[int, str]  # deleted documents: id -> file name, from their events
    vouchers: dict[int, _VoucherInfo]
    by_uid: dict[str, _VoucherInfo]
    companies: dict[int, str]

    def actor_name(self, event: AuditEvent) -> str | None:
        return None if event.actor_id is None else self.user(event.actor_id)

    def actor(self, event: AuditEvent) -> str:
        return self.actor_name(event) or SYSTEM

    def user(self, user_id: Any) -> str:
        key = _int(user_id)
        return self.users.get(key, f"user #{user_id}") if key is not None else "a user"

    def is_current(self, doc_id: int, event: AuditEvent) -> bool:
        """Whether `event` is about the document that has this id today."""
        created = self.doc_created.get(doc_id)
        return created is not None and _utc(event.created_at) >= created

    def document_name(self, doc_id: Any, event: AuditEvent | None = None) -> str:
        key = _int(doc_id)
        if key is None:
            return "a file"
        if key in self.documents:
            if event is None or self.is_current(key, event):
                return self.documents[key]
            return "a file"  # an earlier document that had the same id
        return self.deleted.get(key) or f"document #{doc_id}"

    def filename(self, event: AuditEvent) -> str:
        return _data(event).get("filename") or self.document_name(event.entity_id, event)

    def company(self, event: AuditEvent) -> str:
        name = self.companies.get(_int(event.company_id) or 0)
        return name or _data(event).get("name") or "the company"

    def voucher(self, event: AuditEvent) -> _VoucherInfo | None:
        if event.entity_type == "voucher":
            return self.vouchers.get(_int(event.entity_id) or 0)
        if event.entity_type == "transaction":
            return self.by_uid.get(event.entity_id)
        return None

    def voucher_ref(self, event: AuditEvent) -> str:
        if info := self.voucher(event):
            return info.ref()
        tx = _dict(_data(event).get("transaction"))
        if number := tx.get("reference_no"):
            return f"voucher {number}"
        return f"voucher #{event.entity_id}" if event.entity_type == "voucher" else "a voucher"

    def kind(self, event: AuditEvent) -> str:
        info = self.voucher(event)
        return info.kind if info else "invoice"

    def document_id(self, event: AuditEvent) -> int | None:
        if event.entity_type == "document":
            doc_id = _int(event.entity_id)
            if doc_id in self.documents and self.is_current(doc_id, event):
                return doc_id
            return None  # no link to a deleted file, or to a later file that reused its id
        info = self.voucher(event)
        return info.document_id if info else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ids(values: Iterable[Any]) -> set[int]:
    return {n for n in (_int(v) for v in values) if n is not None}


def _load_context(db: Session, events: Sequence[AuditEvent]) -> _Context:
    user_ids = _ids(e.actor_id for e in events) | _ids(
        e.entity_id for e in events if e.entity_type == "user"
    )
    doc_ids = _ids(e.entity_id for e in events if e.entity_type == "document") | _ids(
        _data(e).get("duplicate_of") for e in events if e.action == "document.duplicate"
    )
    voucher_ids = _ids(e.entity_id for e in events if e.entity_type == "voucher")
    uids = {e.entity_id for e in events if e.entity_type == "transaction"}
    company_ids = _ids(e.company_id for e in events)

    users = (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(user_ids))).all())
        if user_ids
        else {}
    )
    doc_rows = (
        db.execute(
            select(Document.id, Document.original_filename, Document.created_at).where(
                Document.id.in_(doc_ids)
            )
        ).all()
        if doc_ids
        else []
    )
    documents = {doc_id: name for doc_id, name, _ in doc_rows}
    companies = (
        dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids))).all())
        if company_ids
        else {}
    )
    vouchers = _load_vouchers(db, voucher_ids, uids) if voucher_ids or uids else []
    return _Context(
        users=users,
        documents=documents,
        doc_created={doc_id: _utc(created) for doc_id, _, created in doc_rows},
        deleted=_deleted_names(db, doc_ids - documents.keys()),
        vouchers={v.id: v for v in vouchers},
        by_uid={v.uid: v for v in vouchers},
        companies=companies,
    )


def _deleted_names(db: Session, doc_ids: set[int]) -> dict[int, str]:
    """File names of deleted documents: their upload and delete events still have them."""
    if not doc_ids:
        return {}
    rows = db.execute(
        select(AuditEvent.entity_id, AuditEvent.data["filename"].as_string()).where(
            AuditEvent.entity_type == "document",
            AuditEvent.action.in_(_NAMED_DOCUMENT_ACTIONS),
            AuditEvent.entity_id.in_([str(i) for i in doc_ids]),
        )
    ).all()
    return {int(doc_id): name for doc_id, name in rows if name}


def _load_vouchers(db: Session, ids: set[int], uids: set[str]) -> list[_VoucherInfo]:
    # Only the columns a summary needs: the invoice and accounting JSON can be large.
    rows = db.execute(
        select(
            Voucher.id,
            Voucher.voucher_uid,
            Voucher.document_id,
            Voucher.invoice_number,
            Voucher.party_name,
            Voucher.invoice["document_type"].as_string(),
            Voucher.accounting["direction"].as_string(),
        ).where(or_(Voucher.id.in_(ids), Voucher.voucher_uid.in_(uids)))
    ).all()
    return [_VoucherInfo(*row) for row in rows]


# -- summaries ------------------------------------------------------------------------

Summarizer = Callable[[AuditEvent, _Context], str]


def _user_setup(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} set up this office and became its first administrator."


def _user_login_failed(e: AuditEvent, c: _Context) -> str:
    email = _data(e).get("email") or "an unknown account"
    return f"Someone failed to sign in as {email}."


def _user_login(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} signed in."


def _user_created(e: AuditEvent, c: _Context) -> str:
    role = str(_data(e).get("role") or "")
    as_role = f" as {_ROLES.get(role, role)}" if role else ""
    return f"{c.actor(e)} added {c.user(e.entity_id)} to the team{as_role}."


def _user_updated(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    after, before = _after(data), _dict(data.get("before"))
    target = c.user(e.entity_id)
    clauses = []
    if role := after.get("role"):
        clauses.append(f"made {target} {_ROLES.get(str(role), str(role))}")
    if "is_active" in after:
        verb = "reactivated" if after["is_active"] else "deactivated"
        clauses.append(f"{verb} the account of {target}")
    if after.get("full_name"):
        old = before.get("full_name")
        renamed = f"renamed {old} to {after['full_name']}"
        clauses.append(renamed if old else f"changed the name of {target}")
    return f"{c.actor(e)} {_join(clauses) or f'updated the account of {target}'}."


def _password_reset(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} set a new password for {c.user(e.entity_id)}."


def _password_changed(e: AuditEvent, c: _Context) -> str:
    if _int(e.entity_id) == e.actor_id:
        return f"{c.actor(e)} changed their password."
    return f"{c.actor(e)} changed the password of {c.user(e.entity_id)}."


def _company_created(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} added the company {_data(e).get('name') or c.company(e)}."


def _company_rule(key: str, value: Any) -> str | None:
    match key:
        case "auto_post":
            return "turned on automatic posting" if value else "turned off automatic posting"
        case "always_review":
            if value:
                return "sent every entry to review"
            return "let entries that pass every check skip review"
        case "auto_create_ledgers":
            if value:
                return "allowed new ledgers to be created without approval"
            return "required approval before new ledgers are created"
        case "review_above_amount":
            if value in (None, ""):
                return "removed the amount above which entries need review"
            try:
                return f"required review for entries above {rupees(value)}"
            except (ArithmeticError, ValueError):
                return "changed the amount above which entries need review"
    return None


def _company_updated(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    before = _dict(data.get("before"))
    clauses: list[str] = []
    fields: list[str] = []
    for key, value in _after(data).items():
        if key == "before" or (key in before and _same(before[key], value)):
            continue
        if rule := _company_rule(key, value):
            clauses.append(rule)
        else:
            fields.append(_COMPANY_FIELDS.get(key) or _in_sentence(field_label(key)))
    if fields:
        clauses.append(f"changed the {_join(fields)}")
    if not clauses:
        return f"{c.actor(e)} saved the settings of {c.company(e)} without changes."
    return f"{c.actor(e)} updated {c.company(e)}: {_join_clauses(clauses)}."


def _ledgers_synced(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    added, removed = data.get("added") or 0, data.get("removed") or 0
    detail = _join(
        [f"{_number(added)} new" if added else "", f"{_number(removed)} removed" if removed else ""]
    )
    count = _plural(data.get("ledgers", 0), "ledger")
    return f"{c.actor(e)} synced {count} from Tally ({detail or 'no changes'})."


def _ledger_created(e: AuditEvent, c: _Context) -> str:
    parent = _dict(_data(e).get("ledger")).get("parent_group")
    under = f" under {parent}" if parent else ""
    return f"{c.actor(e)} created the ledger {e.entity_id}{under} in Tally."


def _ledger_create_failed(e: AuditEvent, c: _Context) -> str:
    return _failed(f"Creating the ledger {e.entity_id} in Tally failed", _data(e).get("errors"))


def _document_uploaded(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} uploaded {c.filename(e)}."


def _document_duplicate(e: AuditEvent, c: _Context) -> str:
    original = c.document_name(_data(e).get("duplicate_of"))
    return (
        f"{c.actor(e)} uploaded {c.filename(e)}, which is a copy of {original}, "
        "so it was not read again."
    )


def _document_parsed(e: AuditEvent, c: _Context) -> str:
    pages = _int(_data(e).get("pages")) or 0
    detail = f" ({_plural(pages, 'page')})" if pages > 0 else ""
    return f"{c.actor(e)} read {c.filename(e)}{detail}."


def _document_parse_failed(e: AuditEvent, c: _Context) -> str:
    return _failed(f"Reading {c.filename(e)} failed", _data(e).get("error"))


def _document_retry(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} asked for {c.filename(e)} to be read again."


def _document_deleted(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} deleted {c.filename(e)}."


def _reader(data: dict[str, Any]) -> str:
    """The AI service a voucher.extract* event was about, e.g. "Gemini". The record must name
    the third party that got a client's document, so a service is never guessed: events from
    before the service was recorded say Claude only when the model shows it."""
    if service_id := data.get("service"):
        service = services.get(str(service_id))
        return service.short if service else "The AI service"
    return "Claude" if str(data.get("model") or "").startswith("claude") else "The AI service"


def _mid_sentence(name: str) -> str:
    return f"t{name[1:]}" if name.startswith("The ") else name


def _extracted(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    details = [str(data["model"])] if data.get("model") else []
    tokens = data.get("tokens")
    if isinstance(tokens, list) and len(tokens) == 2:
        details.append(f"{_number(tokens[0])} + {_number(tokens[1])} tokens")
    if cost := _cost(data.get("cost_usd")):
        details.append(cost)
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{_reader(data)} read the {c.kind(e)}{suffix}."


def _extraction_skipped(e: AuditEvent, c: _Context) -> str:
    # Nothing was sent. The document waits for the settings: changing them queues it again.
    service = _mid_sentence(_reader(_data(e)))
    return (
        f"The {c.kind(e)} was not read because {service} is not set up yet. It will be read "
        "automatically once an administrator finishes the setup in Settings."
    )


def _extraction_failed(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    return _failed(f"{_reader(data)} could not read the {c.kind(e)}", data.get("error"))


def _edited(e: AuditEvent, c: _Context) -> str:
    changes = field_changes(_data(e))
    labels = [_in_sentence(ch.label) for ch in changes if ch.field != "choices.create_party_ledger"]
    if len(labels) > 5:
        labels = [*labels[:4], f"{len(labels) - 4} other fields"]
    clauses = [f"changed the {_join(labels)}"] if labels else []
    approval = next((ch for ch in changes if ch.field == "choices.create_party_ledger"), None)
    if approval is not None:
        clauses.append(
            "approved a new party ledger"
            if approval.after == "Yes"
            else "withdrew approval for a new party ledger"
        )
    return f"{c.actor(e)} {_join_clauses(clauses) or 'saved the voucher without changes'}."


def _posted(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    external = _external_id(data.get("external_id"))
    if e.entity_type == "transaction":  # the connector's own record of the same post
        accepted = f" as voucher {external}" if external else ""
        return f"Tally accepted {c.voucher_ref(e)}{accepted}."
    text = f"{c.actor(e)} posted {c.voucher_ref(e)} to Tally"
    if external:
        text += f" (Tally voucher {external})"
    if ledgers := _names(data.get("ledgers_created")):
        text += f" and created {_ledger_phrase(ledgers)}"
    return text + "."


def _post_failed(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    if e.entity_type == "transaction":
        return _failed(f"Tally rejected {c.voucher_ref(e)}", data.get("errors"))
    prefix = "Posting to Tally failed"
    if ledgers := _names(data.get("ledgers_created")):
        prefix += f" after creating {_ledger_phrase(ledgers)}"
    return _failed(prefix, data.get("errors"))


def _rejected(e: AuditEvent, c: _Context) -> str:
    reason = str(_data(e).get("reason") or "").strip().rstrip(".")
    because = f" (reason: {reason})" if reason else ""
    return f"{c.actor(e)} rejected {c.voucher_ref(e)}{because}."


def _reopened(e: AuditEvent, c: _Context) -> str:
    return f"{c.actor(e)} reopened {c.voucher_ref(e)} for review."


def _reextract(e: AuditEvent, c: _Context) -> str:
    if (e.data or {}).get("reason") == "ai_settings_changed":
        return f"Queued {c.voucher_ref(e)} to be read now that AI reading is set up."
    return f"{c.actor(e)} asked the AI to read {c.voucher_ref(e)} again."


def settings_fields(data: dict[str, Any]) -> list[str]:
    """Names of the settings changed. Values are never read, so a key can't leak."""
    for key in ("changed", "fields", "changed_fields"):
        value = data.get(key)
        if isinstance(value, dict | list):
            return [str(v) for v in value if isinstance(v, str) and _FIELD_NAME.fullmatch(v)]
    return [key for key in data if key in SETTINGS_LABELS]


def _settings_updated(e: AuditEvent, c: _Context) -> str:
    labels = [SETTINGS_LABELS.get(f) or field_label(f) for f in settings_fields(_data(e))]
    if not labels:
        return f"{c.actor(e)} saved the office settings without changes."
    return f"{c.actor(e)} changed the {_join(labels)} in the office settings."


def _backup_created(e: AuditEvent, c: _Context) -> str:
    data = _data(e)
    what = {
        "scheduled": "the scheduled backup",
        "before_migration": "a backup before updating the database",
    }.get(str(data.get("reason")), "a backup")
    name = data.get("name") or e.entity_id
    text = f"{c.actor(e)} made {what}" + (f" ({name})" if name else "")
    if pruned := _names(data.get("pruned")):
        text += f" and removed {_plural(len(pruned), 'older backup')}"
    return text + "."


def _backup_downloaded(e: AuditEvent, c: _Context) -> str:
    name = _data(e).get("name") or e.entity_id
    return f"{c.actor(e)} downloaded the backup {name or ''}".rstrip() + "."


def _generic(e: AuditEvent, c: _Context) -> str:
    words = re.sub(r"[._]+", " ", e.action).strip() or "activity"
    by = "the system" if e.actor_id is None else c.actor(e)
    return f"{words[:1].upper()}{words[1:]} by {by} ({e.entity_type} {e.entity_id})."


_SUMMARIES: dict[str, Summarizer] = {
    "user.setup_admin": _user_setup,
    "user.login": _user_login,
    "user.login_failed": _user_login_failed,
    "user.created": _user_created,
    "user.updated": _user_updated,
    "user.password_reset": _password_reset,
    "user.password_changed": _password_changed,
    "company.created": _company_created,
    "company.updated": _company_updated,
    "ledgers.synced": _ledgers_synced,
    "ledger.created": _ledger_created,
    "ledger.create_failed": _ledger_create_failed,
    "document.uploaded": _document_uploaded,
    "document.duplicate": _document_duplicate,
    "document.parsed": _document_parsed,
    "document.parse_failed": _document_parse_failed,
    "document.retry": _document_retry,
    "document.deleted": _document_deleted,
    "voucher.extracted": _extracted,
    "voucher.extraction_skipped": _extraction_skipped,
    "voucher.extraction_failed": _extraction_failed,
    "voucher.edited": _edited,
    "voucher.posted": _posted,
    "voucher.post_failed": _post_failed,
    "voucher.rejected": _rejected,
    "voucher.reopened": _reopened,
    "voucher.reextract": _reextract,
    "settings.updated": _settings_updated,
    "backup.created": _backup_created,
    "backup.downloaded": _backup_downloaded,
}


def _summarizer(event: AuditEvent) -> Summarizer:
    if summarize := _SUMMARIES.get(event.action):
        return summarize
    # Other company changes (e.g. posting rules) are logged as before/after like updates.
    if event.action.startswith("company.") and isinstance(_data(event).get("after"), dict):
        return _company_updated
    return _generic


def summarize(event: AuditEvent, context: _Context) -> str:
    try:
        text = _summarizer(event)(event, context)
    except Exception:  # one malformed event must not break the whole feed
        text = _generic(event, context)
    return _sentence(text)


def public_data(event: AuditEvent) -> dict[str, Any]:
    data = _data(event)
    if event.action.startswith("settings."):
        return {"changed": settings_fields(data)}
    return _scrub(data)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[hidden]", value)
    return value


# -- company activity -----------------------------------------------------------------


def company_activity(
    db: Session,
    company_id: int | None,
    *,
    entity_type: str | None = None,
    action_prefix: str | None = None,
    actor_id: int | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> ActivityPage:
    """company_id None gives the office-wide events instead: team, settings and backups."""
    limit = max(1, min(limit, MAX_PAGE))
    stmt = select(AuditEvent).where(
        AuditEvent.company_id.is_(None)
        if company_id is None
        else AuditEvent.company_id == company_id
    )
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if action_prefix:
        stmt = stmt.where(AuditEvent.action.startswith(action_prefix, autoescape=True))
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if before_id is not None:
        stmt = stmt.where(AuditEvent.id < before_id)
    events = list(db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit + 1)))
    more = len(events) > limit
    events = events[:limit]
    context = _load_context(db, events)
    items = [
        ActivityItem(
            id=e.id,
            created_at=_utc(e.created_at),
            actor_id=e.actor_id,
            actor_name=context.actor_name(e),
            action=e.action,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            company_id=e.company_id,
            summary=summarize(e, context),
            document_id=context.document_id(e),
            data=public_data(e),
        )
        for e in events
    ]
    return ActivityPage(items=items, next_before_id=events[-1].id if more else None)


# -- voucher history ------------------------------------------------------------------


def _posting_record(attempt: PostingAttempt) -> PostingRecord:
    return PostingRecord(
        id=attempt.id,
        kind=attempt.kind,
        reference=attempt.reference,
        success=attempt.success,
        error=attempt.error,
        request_payload=attempt.request_payload,
        response_payload=attempt.response_payload,
        created_at=_utc(attempt.created_at),
    )


def _same_actor(actor_id: int | None) -> ColumnElement[bool]:
    return AuditEvent.actor_id.is_(None) if actor_id is None else AuditEvent.actor_id == actor_id


def _run_start(db: Session, company_id: int, post: AuditEvent, after_id: int) -> int:
    """Id after which the posting run that `post` ended began. Edits are refused while a
    voucher is being posted, so it began after the voucher's previous event; and one
    person posts one voucher at a time, so after their previous post of any voucher."""
    other = db.scalar(
        select(func.max(AuditEvent.id)).where(
            AuditEvent.company_id == company_id,
            AuditEvent.entity_type == "voucher",
            AuditEvent.action.in_(POST_ACTIONS),
            _same_actor(post.actor_id),
            AuditEvent.id > after_id,
            AuditEvent.id < post.id,
        )
    )
    return other or after_id


def _made_by_run(ledger_event: AuditEvent, post: AuditEvent) -> bool:
    data = _data(post)
    if "ledgers_created" not in data:  # interrupted: what the run did is not known
        return True
    if ledger_event.action == "ledger.created":
        return ledger_event.entity_id in _names(data["ledgers_created"])
    # Posting stops at the first ledger Tally refuses and reports that ledger's errors.
    return post.action == "voucher.post_failed" and (
        _data(ledger_event).get("errors") == data.get("errors")
    )


def _run_ledger_events(
    db: Session, voucher: Voucher, own: Sequence[AuditEvent]
) -> list[AuditEvent]:
    """ledger.* events written while this voucher was being posted. Found by when and by
    whom they were written, never by the ledgers the voucher proposes now: an edit or a
    ledger sync changes those, and the history must not change after the fact."""
    found: list[AuditEvent] = []
    previous_id = 0
    for event in own:
        if event.entity_type == "voucher" and event.action in POST_ACTIONS:
            start = _run_start(db, voucher.company_id, event, previous_id)
            window = db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.company_id == voucher.company_id,
                    AuditEvent.entity_type == "ledger",
                    AuditEvent.action.in_(LEDGER_ACTIONS),
                    _same_actor(event.actor_id),
                    AuditEvent.id > start,
                    AuditEvent.id < event.id,
                )
                .order_by(AuditEvent.id)
            )
            found.extend(e for e in window if _made_by_run(e, event))
        previous_id = event.id
    return found


def _ledger_attempts(
    db: Session, voucher: Voucher, events: Sequence[AuditEvent], posts: Sequence[AuditEvent]
) -> dict[int, PostingAttempt]:
    """The Tally request behind each ledger event: it was written in the same commit, so
    it is the attempt for the same ledger closest in time."""
    if not events:
        return {}
    until = max(e.created_at for e in posts)  # every ledger event here precedes a post
    candidates = list(
        db.scalars(
            select(PostingAttempt).where(
                PostingAttempt.company_id == voucher.company_id,
                PostingAttempt.kind == "ledger",
                PostingAttempt.reference.in_({e.entity_id for e in events}),
                PostingAttempt.created_at >= voucher.created_at,
                PostingAttempt.created_at <= until,
            )
        )
    )
    attached: dict[int, PostingAttempt] = {}
    for event in events:
        same = [a for a in candidates if a.reference == event.entity_id]
        if same:
            closest = min(same, key=lambda a: abs(a.created_at - event.created_at))
            candidates.remove(closest)
            attached[event.id] = closest
    return attached


def _pair_voucher_attempts(
    posts: Sequence[AuditEvent], voucher_attempts: Sequence[PostingAttempt]
) -> tuple[dict[int, PostingAttempt], list[PostingAttempt]]:
    """Attaches each voucher attempt to the post event that recorded its outcome. An
    attempt is committed before the voucher.posted / post_failed event that follows it
    (and before the next attempt). Attempts without such an event are returned separately.
    """
    attached: dict[int, PostingAttempt] = {}
    loose: list[PostingAttempt] = []
    free = list(posts)
    for i, attempt in enumerate(voucher_attempts):
        upper = voucher_attempts[i + 1].created_at if i + 1 < len(voucher_attempts) else None
        match = next(
            (
                e
                for e in free
                if e.created_at >= attempt.created_at and (upper is None or e.created_at < upper)
            ),
            None,
        )
        if match is None:
            loose.append(attempt)
        else:
            free.remove(match)
            attached[match.id] = attempt
    return attached, loose


def _loose_item(attempt: PostingAttempt, info: _VoucherInfo) -> HistoryItem:
    if attempt.success:
        summary = f"Tally accepted {info.ref()}."
    else:
        summary = _failed(f"Tally rejected {info.ref()}", attempt.error)
    return HistoryItem(
        at=_utc(attempt.created_at),
        actor_name=None,
        action=f"posting.{attempt.kind}",
        summary=_sentence(summary),
        posting=_posting_record(attempt),
    )


def voucher_history(db: Session, voucher_id: int) -> VoucherHistory:
    voucher = db.get(Voucher, voucher_id)
    if voucher is None:
        raise LookupError(f"Voucher {voucher_id} does not exist.")
    own = list(
        db.scalars(
            select(AuditEvent)
            .where(
                or_(
                    and_(
                        AuditEvent.entity_type == "document",
                        AuditEvent.entity_id == str(voucher.document_id),
                    ),
                    and_(
                        AuditEvent.entity_type == "voucher",
                        AuditEvent.entity_id == str(voucher.id),
                    ),
                )
            )
            .order_by(AuditEvent.id)
        )
    )
    document = db.get(Document, voucher.document_id)
    if document is not None:  # leave out events of an earlier document that had the same id
        since = _utc(document.created_at)
        own = [e for e in own if e.entity_type != "document" or _utc(e.created_at) >= since]
    posts = [e for e in own if e.entity_type == "voucher" and e.action in POST_ACTIONS]
    ledger_events = _run_ledger_events(db, voucher, own)
    attached = _ledger_attempts(db, voucher, ledger_events, posts)
    voucher_attempts = list(
        db.scalars(
            select(PostingAttempt)
            .where(
                PostingAttempt.company_id == voucher.company_id,
                PostingAttempt.kind == "voucher",
                PostingAttempt.reference == voucher.voucher_uid,
            )
            .order_by(PostingAttempt.id)
        )
    )
    posted, loose = _pair_voucher_attempts(posts, voucher_attempts)
    attached |= posted

    events = sorted([*own, *ledger_events], key=lambda e: e.id)
    context = _load_context(db, events)
    info = context.vouchers.get(voucher.id) or _VoucherInfo(
        voucher.id,
        voucher.voucher_uid,
        voucher.document_id,
        voucher.invoice_number,
        voucher.party_name,
        _dict(voucher.invoice).get("document_type"),
        _dict(voucher.accounting).get("direction"),
    )
    items = [
        HistoryItem(
            at=_utc(e.created_at),
            actor_name=context.actor_name(e),
            action=e.action,
            summary=summarize(e, context),
            changes=field_changes(_data(e)) if e.action == "voucher.edited" else [],
            posting=_posting_record(attached[e.id]) if e.id in attached else None,
        )
        for e in events
    ]
    items.extend(_loose_item(attempt, info) for attempt in loose)
    items.sort(key=lambda item: item.at)  # stable: events keep their id order on ties
    return VoucherHistory(voucher_id=voucher.id, document_id=voucher.document_id, items=items)
