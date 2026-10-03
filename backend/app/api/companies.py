from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic_core import to_jsonable_python
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import audit
from app.connectors.base import ConnectorError
from app.db import get_db
from app.deps import get_company, get_current_user, require_role
from app.models import Company, Ledger, LedgerGroup, User, UserRole
from app.schemas.api import (
    CompanyIn,
    CompanyOut,
    CompanyUpdate,
    GroupOut,
    LedgerCreateResult,
    LedgerOut,
    PostingOutcomeOut,
    SyncResultOut,
)
from app.schemas.canonical import CanonicalTransaction, ProposedLedger
from app.services import ledger_sync, posting, vouchers
from app.services.connectors import ConnectorFactory, connector_url, get_connector_factory

router = APIRouter(prefix="/api/companies", tags=["companies"])

# Fields a null may clear; for the others a null means "leave as is".
_CLEARABLE = {"gstin", "state", "connector_url", "review_above_amount"}


@router.get("", response_model=list[CompanyOut])
def list_companies(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[Company]:
    return list(db.scalars(select(Company).order_by(Company.name)))


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(
    body: CompanyIn, db: Session = Depends(get_db), user: User = Depends(require_role())
) -> Company:
    company = Company(**body.model_dump())
    db.add(company)
    db.flush()
    audit.record(
        db,
        action="company.created",
        entity_type="company",
        entity_id=company.id,
        company_id=company.id,
        actor_id=user.id,
        data=body.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(company)  # amounts as stored (100000 -> 100000.00)
    return company


@router.get("/{company_id}", response_model=CompanyOut)
def get_one(company: Company = Depends(get_company)) -> Company:
    return company


@router.patch("/{company_id}", response_model=CompanyOut)
def update_company(
    body: CompanyUpdate,
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    user: User = Depends(require_role()),
) -> Company:
    sent = body.model_dump(exclude_unset=True)
    changes = {
        k: v
        for k, v in sent.items()
        if (v is not None or k in _CLEARABLE) and getattr(company, k) != v
    }
    if not changes:
        return company
    before = {k: to_jsonable_python(getattr(company, k)) for k in changes}
    for key, value in changes.items():
        setattr(company, key, value)
    audit.record(
        db,
        action="company.updated",
        entity_type="company",
        entity_id=company.id,
        company_id=company.id,
        actor_id=user.id,
        data={"before": before, "after": to_jsonable_python(changes)},
    )
    db.commit()
    # Review rules and the company's own details decide where open entries go.
    vouchers.reevaluate_open(db, company)
    db.refresh(company)
    return company


# -- ledger masters -------------------------------------------------------------------


@router.post("/{company_id}/ledgers/sync", response_model=SyncResultOut)
def sync_ledgers(
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER, UserRole.PREPARER)),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ledger_sync.SyncResult:
    try:
        result = ledger_sync.sync_masters(db, company, factory(connector_url(company)), user.id)
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    vouchers.reevaluate_open(db, company)
    return result


@router.get("/{company_id}/ledgers", response_model=list[LedgerOut])
def list_ledgers(
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, max_length=100),
    parent: str | None = None,
) -> list[Ledger]:
    stmt = select(Ledger).where(Ledger.company_id == company.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Ledger.name.ilike(like), Ledger.gstin.ilike(like)))
    if parent:
        stmt = stmt.where(Ledger.parent == parent)
    return list(db.scalars(stmt.order_by(Ledger.name)))


@router.get("/{company_id}/groups", response_model=list[GroupOut])
def list_groups(
    company: Company = Depends(get_company), db: Session = Depends(get_db)
) -> list[LedgerGroup]:
    stmt = select(LedgerGroup).where(LedgerGroup.company_id == company.id)
    return list(db.scalars(stmt.order_by(LedgerGroup.name)))


@router.post("/{company_id}/ledgers", response_model=LedgerCreateResult)
def create_ledger(
    body: ProposedLedger,
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER)),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> LedgerCreateResult:
    try:
        result = posting.create_ledger(db, company, factory(connector_url(company)), body, user.id)
    except posting.PostingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return LedgerCreateResult(success=result.success, errors=result.errors)


# -- vouchers -------------------------------------------------------------------------


@router.post("/{company_id}/vouchers", response_model=PostingOutcomeOut)
def post_voucher(
    body: CanonicalTransaction,
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER)),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> posting.PostingOutcome:
    """Posts a canonical transaction directly. In later phases transactions reach this
    point through the review queue; for now it is the end-to-end connector test."""
    try:
        return posting.post_transaction(db, company, factory(connector_url(company)), body, user.id)
    except posting.PostingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
