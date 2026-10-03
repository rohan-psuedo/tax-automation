from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import QueryId, get_company, get_current_user, require_role
from app.models import AuditEvent, Company, User, Voucher
from app.schemas.api import ActivityPage, AuditEventOut, VoucherHistory
from app.services import audit_trail

router = APIRouter(prefix="/api", tags=["audit"])

# The database stores ids as 64-bit integers; a larger one is refused (422), not a crash.
MAX_ID = 2**63 - 1


@router.get("/audit", response_model=list[AuditEventOut])
def list_events(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    company_id: QueryId = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(default=100, le=500),
) -> list[AuditEvent]:
    stmt = select(AuditEvent)
    if company_id is not None:
        stmt = stmt.where(AuditEvent.company_id == company_id)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    return list(db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit)))


@router.get("/companies/{company_id}/activity", response_model=ActivityPage)
def company_activity(
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    entity_type: str | None = Query(default=None, max_length=64),
    action_prefix: str | None = Query(default=None, max_length=64),
    actor_id: int | None = Query(default=None, ge=1, le=MAX_ID),
    before_id: int | None = Query(default=None, ge=1, le=MAX_ID),
    limit: int = Query(default=50, ge=1, le=audit_trail.MAX_PAGE),
) -> ActivityPage:
    """What happened in this company, newest first. Pass next_before_id as before_id to
    get the next (older) page."""
    return audit_trail.company_activity(
        db,
        company.id,
        entity_type=entity_type,
        action_prefix=action_prefix,
        actor_id=actor_id,
        before_id=before_id,
        limit=limit,
    )


@router.get("/activity", response_model=ActivityPage)
def office_activity(
    db: Session = Depends(get_db),
    _: User = Depends(require_role()),
    entity_type: str | None = Query(default=None, max_length=64),
    action_prefix: str | None = Query(default=None, max_length=64),
    actor_id: int | None = Query(default=None, ge=1, le=MAX_ID),
    before_id: int | None = Query(default=None, ge=1, le=MAX_ID),
    limit: int = Query(default=50, ge=1, le=audit_trail.MAX_PAGE),
) -> ActivityPage:
    """Office-wide events that belong to no company (sign-ins, team changes, settings,
    backups), newest first. Administrators only."""
    return audit_trail.company_activity(
        db,
        None,
        entity_type=entity_type,
        action_prefix=action_prefix,
        actor_id=actor_id,
        before_id=before_id,
        limit=limit,
    )


@router.get("/vouchers/{voucher_id}/history", response_model=VoucherHistory)
def voucher_history(
    voucher_id: int = Path(ge=1, le=MAX_ID),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> VoucherHistory:
    """Everything that happened to one voucher and its document, oldest first, with each
    request sent to Tally and its response."""
    if db.get(Voucher, voucher_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This voucher does not exist. It may have been deleted together with its document.",
        )
    return audit_trail.voucher_history(db, voucher_id)
