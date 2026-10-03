"""IDs in URLs larger than the database can hold must give a clean 404/422, never a 500."""

import pytest
from fastapi.testclient import TestClient

HUGE = "99999999999999999999999"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", f"/api/companies/{HUGE}"),
        ("get", f"/api/companies/{HUGE}/documents"),
        ("get", f"/api/companies/{HUGE}/ledgers"),
        ("get", f"/api/documents/{HUGE}"),
        ("get", f"/api/documents/{HUGE}/voucher"),
        ("get", f"/api/documents/1/pages/{HUGE}"),
        ("get", f"/api/vouchers/{HUGE}/history"),
        ("post", f"/api/vouchers/{HUGE}/reject"),
        ("patch", f"/api/users/{HUGE}"),
        ("get", f"/api/audit?company_id={HUGE}"),
    ],
)
def test_huge_ids_are_rejected_cleanly(admin_client: TestClient, method: str, path: str):
    kwargs = {"json": {}} if method in ("post", "patch") else {}
    resp = getattr(admin_client, method)(path, **kwargs)
    assert resp.status_code in (404, 422), (path, resp.status_code, resp.text[:200])


def test_long_ledger_names_fit_the_audit_trail():
    from app.models import AuditEvent

    assert AuditEvent.__table__.c.entity_id.type.length >= 255
