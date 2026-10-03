"""End-to-end flow through the API against the mock Tally:
setup -> company -> Tally status -> ledger sync -> post voucher (with a new ledger) -> audit."""

from fastapi.testclient import TestClient

from app.devtools.mock_tally import DEMO_COMPANY, MockTally
from app.schemas.canonical import ProposedLedger
from tests.factories import purchase_tx

SYNC_HINT = "Sync ledgers from Tally, then check the ledgers on this entry."


def _tx_json(**kwargs) -> dict:
    return purchase_tx(**kwargs).model_dump(mode="json")


def test_setup_only_once_and_login(client: TestClient):
    assert client.get("/api/auth/setup").json() == {"needs_setup": True}
    body = {"email": "Admin@CA.test", "full_name": "Admin", "password": "s3cret-pass"}
    assert client.post("/api/auth/setup", json=body).status_code == 201
    assert client.post("/api/auth/setup", json=body).status_code == 409

    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401
    bad = client.post("/api/auth/login", json={"email": "admin@ca.test", "password": "wrong-pass"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/login", json={"email": "admin@ca.test", "password": "s3cret-pass"})
    assert ok.status_code == 200
    assert client.get("/api/auth/me").json()["role"] == "admin"


def test_tally_status_and_companies(admin_client: TestClient):
    status = admin_client.get("/api/connectors/tally/status").json()
    assert status["ok"] is True
    names = [c["name"] for c in admin_client.get("/api/connectors/tally/companies").json()]
    assert names == [DEMO_COMPANY]


def test_sync_ledgers(admin_client: TestClient, company_id: int):
    result = admin_client.post(f"/api/companies/{company_id}/ledgers/sync").json()
    assert result["ledgers"] == 14 and result["added"] == 14 and result["groups"] == 15

    found = admin_client.get(f"/api/companies/{company_id}/ledgers", params={"q": "29ABCDE"}).json()
    assert [lg["name"] for lg in found] == ["Sharma Electronics"]
    assert found[0]["parent"] == "Sundry Creditors"
    assert found[0]["aliases"] == ["Sharma Elec"]

    again = admin_client.post(f"/api/companies/{company_id}/ledgers/sync").json()
    assert again["added"] == 0 and again["removed"] == 0


def test_post_voucher_to_existing_party(
    admin_client: TestClient, company_id: int, mock_tally: MockTally
):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    tx = _tx_json()
    resp = admin_client.post(f"/api/companies/{company_id}/vouchers", json=tx)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    vouchers = mock_tally.companies[DEMO_COMPANY]["vouchers"]
    assert len(vouchers) == 1 and vouchers[0]["remote_id"] == tx["id"]

    # Idempotency: the same transaction can never be posted twice.
    dup = admin_client.post(f"/api/companies/{company_id}/vouchers", json=tx)
    assert dup.status_code == 409 and "already posted" in dup.json()["detail"]
    assert len(vouchers) == 1


def test_post_voucher_creates_new_party_ledger_first(
    admin_client: TestClient, company_id: int, mock_tally: MockTally
):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    proposed = ProposedLedger(
        name="Mehta Steel",
        parent_group="Sundry Creditors",
        gstin="29AAACM1234B1Z2",
        state="Karnataka",
        bill_wise=True,
    )
    resp = admin_client.post(
        f"/api/companies/{company_id}/vouchers",
        json=_tx_json(party="Mehta Steel", proposed=proposed),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "success": True,
        "ledgers_created": ["Mehta Steel"],
        "errors": [],
        "external_id": "1",
    }
    assert "Mehta Steel" in mock_tally.companies[DEMO_COMPANY]["ledgers"]
    local = admin_client.get(f"/api/companies/{company_id}/ledgers", params={"q": "Mehta"}).json()
    assert local[0]["gstin"] == "29AAACM1234B1Z2"

    events = admin_client.get("/api/audit", params={"company_id": company_id}).json()
    actions = [e["action"] for e in events]
    assert actions[:2] == ["voucher.posted", "ledger.created"]


def test_unknown_ledger_is_rejected_before_calling_tally(
    admin_client: TestClient, company_id: int, mock_tally: MockTally
):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    resp = admin_client.post(
        f"/api/companies/{company_id}/vouchers", json=_tx_json(party="Nobody & Co")
    )
    assert resp.status_code == 409
    assert "Nobody & Co" in resp.json()["detail"]
    assert mock_tally.companies[DEMO_COMPANY]["vouchers"] == []


def test_tally_side_rejection_is_reported(
    admin_client: TestClient, company_id: int, mock_tally: MockTally
):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    # Ledger exists in our cache but was deleted in Tally since the last sync.
    del mock_tally.companies[DEMO_COMPANY]["ledgers"]["Input SGST"]
    resp = admin_client.post(f"/api/companies/{company_id}/vouchers", json=_tx_json())
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["errors"] == [f"Ledger 'Input SGST' does not exist! {SYNC_HINT}"]


def test_unknown_tally_company_gives_502(admin_client: TestClient):
    cid = admin_client.post(
        "/api/companies", json={"name": "Ghost", "external_company_name": "Not In Tally"}
    ).json()["id"]
    resp = admin_client.post(f"/api/companies/{cid}/ledgers/sync")
    assert resp.status_code == 502
    assert "SVCurrentCompany" in resp.json()["detail"]


def test_role_permissions(admin_client: TestClient, company_id: int):
    admin_client.post(
        "/api/users",
        json={
            "email": "prep@ca.test",
            "full_name": "Preparer",
            "password": "prep-pass-123",
            "role": "preparer",
        },
    )
    admin_client.post("/api/auth/logout")
    admin_client.cookies.clear()
    admin_client.post(
        "/api/auth/login", json={"email": "prep@ca.test", "password": "prep-pass-123"}
    )

    assert admin_client.post(f"/api/companies/{company_id}/ledgers/sync").status_code == 200
    resp = admin_client.post(f"/api/companies/{company_id}/vouchers", json=_tx_json())
    assert resp.status_code == 403
    assert (
        admin_client.post(
            "/api/companies", json={"name": "x", "external_company_name": "x"}
        ).status_code
        == 403
    )


def test_tally_endpoints_ignore_a_caller_supplied_address(admin_client: TestClient, mock_tally):
    """The server must only ever contact the configured Tally, never an address a user sends
    (that would let any logged-in user make the server connect anywhere)."""
    contacted: list[str] = []
    from app.connectors.tally import TallyConnector
    from app.devtools.mock_tally import httpx_transport
    from app.main import app
    from app.services.connectors import get_connector_factory

    def spy(url: str):
        contacted.append(url)
        return TallyConnector(url, transport=httpx_transport(mock_tally))

    app.dependency_overrides[get_connector_factory] = lambda: spy
    for path in ("/api/connectors/tally/status", "/api/connectors/tally/companies"):
        assert (
            admin_client.get(path, params={"url": "http://169.254.169.254/latest"}).status_code
            == 200
        )
    assert contacted and all("169.254" not in u for u in contacted), contacted
