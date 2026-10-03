from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit
from app.connectors.base import AccountingConnector
from app.models import Company, Ledger, LedgerGroup
from app.models._common import utcnow


@dataclass
class SyncResult:
    ledgers: int
    groups: int
    added: int
    removed: int


def sync_masters(
    db: Session, company: Company, connector: AccountingConnector, actor_id: int | None
) -> SyncResult:
    """Replace the local ledger/group cache with the accounting system's current masters."""
    ext_ledgers = connector.fetch_ledgers(company.external_company_name)
    ext_groups = connector.fetch_groups(company.external_company_name)

    existing = {
        led.name: led for led in db.scalars(select(Ledger).where(Ledger.company_id == company.id))
    }
    now = utcnow()
    seen: set[str] = set()
    added = 0
    for ext in ext_ledgers:
        seen.add(ext.name)
        led = existing.get(ext.name)
        if led is None:
            led = Ledger(company_id=company.id, name=ext.name)
            db.add(led)
            added += 1
        led.parent = ext.parent
        led.gstin = ext.gstin
        led.state = ext.state
        led.aliases = ext.aliases
        led.external_id = ext.external_id
        led.synced_at = now
    stale = [led for name, led in existing.items() if name not in seen]
    for led in stale:
        db.delete(led)

    db.execute(delete(LedgerGroup).where(LedgerGroup.company_id == company.id))
    db.add_all(LedgerGroup(company_id=company.id, name=g.name, parent=g.parent) for g in ext_groups)

    result = SyncResult(
        ledgers=len(ext_ledgers), groups=len(ext_groups), added=added, removed=len(stale)
    )
    audit.record(
        db,
        action="ledgers.synced",
        entity_type="company",
        entity_id=company.id,
        company_id=company.id,
        actor_id=actor_id,
        data=result.__dict__,
    )
    db.commit()
    return result
