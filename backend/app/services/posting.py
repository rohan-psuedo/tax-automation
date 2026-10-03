"""Posts canonical transactions to the connected accounting system.

Order of operations: check that the company is open there and that new ledger names are
still free, create approved new ledgers, then the voucher. Every request and response is
stored in posting_attempts, and the outcome goes to the audit log.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.connectors.base import AccountingConnector, ConnectorError, PostResult
from app.models import Company, Ledger, PostingAttempt
from app.schemas.canonical import CanonicalTransaction, ProposedLedger


class PostingError(Exception):
    pass


@dataclass
class PostingOutcome:
    success: bool
    ledgers_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    external_id: str | None = None


def _log_attempt(
    db: Session, company: Company, kind: str, reference: str, result: PostResult
) -> None:
    db.add(
        PostingAttempt(
            company_id=company.id,
            kind=kind,
            reference=reference,
            request_payload=result.request_payload,
            response_payload=result.response_payload,
            success=result.success,
            error="; ".join(result.errors) or None,
        )
    )


def _local_ledger_names(db: Session, company: Company) -> set[str]:
    return set(db.scalars(select(Ledger.name).where(Ledger.company_id == company.id)))


def _key(name: str) -> str:
    return name.strip().casefold()  # Tally compares company and ledger names this way


def _check_company_open(company: Company, connector: AccountingConnector) -> None:
    """Tally can import into whichever company is active when the one asked for isn't open,
    so nothing is sent unless it is."""
    try:
        open_books = connector.list_companies()
    except ConnectorError as exc:
        raise PostingError(str(exc)) from exc
    name = company.external_company_name
    if not any(_key(c.name) == _key(name) for c in open_books):
        raise PostingError(f"'{name}' is not open in Tally. Open it in TallyPrime and try again.")


def _check_names_free(
    company: Company, connector: AccountingConnector, ledgers: list[ProposedLedger]
) -> None:
    """Creating a ledger under a name Tally already uses (as a name or an alias) would change
    that ledger instead, so new names are checked against Tally itself, not the last sync."""
    try:
        existing = connector.fetch_ledgers(company.external_company_name)
    except ConnectorError as exc:
        raise PostingError(str(exc)) from exc
    taken = {_key(n) for led in existing for n in (led.name, *led.aliases)}
    clashes = [led.name for led in ledgers if _key(led.name) in taken]
    if clashes:
        names = ", ".join(f"'{n}'" for n in clashes)
        raise PostingError(
            f"Tally already has a ledger named {names}. Sync ledgers from Tally, then choose "
            "it on this entry instead of creating a new one."
        )


def create_ledger(
    db: Session,
    company: Company,
    connector: AccountingConnector,
    ledger: ProposedLedger,
    actor_id: int | None,
) -> PostResult:
    if ledger.name in _local_ledger_names(db, company):
        raise PostingError(f"Ledger '{ledger.name}' already exists")
    _check_company_open(company, connector)
    _check_names_free(company, connector, [ledger])
    return _create_ledger(db, company, connector, ledger, actor_id)


def _create_ledger(
    db: Session,
    company: Company,
    connector: AccountingConnector,
    ledger: ProposedLedger,
    actor_id: int | None,
) -> PostResult:
    try:
        result = connector.create_ledger(company.external_company_name, ledger)
    except ConnectorError as exc:
        raise PostingError(str(exc)) from exc
    _log_attempt(db, company, "ledger", ledger.name, result)
    if result.success:
        db.add(
            Ledger(
                company_id=company.id,
                name=ledger.name,
                parent=ledger.parent_group,
                gstin=ledger.gstin,
                state=ledger.state,
                aliases=[],
            )
        )
    audit.record(
        db,
        action="ledger.created" if result.success else "ledger.create_failed",
        entity_type="ledger",
        entity_id=ledger.name,
        company_id=company.id,
        actor_id=actor_id,
        data={"ledger": ledger.model_dump(mode="json"), "errors": result.errors},
    )
    db.commit()
    return result


def already_posted(db: Session, tx_id: str) -> bool:
    return (
        db.scalar(
            select(PostingAttempt.id).where(
                PostingAttempt.kind == "voucher",
                PostingAttempt.reference == tx_id,
                PostingAttempt.success.is_(True),
            )
        )
        is not None
    )


def post_transaction(
    db: Session,
    company: Company,
    connector: AccountingConnector,
    tx: CanonicalTransaction,
    actor_id: int | None,
) -> PostingOutcome:
    tx_id = str(tx.id)
    if already_posted(db, tx_id):
        raise PostingError(f"Transaction {tx_id} was already posted")

    known = _local_ledger_names(db, company)
    proposed = {p.name for p in tx.ledgers_to_create()}
    missing = sorted({e.ledger.name for e in tx.entries} - known - proposed)
    if missing:
        raise PostingError(
            "Unknown ledger(s): " + ", ".join(missing) + ". Sync ledgers or propose them as new."
        )

    _check_company_open(company, connector)
    # A proposed ledger already in the local cache was created by an earlier attempt.
    new = [led for led in tx.ledgers_to_create() if led.name not in known]
    if new:
        _check_names_free(company, connector, new)

    outcome = PostingOutcome(success=False)
    for led in new:
        result = _create_ledger(db, company, connector, led, actor_id)
        if not result.success:
            outcome.errors.extend(result.errors)
            return outcome
        outcome.ledgers_created.append(led.name)

    try:
        result = connector.post_transaction(company.external_company_name, tx)
    except ConnectorError as exc:
        raise PostingError(str(exc)) from exc
    _log_attempt(db, company, "voucher", tx_id, result)
    audit.record(
        db,
        action="voucher.posted" if result.success else "voucher.post_failed",
        entity_type="transaction",
        entity_id=tx_id,
        company_id=company.id,
        actor_id=actor_id,
        data={
            "transaction": tx.model_dump(mode="json"),
            "errors": result.errors,
            "external_id": result.external_id,
        },
    )
    db.commit()
    outcome.success = result.success
    outcome.errors.extend(result.errors)
    outcome.external_id = result.external_id
    return outcome
