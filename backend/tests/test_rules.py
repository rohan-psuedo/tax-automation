"""Company rules: the review limit, auto posting by the system, and how company settings
are saved and audited."""

from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.base import ConnectorError
from app.connectors.tally import TallyConnector
from app.db import SessionLocal
from app.devtools.mock_tally import DEMO_COMPANY, MockTally, httpx_transport
from app.main import app
from app.models import AuditEvent, Company, Voucher
from app.services import vouchers as voucher_service
from tests import test_vouchers_api as flow
from tests.test_vouchers_api import _codes, _upload_invoice, _voucher, extraction

# The invoice flow's fixtures: a faked Claude call and a company with ledgers synced.
fake_ai = flow.fake_ai
synced = flow.synced


def _events(action: str) -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)
            )
        )


def _patch(client: TestClient, company_id: int, **changes) -> dict:
    resp = client.patch(f"/api/companies/{company_id}", json=changes)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _issue(voucher: dict, code: str) -> dict:
    return next(i for i in voucher["issues"] if i["code"] == code)


def _upload(
    client: TestClient,
    company_id: int,
    ai: dict,
    number: str,
    taxable="10000",
    document_type="tax_invoice",
) -> dict:
    """Uploads an intra-state invoice from Sharma Electronics, taxed at 18%."""
    value = Decimal(taxable)
    tax = value * Decimal("0.09")
    ai["next"] = extraction(
        number=number,
        taxable=f"{value:.2f}",
        cgst=f"{tax:.2f}",
        sgst=f"{tax:.2f}",
        total=f"{value + 2 * tax:.2f}",
    )
    ai["next"].document_type = document_type
    doc = _upload_invoice(client, company_id, f"{number.replace('/', '-')}.pdf")
    return _voucher(client, doc["id"])


def _corrected(voucher: dict) -> dict:
    """An edit of an uploaded ₹11,800.00 entry to ₹23,600.00 that keeps it ready."""
    invoice = voucher["invoice"] | {
        "taxable_value": "20000.00",
        "cgst": "1800.00",
        "sgst": "1800.00",
        "grand_total": "23600.00",
    }
    line = invoice["lines"][0] | {"quantity": "4", "taxable_value": "20000.00"}
    return {"invoice": invoice | {"lines": [line]}, "choices": voucher["choices"]}


def _tally_factory(mock_tally: MockTally, urls: list[str] | None = None):
    transport = httpx_transport(mock_tally)

    def factory(url: str) -> TallyConnector:
        if urls is not None:
            urls.append(url)
        return TallyConnector(url, transport=transport)

    return factory


def _auto_post(mock_tally: MockTally, factory=None) -> int:
    with SessionLocal() as db:
        return voucher_service.auto_post_ready(db, factory or _tally_factory(mock_tally))


def _tally_refs(mock_tally: MockTally) -> list[str]:
    return [v["reference"] for v in mock_tally.companies[DEMO_COMPANY]["vouchers"]]


def _status(voucher_id: int) -> str:
    with SessionLocal() as db:
        return db.get(Voucher, voucher_id).status


def _tally_xml(mock_tally: MockTally) -> list[str]:
    return [v["xml"] for v in mock_tally.companies[DEMO_COMPANY]["vouchers"]]


class _Wrapped:
    """A connector to the mock Tally whose post_transaction a test replaces."""

    def __init__(self, inner: TallyConnector) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


# -- company settings -----------------------------------------------------------------


def test_company_created_with_rules(admin_client: TestClient):
    resp = admin_client.post(
        "/api/companies",
        json={
            "name": "Mehta Steel",
            "external_company_name": "Mehta Steel Pvt Ltd",
            "always_review": False,
            "review_above_amount": 100000,
            "auto_post": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["review_above_amount"] == "100000.00" and body["auto_post"] is True

    [event] = _events("company.created")
    assert event.data["review_above_amount"] == "100000"
    assert event.data["auto_post"] is True and event.data["always_review"] is False


def test_company_defaults(admin_client: TestClient, company_id: int):
    body = admin_client.get(f"/api/companies/{company_id}").json()
    assert body["review_above_amount"] is None and body["auto_post"] is False


def test_company_rules_are_updated_and_audited(admin_client: TestClient, company_id: int):
    body = _patch(admin_client, company_id, review_above_amount="250000.50", auto_post=True)
    assert body["review_above_amount"] == "250000.50" and body["auto_post"] is True

    body = _patch(admin_client, company_id, review_above_amount="100000")
    assert body["review_above_amount"] == "100000.00"

    # A null clears the limit; a null for a field that can't be empty changes nothing.
    body = _patch(admin_client, company_id, review_above_amount=None, name=None)
    assert body["review_above_amount"] is None and body["name"] == "Demo Traders"

    # Sending what is already saved is not a change.
    _patch(admin_client, company_id, auto_post=True, always_review=True)

    assert [e.data for e in _events("company.updated")] == [
        {
            "before": {"review_above_amount": None, "auto_post": False},
            "after": {"review_above_amount": "250000.50", "auto_post": True},
        },
        {
            "before": {"review_above_amount": "250000.50"},
            "after": {"review_above_amount": "100000"},
        },
        {"before": {"review_above_amount": "100000.00"}, "after": {"review_above_amount": None}},
    ]


def test_company_rule_validation(admin_client: TestClient, company_id: int):
    for bad in (-1, "12.345", "1000000000000000"):
        resp = admin_client.patch(f"/api/companies/{company_id}", json={"review_above_amount": bad})
        assert resp.status_code == 422, bad
    assert _events("company.updated") == []


@pytest.mark.parametrize("role", ["reviewer", "preparer"])
def test_only_admins_change_company_rules(admin_client: TestClient, company_id: int, role: str):
    admin_client.post(
        "/api/users",
        json={"email": "m@ca.test", "full_name": "M", "password": "member-pass", "role": role},
    )
    member = TestClient(app)
    member.post("/api/auth/login", json={"email": "m@ca.test", "password": "member-pass"})
    resp = member.patch(f"/api/companies/{company_id}", json={"auto_post": True})
    assert resp.status_code == 403
    created = member.post("/api/companies", json={"name": "X", "external_company_name": "X"})
    assert created.status_code == 403
    assert admin_client.get(f"/api/companies/{company_id}").json()["auto_post"] is False


# -- review limit ---------------------------------------------------------------------


def test_entries_above_the_limit_go_to_review(admin_client: TestClient, synced: int, fake_ai):
    _patch(admin_client, synced, always_review=False, review_above_amount="10000")
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0042")  # ₹11,800.00

    assert v["status"] == "needs_review"
    assert _issue(v, "above_review_limit") == {
        "code": "above_review_limit",
        "severity": "warning",
        "message": "Entries above ₹10,000.00 are always reviewed for this company.",
        "field": "grand_total",
    }
    # Only a warning: a reviewer can still post it.
    posted = admin_client.post(f"/api/vouchers/{v['id']}/post")
    assert posted.status_code == 200 and posted.json()["status"] == "posted"


def test_review_limit_message_uses_indian_grouping(admin_client: TestClient, synced: int, fake_ai):
    _patch(admin_client, synced, review_above_amount="100000")
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0500", taxable="100000")
    assert _issue(v, "above_review_limit")["message"] == (
        "Entries above ₹1,00,000.00 are always reviewed for this company."
    )


def test_entries_at_or_below_the_limit_are_ready(admin_client: TestClient, synced: int, fake_ai):
    _patch(admin_client, synced, always_review=False, review_above_amount="11800")
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0042")
    assert v["status"] == "ready" and "above_review_limit" not in _codes(v)


def test_amounts_printed_negative_are_checked_against_the_limit(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, review_above_amount="100000", auto_post=True)
    # Tally is sent ₹2,36,000.00 for these whatever the sign on the paper.
    minus = _upload(admin_client, synced, fake_ai, "SE/99", taxable="-200000")
    note = _upload(
        admin_client, synced, fake_ai, "CN/7", taxable="-200000", document_type="credit_note"
    )

    assert minus["status"] == "needs_review" and _codes(minus) == {"above_review_limit"}
    assert note["status"] == "needs_review" and "above_review_limit" in _codes(note)
    assert _auto_post(mock_tally) == 0 and _tally_refs(mock_tally) == []


def test_documents_that_are_not_invoices_get_no_limit_warning(
    admin_client: TestClient, synced: int, fake_ai
):
    _patch(admin_client, synced, review_above_amount="1000")
    v = _upload(admin_client, synced, fake_ai, "PF/1", document_type="proforma")
    assert [(i["code"], i["severity"]) for i in v["issues"]] == [("not_invoice", "error")]


def test_changing_the_limit_reroutes_open_entries(admin_client: TestClient, synced: int, fake_ai):
    _patch(admin_client, synced, always_review=False)
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0042")
    assert v["status"] == "ready"

    _patch(admin_client, synced, review_above_amount="5000")
    lowered = _voucher(admin_client, v["document_id"])
    assert lowered["status"] == "needs_review" and "above_review_limit" in _codes(lowered)

    _patch(admin_client, synced, review_above_amount=None)
    cleared = _voucher(admin_client, v["document_id"])
    assert cleared["status"] == "ready" and cleared["issues"] == []


# -- auto posting ---------------------------------------------------------------------


def test_auto_post_posts_only_ready_entries_as_the_system(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, review_above_amount="50000", auto_post=True)
    ready_a = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    big = _upload(admin_client, synced, fake_ai, "SE/2026/0102", taxable="50000")
    rejected = _upload(admin_client, synced, fake_ai, "SE/2026/0103")
    admin_client.post(f"/api/vouchers/{rejected['id']}/reject", json={})
    ready_b = _upload(admin_client, synced, fake_ai, "SE/2026/0104")
    assert [ready_a["status"], big["status"], ready_b["status"]] == [
        "ready",
        "needs_review",
        "ready",
    ]
    assert [i["code"] for i in big["issues"]] == ["above_review_limit"]

    assert _auto_post(mock_tally) == 2
    assert _tally_refs(mock_tally) == ["SE/2026/0101", "SE/2026/0104"]  # oldest first
    assert [_status(v["id"]) for v in (ready_a, big, rejected, ready_b)] == [
        "posted",
        "needs_review",
        "rejected",
        "posted",
    ]
    with SessionLocal() as db:
        assert db.get(Voucher, ready_a["id"]).posted_by is None
    posted = [e for e in _events("voucher.posted") if e.entity_type == "voucher"]
    assert [e.entity_id for e in posted] == [str(ready_a["id"]), str(ready_b["id"])]
    assert all(e.actor_id is None for e in _events("voucher.posted"))  # incl. the transaction

    assert _auto_post(mock_tally) == 0  # nothing is posted twice
    assert len(_tally_refs(mock_tally)) == 2


def test_companies_without_auto_post_are_left_alone(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False)
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    assert v["status"] == "ready"
    assert _auto_post(mock_tally) == 0
    assert _tally_refs(mock_tally) == [] and _status(v["id"]) == "ready"


def test_auto_post_rechecks_before_posting(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    with SessionLocal() as db:  # a rule changed behind the entry's back, not re-checked yet
        db.get(Company, synced).review_above_amount = Decimal("1000")
        db.commit()

    assert _auto_post(mock_tally) == 0
    assert _tally_refs(mock_tally) == []
    assert _status(v["id"]) == "needs_review"


def test_a_tally_rejection_does_not_stop_the_rest(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai, monkeypatch
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    first = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    second = _upload(admin_client, synced, fake_ai, "SE/2026/0102")
    accept = mock_tally._create_voucher

    def reject_first(company: dict, el) -> tuple[str, str | None]:
        if el.findtext("REFERENCE") == "SE/2026/0101":
            return "", "Voucher date is before the books beginning date"
        return accept(company, el)

    monkeypatch.setattr(mock_tally, "_create_voucher", reject_first)

    assert _auto_post(mock_tally) == 1
    assert _tally_refs(mock_tally) == ["SE/2026/0102"]
    failed = _voucher(admin_client, first["document_id"])
    assert failed["status"] == "post_failed"
    assert "books beginning date" in failed["post_error"]
    assert _status(second["id"]) == "posted"
    [event] = [e for e in _events("voucher.post_failed") if e.entity_type == "voucher"]
    assert event.entity_id == str(first["id"]) and event.actor_id is None

    # A failed entry waits for a person; it is not retried on every round.
    assert _auto_post(mock_tally) == 0


def test_an_unexpected_error_does_not_stop_the_rest(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    first = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    second = _upload(admin_client, synced, fake_ai, "SE/2026/0102")
    tally = _tally_factory(mock_tally)

    class FlakyConnector(_Wrapped):
        def post_transaction(self, company: str, tx):
            if tx.reference_no == "SE/2026/0101":
                raise RuntimeError("connection reset")
            return self.inner.post_transaction(company, tx)

    assert _auto_post(mock_tally, lambda url: FlakyConnector(tally(url))) == 1
    assert _tally_refs(mock_tally) == ["SE/2026/0102"]
    failed = _voucher(admin_client, first["document_id"])
    assert failed["status"] == "post_failed"
    assert failed["post_error"].startswith("Posting stopped unexpectedly. Check in Tally")
    assert _status(second["id"]) == "posted"
    # The audit trail says the system tried, so the failed entry is not a mystery.
    [event] = [e for e in _events("voucher.post_failed") if e.entity_type == "voucher"]
    assert event.entity_id == str(first["id"]) and event.actor_id is None
    assert event.data["errors"] == [failed["post_error"]]


def _closed_tally(requests: list[str]):
    """A connector factory for an office PC where TallyPrime is not running."""

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        raise httpx.ConnectError("Connection refused", request=request)

    return lambda url: TallyConnector(url, transport=httpx.MockTransport(refuse))


def test_entries_wait_while_tally_is_closed(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    ids = [_upload(admin_client, synced, fake_ai, f"SE/2026/010{n}")["id"] for n in range(3)]
    requests: list[str] = []

    assert _auto_post(mock_tally, _closed_tally(requests)) == 0
    assert len(requests) == 1  # one check for the company, not one wait per entry
    assert [_status(i) for i in ids] == ["ready"] * 3
    assert _events("voucher.post_failed") == []

    # Tally is opened again: the next round posts them.
    assert _auto_post(mock_tally) == 3
    assert _tally_refs(mock_tally) == ["SE/2026/0100", "SE/2026/0101", "SE/2026/0102"]


def test_entries_wait_while_tally_has_other_books_open(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    books = mock_tally.companies.pop(DEMO_COMPANY)
    mock_tally.add_company("Another Client Pvt Ltd", state="Karnataka")

    assert _auto_post(mock_tally) == 0
    assert _status(v["id"]) == "ready" and _events("voucher.post_failed") == []

    mock_tally.companies[DEMO_COMPANY] = books
    assert _auto_post(mock_tally) == 1 and _tally_refs(mock_tally) == ["SE/2026/0101"]


def test_losing_tally_mid_round_stops_that_company(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    first = _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    second = _upload(admin_client, synced, fake_ai, "SE/2026/0102")
    tally = _tally_factory(mock_tally)
    sent: list[str] = []

    class ClosedWhilePosting(_Wrapped):
        def post_transaction(self, company: str, tx):
            sent.append(tx.reference_no)
            raise ConnectorError("Cannot reach Tally at http://127.0.0.1:9000.")

    assert _auto_post(mock_tally, lambda url: ClosedWhilePosting(tally(url))) == 0
    assert sent == ["SE/2026/0101"]  # the rest are not sent one by one into the void
    # Tally may have saved the first before it went away, so a person checks that one.
    failed = _voucher(admin_client, first["document_id"])
    assert failed["status"] == "post_failed" and failed["post_error"].startswith("Cannot reach")
    assert _status(second["id"]) == "ready"

    assert _auto_post(mock_tally) == 1 and _tally_refs(mock_tally) == ["SE/2026/0102"]


def test_turning_auto_post_off_stops_a_running_round(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    second = _upload(admin_client, synced, fake_ai, "SE/2026/0102")
    tally = _tally_factory(mock_tally)

    class SwitchedOffMeanwhile(_Wrapped):
        def post_transaction(self, company: str, tx):
            result = self.inner.post_transaction(company, tx)
            _patch(admin_client, synced, auto_post=False)
            return result

    assert _auto_post(mock_tally, lambda url: SwitchedOffMeanwhile(tally(url))) == 1
    assert _tally_refs(mock_tally) == ["SE/2026/0101"]
    assert _status(second["id"]) == "ready"


def test_an_edit_saved_before_the_claim_is_what_gets_posted(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0101")  # ₹11,800.00
    with SessionLocal() as db:
        seen = db.get(Voucher, v["id"])  # the worker has read the entry...
        company = db.get(Company, synced)
        edit = admin_client.put(f"/api/vouchers/{v['id']}", json=_corrected(v))  # ...then this
        assert edit.status_code == 200 and edit.json()["status"] == "ready"
        connector = _tally_factory(mock_tally)("http://tally.test:9000")
        outcome = voucher_service.post_voucher(db, seen, company, connector, None, only_ready=True)
    assert outcome.success

    [xml] = _tally_xml(mock_tally)
    assert "23600" in xml and "11800" not in xml
    posted = _voucher(admin_client, v["document_id"])
    assert posted["status"] == "posted" and Decimal(str(posted["grand_total"])) == 23600


def test_an_entry_being_posted_cannot_be_edited(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai, monkeypatch
):
    _patch(admin_client, synced, always_review=False, auto_post=True)
    v = _upload(admin_client, synced, fake_ai, "SE/2026/0101")  # ₹11,800.00
    real_context = voucher_service.accounting_context
    edit: dict = {}

    def edit_while_checking(db, company):
        if not edit:  # once: when the worker checks the entry it is about to post
            edit["tried"] = True
            edit["response"] = admin_client.put(f"/api/vouchers/{v['id']}", json=_corrected(v))
        return real_context(db, company)

    monkeypatch.setattr(voucher_service, "accounting_context", edit_while_checking)
    assert _auto_post(mock_tally) == 1

    refused = edit["response"]
    assert refused.status_code == 409
    assert refused.json()["detail"] == "This voucher is being posted and can't be edited."
    # The app shows exactly what Tally holds.
    [xml] = _tally_xml(mock_tally)
    assert "11800" in xml
    posted = _voucher(admin_client, v["document_id"])
    assert posted["status"] == "posted" and Decimal(str(posted["grand_total"])) == 11800


def test_auto_post_uses_the_company_connection(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai, monkeypatch
):
    _patch(
        admin_client,
        synced,
        always_review=False,
        auto_post=True,
        connector_url="http://office-tally:9000",
    )
    _upload(admin_client, synced, fake_ai, "SE/2026/0101")
    urls: list[str] = []
    monkeypatch.setattr(
        voucher_service, "default_connector_factory", _tally_factory(mock_tally, urls)
    )
    with SessionLocal() as db:
        assert voucher_service.auto_post_ready(db) == 1
    assert urls == ["http://office-tally:9000"]
    assert _tally_refs(mock_tally) == ["SE/2026/0101"]
