"""End to end: upload -> read -> AI extraction (faked) -> accounting -> validation ->
review -> post to (mock) Tally."""

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.devtools.mock_tally import DEMO_COMPANY, MockTally
from app.extraction import extractor
from app.models import LedgerMapping, Voucher
from app.pipeline.worker import process_pending as process_documents
from app.schemas.extraction import InvoiceExtraction
from app.services import vouchers as voucher_service
from tests import samples

COMPANY_GSTIN = "29AAACD1234A1ZD"  # Demo Traders (Karnataka), as in conftest
SHARMA_GSTIN = "29ABCDE1234F1ZW"  # existing supplier ledger in the mock Tally
MEHTA_GSTIN = "24AAACM1234B1ZC"  # supplier not in Tally (Gujarat -> IGST)


def _value(value, confidence="high"):
    return {"value": value, "confidence": confidence, "source_text": value, "page": 1}


def extraction(
    *,
    number="SE/2026/0042",
    seller=("Sharma Electronics", SHARMA_GSTIN, "Karnataka"),
    taxable="10000.00",
    cgst="900.00",
    sgst="900.00",
    igst=None,
    total="11800.00",
) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "is_invoice": True,
            "document_type": "tax_invoice",
            "invoice_number": _value(number),
            "invoice_date": _value("2026-09-28"),
            "seller": {
                "name": _value(seller[0]),
                "gstin": _value(seller[1]),
                "address": "MG Road, Bengaluru",
                "state": seller[2],
            },
            "buyer": {
                "name": _value("Demo Traders Pvt Ltd"),
                "gstin": _value(COMPANY_GSTIN),
                "address": None,
                "state": "Karnataka",
            },
            "place_of_supply": "Karnataka",
            "reverse_charge": False,
            "line_items": [
                {
                    "description": "Network switch 24-port",
                    "hsn_sac": "8517",
                    "quantity": "2",
                    "unit": "Nos",
                    "rate": "5000",
                    "discount": None,
                    "taxable_value": taxable,
                    "gst_rate": "18",
                }
            ],
            "totals": {
                "taxable_value": _value(taxable),
                "cgst": _value(cgst),
                "sgst": _value(sgst),
                "igst": _value(igst),
                "cess": _value(None),
                "round_off": _value(None),
                "grand_total": _value(total),
            },
            "notes": [],
        }
    )


@pytest.fixture
def fake_ai(monkeypatch):
    """Replaces the Claude call; tests set state["next"] to what it should return/raise."""
    state = {"next": extraction(), "calls": []}

    def fake_extract_invoice(**kwargs):
        state["calls"].append(kwargs)
        result = state["next"]
        if isinstance(result, Exception):
            raise result
        return extractor.ExtractionOutcome(
            extraction=result,
            model="claude-opus-5-5",
            input_tokens=1200,
            output_tokens=600,
            cache_read_tokens=0,
            cost_usd=0.0168,
        )

    monkeypatch.setattr(extractor, "extract_invoice", fake_extract_invoice)
    return state


@pytest.fixture
def synced(admin_client: TestClient, company_id: int) -> int:
    assert admin_client.post(f"/api/companies/{company_id}/ledgers/sync").status_code == 200
    return company_id


def _process_all() -> None:
    with SessionLocal() as db:
        process_documents(db)
        while voucher_service.process_pending(db):
            pass


def _upload_invoice(client: TestClient, company_id: int, name="inv.pdf", pages=1) -> dict:
    # Each call builds a fresh PDF (PDFs embed timestamps), so files are never duplicates.
    resp = client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", (name, samples.text_pdf(pages=pages), "application/pdf"))],
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()["documents"][0]
    _process_all()
    return doc


def _voucher(client: TestClient, doc_id: int) -> dict:
    resp = client.get(f"/api/documents/{doc_id}/voucher")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _codes(voucher: dict) -> set[str]:
    return {i["code"] for i in voucher["issues"]}


def test_invoice_reaches_tally(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    doc = _upload_invoice(admin_client, synced)
    v = _voucher(admin_client, doc["id"])

    assert fake_ai["calls"][0]["kind"] == "pdf"
    assert fake_ai["calls"][0]["company_gstin"] == COMPANY_GSTIN
    assert v["source"] == "ai" and v["model"] == "claude-opus-5-5"
    # The company reviews everything by default, but nothing is wrong with this one.
    assert v["status"] == "needs_review"
    assert not [i for i in v["issues"] if i["severity"] == "error"], v["issues"]
    acc = v["accounting"]
    assert acc["direction"] == "purchase" and acc["voucher_kind"] == "purchase"
    assert acc["party"]["ledger"] == "Sharma Electronics" and acc["party"]["method"] == "gstin"
    entries = {(e["ledger"]["name"], e["side"], e["amount"]) for e in acc["transaction"]["entries"]}
    assert entries == {
        ("Purchase", "dr", "10000.00"),
        ("Input CGST", "dr", "900.00"),
        ("Input SGST", "dr", "900.00"),
        ("Sharma Electronics", "cr", "11800.00"),
    }

    posted = admin_client.post(f"/api/vouchers/{v['id']}/post")
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "posted"
    tally_vouchers = mock_tally.companies[DEMO_COMPANY]["vouchers"]
    assert len(tally_vouchers) == 1
    assert tally_vouchers[0]["remote_id"] == v["voucher_uid"]
    assert tally_vouchers[0]["reference"] == "SE/2026/0042"

    # Inbox shows it, the document can't be deleted any more, and the choice is learned.
    listed = admin_client.get(f"/api/companies/{synced}/documents").json()[0]
    assert listed["voucher_status"] == "posted" and listed["party_name"] == "Sharma Electronics"
    assert listed["grand_total"] == 11800.0
    assert admin_client.delete(f"/api/documents/{doc['id']}").status_code == 409
    with SessionLocal() as db:
        mapping = db.query(LedgerMapping).one()
        assert (mapping.party_ledger, mapping.item_ledger) == ("Sharma Electronics", "Purchase")

    # Posting twice is impossible.
    again = admin_client.post(f"/api/vouchers/{v['id']}/post")
    assert again.status_code == 409
    assert len(tally_vouchers) == 1


def test_new_supplier_needs_approval_then_is_created(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    fake_ai["next"] = extraction(
        number="MS/1187",
        seller=("MEHTA STEEL", MEHTA_GSTIN, "Gujarat"),
        taxable="50000.00",
        cgst=None,
        sgst=None,
        igst="9000.00",
        total="59000.00",
    )
    doc = _upload_invoice(admin_client, synced)
    v = _voucher(admin_client, doc["id"])
    assert "party_ledger_needs_approval" in _codes(v)
    proposed = v["accounting"]["proposed_party"]
    assert proposed["parent_group"] == "Sundry Creditors" and proposed["gstin"] == MEHTA_GSTIN

    blocked = admin_client.post(f"/api/vouchers/{v['id']}/post")
    assert blocked.status_code == 409
    assert "Mehta" in blocked.json()["detail"] or "MEHTA" in blocked.json()["detail"]
    assert mock_tally.companies[DEMO_COMPANY]["vouchers"] == []

    choices = {**v["choices"], "create_party_ledger": True}
    updated = admin_client.put(
        f"/api/vouchers/{v['id']}", json={"invoice": v["invoice"], "choices": choices}
    ).json()
    assert "party_ledger_needs_approval" not in _codes(updated)

    posted = admin_client.post(f"/api/vouchers/{v['id']}/post").json()
    assert posted["status"] == "posted", posted
    ledgers = mock_tally.companies[DEMO_COMPANY]["ledgers"]
    new_name = proposed["name"]
    assert ledgers[new_name]["gstin"] == MEHTA_GSTIN
    entries = mock_tally.companies[DEMO_COMPANY]["vouchers"][0]["xml"]
    assert "Input IGST" in entries and "Input CGST" not in entries


def test_manual_entry_when_ai_is_not_configured(
    admin_client: TestClient, synced: int, mock_tally: MockTally, monkeypatch
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    doc = _upload_invoice(admin_client, synced)
    v = _voucher(admin_client, doc["id"])
    assert v["status"] == "needs_review" and v["source"] == "manual"
    assert "ANTHROPIC_API_KEY" in v["extraction_error"]
    assert "missing_invoice_number" in _codes(v)
    assert admin_client.post(f"/api/vouchers/{v['id']}/post").status_code == 409

    invoice = v["invoice"] | {
        "invoice_number": "SE/2026/0099",
        "invoice_date": "2026-09-28",
        "seller": v["invoice"]["seller"] | {"name": "Sharma Electronics", "gstin": SHARMA_GSTIN},
        "buyer": v["invoice"]["buyer"] | {"name": "Demo Traders", "gstin": COMPANY_GSTIN},
        "taxable_value": "1000",
        "cgst": "90",
        "sgst": "90",
        "grand_total": "1180",
    }
    updated = admin_client.put(
        f"/api/vouchers/{v['id']}", json={"invoice": invoice, "choices": v["choices"]}
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert not [i for i in body["issues"] if i["severity"] == "error"], body["issues"]
    assert body["invoice"]["confidence"]["grand_total"] == 1.0  # typed by a person

    posted = admin_client.post(f"/api/vouchers/{v['id']}/post").json()
    assert posted["status"] == "posted"
    assert mock_tally.companies[DEMO_COMPANY]["vouchers"][0]["reference"] == "SE/2026/0099"


def test_duplicate_invoice_is_blocked(admin_client: TestClient, synced: int, fake_ai):
    first = _voucher(admin_client, _upload_invoice(admin_client, synced, "a.pdf")["id"])
    second = _voucher(admin_client, _upload_invoice(admin_client, synced, "b.pdf")["id"])
    assert "duplicate_invoice" not in _codes(first)
    assert "duplicate_invoice" in _codes(second)
    assert admin_client.post(f"/api/vouchers/{second['id']}/post").status_code == 409

    # Rejecting the second clears the way; the first can still be posted.
    assert admin_client.post(f"/api/vouchers/{second['id']}/reject", json={}).status_code == 200
    assert admin_client.post(f"/api/vouchers/{first['id']}/post").json()["status"] == "posted"


def test_bulk_post_ready(admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai):
    admin_client.patch(f"/api/companies/{synced}", json={"always_review": False})
    ids = []
    for n in range(3):
        fake_ai["next"] = extraction(number=f"SE/2026/01{n}")
        ids.append(_upload_invoice(admin_client, synced, f"inv{n}.pdf")["id"])
    assert {_voucher(admin_client, i)["status"] for i in ids} == {"ready"}

    result = admin_client.post(f"/api/companies/{synced}/vouchers/post-ready").json()
    assert len(result["posted"]) == 3 and result["failed"] == []
    assert len(mock_tally.companies[DEMO_COMPANY]["vouchers"]) == 3


def test_edits_are_rechecked(admin_client: TestClient, synced: int, fake_ai):
    v = _voucher(admin_client, _upload_invoice(admin_client, synced)["id"])
    broken = v["invoice"] | {"grand_total": "12800.00"}
    body = admin_client.put(
        f"/api/vouchers/{v['id']}", json={"invoice": broken, "choices": v["choices"]}
    ).json()
    assert "totals_mismatch" in _codes(body)
    assert admin_client.post(f"/api/vouchers/{v['id']}/post").status_code == 409

    events = admin_client.get("/api/audit", params={"entity_type": "voucher"}).json()
    edit = next(e for e in events if e["action"] == "voucher.edited")
    assert edit["data"]["changed_fields"] == ["grand_total"]


def test_preparer_cannot_post(admin_client: TestClient, synced: int, fake_ai):
    v = _voucher(admin_client, _upload_invoice(admin_client, synced)["id"])
    admin_client.post(
        "/api/users",
        json={
            "email": "p@ca.test",
            "full_name": "Prep",
            "password": "prep-pass-1",
            "role": "preparer",
        },
    )
    admin_client.cookies.clear()
    admin_client.post("/api/auth/login", json={"email": "p@ca.test", "password": "prep-pass-1"})
    assert admin_client.post(f"/api/vouchers/{v['id']}/post").status_code == 403
    # ...but can correct fields.
    resp = admin_client.put(
        f"/api/vouchers/{v['id']}", json={"invoice": v["invoice"], "choices": v["choices"]}
    )
    assert resp.status_code == 200


def test_retryable_ai_errors_are_retried_then_handed_to_a_person(
    admin_client: TestClient, synced: int, fake_ai
):
    fake_ai["next"] = extractor.ExtractionError("Claude is busy right now.", retryable=True)
    doc = _upload_invoice(admin_client, synced)
    v = _voucher(admin_client, doc["id"])
    assert len(fake_ai["calls"]) == voucher_service.MAX_EXTRACTION_ATTEMPTS
    assert v["status"] == "needs_review" and v["source"] == "manual"
    assert v["extraction_error"] == "Claude is busy right now."

    fake_ai["next"] = extraction()
    admin_client.post(f"/api/vouchers/{v['id']}/extract")
    _process_all()
    again = _voucher(admin_client, doc["id"])
    assert again["source"] == "ai" and again["extraction_error"] is None


def test_reject_and_reopen(admin_client: TestClient, synced: int, fake_ai):
    v = _voucher(admin_client, _upload_invoice(admin_client, synced)["id"])
    assert (
        admin_client.post(f"/api/vouchers/{v['id']}/reject", json={"reason": "personal"}).json()[
            "status"
        ]
        == "rejected"
    )
    assert admin_client.post(f"/api/vouchers/{v['id']}/post").status_code == 409
    assert admin_client.post(f"/api/vouchers/{v['id']}/reopen").json()["status"] == "needs_review"


def test_spreadsheets_get_no_voucher(admin_client: TestClient, synced: int, fake_ai):
    resp = admin_client.post(
        f"/api/companies/{synced}/documents",
        files=[("files", ("sales.xlsx", samples.xlsx_file(), "application/octet-stream"))],
    )
    doc = resp.json()["documents"][0]
    _process_all()
    assert admin_client.get(f"/api/documents/{doc['id']}/voucher").status_code == 404
    assert fake_ai["calls"] == []


def test_ledger_sync_refreshes_open_vouchers(
    admin_client: TestClient, synced: int, mock_tally: MockTally, fake_ai
):
    fake_ai["next"] = extraction(
        seller=("Mehta Steel", MEHTA_GSTIN, "Gujarat"), cgst=None, sgst=None, igst="1800.00"
    )
    v = _voucher(admin_client, _upload_invoice(admin_client, synced)["id"])
    assert v["accounting"]["party"]["method"] == "none"

    # Someone creates the ledger directly in Tally, then syncs.
    mock_tally.companies[DEMO_COMPANY]["ledgers"]["Mehta Steel"] = {
        "name": "Mehta Steel",
        "parent": "Sundry Creditors",
        "gstin": MEHTA_GSTIN,
        "state": "Gujarat",
    }
    admin_client.post(f"/api/companies/{synced}/ledgers/sync")
    refreshed = _voucher(admin_client, v["document_id"])
    assert refreshed["accounting"]["party"]["ledger"] == "Mehta Steel"
    assert "party_ledger_needs_approval" not in _codes(refreshed)


def test_same_number_in_a_new_financial_year_is_not_a_duplicate(
    admin_client: TestClient, synced: int, fake_ai
):
    march = extraction()
    march.invoice_date.value = "2026-03-31"  # FY 2025-26
    april = extraction()
    april.invoice_date.value = "2026-04-01"  # FY 2026-27: numbering restarts
    fake_ai["next"] = march
    _upload_invoice(admin_client, synced, "march.pdf")
    fake_ai["next"] = april
    v = _voucher(admin_client, _upload_invoice(admin_client, synced, "april.pdf")["id"])
    assert "duplicate_invoice" not in _codes(v)


def test_credit_note_with_an_invoice_number_is_not_a_duplicate(
    admin_client: TestClient, synced: int, fake_ai
):
    _upload_invoice(admin_client, synced, "invoice.pdf")
    note = extraction()
    note.document_type = "credit_note"  # notes have their own number series
    fake_ai["next"] = note
    v = _voucher(admin_client, _upload_invoice(admin_client, synced, "note.pdf")["id"])
    assert "duplicate_invoice" not in _codes(v)


def test_our_sales_numbers_are_unique_across_customers(
    admin_client: TestClient, synced: int, fake_ai
):
    def sale(customer: str):
        x = extraction(
            number="DT/2026/0007", seller=("Demo Traders Pvt Ltd", COMPANY_GSTIN, "Karnataka")
        )
        x.buyer.name.value, x.buyer.gstin.value = customer, None
        return x

    fake_ai["next"] = sale("Walk-in customer")
    first = _voucher(admin_client, _upload_invoice(admin_client, synced, "s1.pdf")["id"])
    assert first["accounting"]["direction"] == "sales"
    fake_ai["next"] = sale("Someone else")
    second = _voucher(admin_client, _upload_invoice(admin_client, synced, "s2.pdf")["id"])
    assert "duplicate_invoice" in _codes(second)


def test_documents_read_before_vouchers_existed_get_one(
    admin_client: TestClient, synced: int, fake_ai
):
    doc = _upload_invoice(admin_client, synced)
    with SessionLocal() as db:
        db.execute(Voucher.__table__.delete())  # as if read before this feature existed
        db.commit()
        assert voucher_service.queue_missing(db) == 1
        assert voucher_service.queue_missing(db) == 0  # idempotent
    _process_all()
    assert _voucher(admin_client, doc["id"])["source"] == "ai"
