from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | int,
    actor_id: int | None = None,
    company_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event. The caller owns the transaction (commit)."""
    event = AuditEvent(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_id=actor_id,
        company_id=company_id,
        data=data or {},
    )
    db.add(event)
    return event
