from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import PathId, get_company, get_current_user, require_role
from app.models import Company, User, UserRole, Voucher, VoucherStatus
from app.pipeline.worker import worker
from app.schemas.api import BulkPostResult, RejectIn, VoucherOut, VoucherUpdate
from app.services import vouchers as svc
from app.services.connectors import ConnectorFactory, connector_url, get_connector_factory

router = APIRouter(prefix="/api", tags=["vouchers"])


def get_voucher(
    voucher_id: PathId, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Voucher:
    voucher = db.get(Voucher, voucher_id)
    if voucher is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voucher not found")
    return voucher


def _company(db: Session, voucher: Voucher) -> Company:
    company = db.get(Company, voucher.company_id)
    assert company is not None  # FK with cascade
    return company


def _conflict(exc: svc.VoucherError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/documents/{document_id}/voucher", response_model=VoucherOut)
def voucher_for_document(
    document_id: PathId, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Voucher:
    voucher = db.scalar(select(Voucher).where(Voucher.document_id == document_id))
    if voucher is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This document has no voucher")
    return voucher


@router.get("/companies/{company_id}/vouchers", response_model=list[VoucherOut])
def list_vouchers(
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    status_: list[VoucherStatus] | None = Query(default=None, alias="status"),
) -> list[Voucher]:
    stmt = select(Voucher).where(Voucher.company_id == company.id)
    if status_:
        stmt = stmt.where(Voucher.status.in_(status_))
    return list(db.scalars(stmt.order_by(Voucher.id.desc())))


@router.put("/vouchers/{voucher_id}", response_model=VoucherOut)
def update(
    body: VoucherUpdate,
    voucher: Voucher = Depends(get_voucher),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Voucher:
    try:
        svc.update_voucher(db, voucher, _company(db, voucher), body.invoice, body.choices, user)
    except svc.VoucherError as exc:
        raise _conflict(exc) from exc
    return voucher


@router.post("/vouchers/{voucher_id}/post", response_model=VoucherOut)
def post(
    voucher: Voucher = Depends(get_voucher),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER)),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> Voucher:
    company = _company(db, voucher)
    try:
        svc.post_voucher(db, voucher, company, factory(connector_url(company)), user)
    except svc.VoucherError as exc:
        raise _conflict(exc) from exc
    return voucher


@router.post("/companies/{company_id}/vouchers/post-ready", response_model=BulkPostResult)
def post_ready(
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER)),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> BulkPostResult:
    """Posts every voucher that is ready (no issues), oldest first."""
    ready = list(
        db.scalars(
            select(Voucher)
            .where(Voucher.company_id == company.id, Voucher.status == VoucherStatus.READY)
            .order_by(Voucher.id)
        )
    )
    connector = factory(connector_url(company))
    result = BulkPostResult(posted=[], failed=[])
    for voucher in ready:
        try:
            outcome = svc.post_voucher(db, voucher, company, connector, user)
        except svc.VoucherError as exc:
            result.failed.append({"voucher_id": voucher.id, "error": str(exc)})
            continue
        if outcome.success:
            result.posted.append(voucher.id)
        else:
            result.failed.append({"voucher_id": voucher.id, "error": voucher.post_error})
    return result


@router.post("/vouchers/{voucher_id}/reject", response_model=VoucherOut)
def reject(
    body: RejectIn,
    voucher: Voucher = Depends(get_voucher),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER)),
) -> Voucher:
    try:
        svc.reject_voucher(db, voucher, user, body.reason)
    except svc.VoucherError as exc:
        raise _conflict(exc) from exc
    return voucher


@router.post("/vouchers/{voucher_id}/reopen", response_model=VoucherOut)
def reopen(
    voucher: Voucher = Depends(get_voucher),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.REVIEWER)),
) -> Voucher:
    try:
        svc.reopen_voucher(db, voucher, _company(db, voucher), user)
    except svc.VoucherError as exc:
        raise _conflict(exc) from exc
    return voucher


@router.post("/vouchers/{voucher_id}/extract", response_model=VoucherOut)
def extract_again(
    voucher: Voucher = Depends(get_voucher),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Voucher:
    try:
        svc.request_extraction(db, voucher, user)
    except svc.VoucherError as exc:
        raise _conflict(exc) from exc
    worker.wake()
    return voucher
