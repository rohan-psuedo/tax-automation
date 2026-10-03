"""Activity feed and voucher history: real flows through the API (upload -> read -> AI
(faked) -> edit -> post to the mock Tally), checked the way a CA would read them."""

import json
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db import SessionLocal, engine
from app.devtools.mock_tally import DEMO_COMPANY, MockTally
from app.extraction import extractor
from app.models import AuditEvent, PostingAttempt
from app.services import audit_trail
from app.services.audit_trail import field_label, rupees
from tests import samples
from tests import test_vouchers_api as flows

PASSWORD = "s3cret-pass"
SECRET_KEY = "sk-ant-api03-THIS-IS-NOT-A-REAL-KEY-0123456789"

fake_ai = flows.fake_ai  # the Claude call, replaced (fixture)


@pytest.fixture
def office(client: TestClient) -> TestClient:
    resp = client.post(
        "/api/auth/setup",
        json={"email": "dev@ca.test", "full_name": "Dev Admin", "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    return client


@pytest.fixture
def company(office: TestClient) -> int:
    resp = office.post(
        "/api/companies",
        json={
            "name": "Demo Traders",
            "external_company_name": DEMO_COMPANY,
            "gstin": flows.COMPANY_GSTIN,
            "state": "Karnataka",
        },
    )
    assert resp.status_code == 201, resp.text
    company_id = resp.json()["id"]
    assert office.post(f"/api/companies/{company_id}/ledgers/sync").status_code == 200
    return company_id


SYNC_HINT = "Sync ledgers from Tally, then check the ledgers on this entry."


def _activity(client: TestClient, company_id: int, **params) -> dict:
    resp = client.get(f"/api/companies/{company_id}/activity", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _history(client: TestClient, voucher_id: int) -> list[dict]:
    resp = client.get(f"/api/vouchers/{voucher_id}/history")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _edit(client: TestClient, voucher: dict, invoice=None, choices=None) -> dict:
    body = {
        "invoice": voucher["invoice"] | (invoice or {}),
        "choices": voucher["choices"] | (choices or {}),
    }
    resp = client.put(f"/api/vouchers/{voucher['id']}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_raw(client: TestClient, company_id: int, name: str, data: bytes) -> dict:
    resp = client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", (name, data, "application/pdf"))],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["documents"][0]


def _my_id(client: TestClient) -> int:
    return client.get("/api/auth/me").json()["id"]


def _mehta_invoice(number="MS/1187", taxable="50000.00", igst="9000.00", total="59000.00"):
    return flows.extraction(
        number=number,
        seller=("Mehta Steel", flows.MEHTA_GSTIN, "Gujarat"),
        taxable=taxable,
        cgst=None,
        sgst=None,
        igst=igst,
        total=total,
    )


def _mehta_voucher(client: TestClient, company_id: int, fake_ai, **invoice) -> dict:
    """A Mehta Steel invoice (a supplier not in Tally) with its new ledger approved."""
    fake_ai["next"] = _mehta_invoice(**invoice)
    name = f"{invoice.get('number', 'MS/1187').replace('/', '_')}.pdf"
    v = flows._voucher(client, flows._upload_invoice(client, company_id, name)["id"])
    return _edit(client, v, choices={"create_party_ledger": True})


def _post(client: TestClient, voucher: dict) -> str:
    resp = client.post(f"/api/vouchers/{voucher['id']}/post")
    assert resp.status_code == 200, resp.text
    return resp.json()["status"]


def _story(items: list[dict]) -> list[tuple[str, str]]:
    return [(i["action"], i["summary"]) for i in items]


def test_history_traces_an_invoice_from_upload_to_tally(
    office: TestClient, company: int, mock_tally: MockTally, fake_ai
):
    fake_ai["next"] = _mehta_invoice()
    doc = flows._upload_invoice(office, company, "Mehta_Steel.pdf", pages=2)
    v = flows._voucher(office, doc["id"])
    assert v["accounting"]["proposed_party"]["name"] == "Mehta Steel"

    v = _edit(office, v, invoice={"grand_total": "59500.00"})
    v = _edit(office, v, invoice={"invoice_number": "MS/1187-A", "grand_total": "59000.00"})
    v = _edit(office, v, choices={"create_party_ledger": True})
    posted = office.post(f"/api/vouchers/{v['id']}/post")
    assert posted.status_code == 200 and posted.json()["status"] == "posted", posted.text

    resp = office.get(f"/api/vouchers/{v['id']}/history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["voucher_id"] == v["id"] and body["document_id"] == doc["id"]
    items = body["items"]
    assert [(i["action"], i["summary"]) for i in items] == [
        ("document.uploaded", "Dev Admin uploaded Mehta_Steel.pdf."),
        ("document.parsed", "The system read Mehta_Steel.pdf (2 pages)."),
        (
            "voucher.extracted",
            "Claude read the invoice (claude-opus-5-5, 1,200 + 600 tokens, about $0.02).",
        ),
        ("voucher.edited", "Dev Admin changed the grand total."),
        ("voucher.edited", "Dev Admin changed the invoice number and grand total."),
        ("voucher.edited", "Dev Admin approved a new party ledger."),
        (
            "ledger.created",
            "Dev Admin created the ledger Mehta Steel under Sundry Creditors in Tally.",
        ),
        (
            "voucher.posted",
            "Dev Admin posted invoice MS/1187-A from Mehta Steel to Tally (Tally voucher 1) "
            "and created the ledger Mehta Steel.",
        ),
    ]
    assert [i["at"] for i in items] == sorted(i["at"] for i in items)
    assert all(i["at"].endswith("Z") for i in items)  # UTC, so browsers show local time
    assert [i["actor_name"] for i in items] == ["Dev Admin", None, None] + ["Dev Admin"] * 5

    assert items[4]["changes"] == [
        {
            "field": "invoice_number",
            "label": "Invoice number",
            "before": "MS/1187",
            "after": "MS/1187-A",
        },
        {
            "field": "grand_total",
            "label": "Grand total",
            "before": "₹59,500.00",
            "after": "₹59,000.00",
        },
    ]
    assert items[5]["changes"] == [
        {
            "field": "choices.create_party_ledger",
            "label": "Create new ledger",
            "before": "No",
            "after": "Yes",
        }
    ]
    assert all(i["changes"] == [] for i in items if i["action"] != "voucher.edited")

    ledger_post, voucher_post = items[6]["posting"], items[7]["posting"]
    assert ledger_post["kind"] == "ledger" and ledger_post["reference"] == "Mehta Steel"
    assert ledger_post["success"] is True and ledger_post["error"] is None
    assert '<LEDGER NAME="Mehta Steel"' in ledger_post["request_payload"]
    assert "<CREATED>1</CREATED>" in ledger_post["response_payload"]
    assert voucher_post["kind"] == "voucher" and voucher_post["reference"] == v["voucher_uid"]
    assert voucher_post["success"] is True
    assert "MS/1187-A" in voucher_post["request_payload"]
    assert "<LASTVCHID>1</LASTVCHID>" in voucher_post["response_payload"]
    assert all(i["posting"] is None for i in items[:6])

    # A later attempt for a ledger of the same name is not part of this voucher's story.
    with SessionLocal() as db:
        db.add(
            PostingAttempt(
                company_id=company,
                kind="ledger",
                reference="Mehta Steel",
                request_payload="<ENVELOPE/>",
                response_payload=None,
                success=False,
                error="Ledger 'Mehta Steel' already exists",
            )
        )
        db.commit()
    assert len(_history(office, v["id"])) == len(items)


def test_activity_feed_links_and_describes_every_step(
    office: TestClient, company: int, mock_tally: MockTally, fake_ai
):
    fake_ai["next"] = _mehta_invoice()
    doc = flows._upload_invoice(office, company, "Mehta_Steel.pdf")
    v = flows._voucher(office, doc["id"])
    v = _edit(office, v, choices={"create_party_ledger": True})
    assert office.post(f"/api/vouchers/{v['id']}/post").json()["status"] == "posted"

    items = _activity(office, company)["items"]
    assert [(i["action"], i["entity_type"]) for i in items] == [
        ("voucher.posted", "voucher"),
        ("voucher.posted", "transaction"),  # the connector's own record of the same post
        ("ledger.created", "ledger"),
        ("voucher.edited", "voucher"),
        ("voucher.extracted", "voucher"),
        ("document.parsed", "document"),
        ("document.uploaded", "document"),
        ("ledgers.synced", "company"),
        ("company.created", "company"),
    ]
    summaries = [i["summary"] for i in items]
    assert summaries[1] == "Tally accepted invoice MS/1187 from Mehta Steel as voucher 1."
    assert summaries[3] == "Dev Admin approved a new party ledger."
    assert re.fullmatch(r"Dev Admin synced \d+ ledgers from Tally \(\d+ new\)\.", summaries[7])
    assert summaries[8] == "Dev Admin added the company Demo Traders."
    assert all(s.endswith(".") and s[0].isupper() for s in summaries)

    links = [i["document_id"] for i in items]
    assert links == [doc["id"]] * 2 + [None] + [doc["id"]] * 4 + [None, None]
    assert [i["actor_name"] for i in items] == [
        "Dev Admin",
        "Dev Admin",
        "Dev Admin",
        "Dev Admin",
        None,
        None,
        "Dev Admin",
        "Dev Admin",
        "Dev Admin",
    ]
    assert {i["company_id"] for i in items} == {company}
    assert all(i["created_at"].endswith("Z") for i in items)
    assert items[0]["data"]["ledgers_created"] == ["Mehta Steel"]


def test_failed_post_and_retry_keep_their_tally_responses(
    office: TestClient, company: int, mock_tally: MockTally, fake_ai
):
    doc = flows._upload_invoice(office, company, "Sharma_Electronics.pdf")
    v = flows._voucher(office, doc["id"])
    ledgers = mock_tally.companies[DEMO_COMPANY]["ledgers"]
    sgst = ledgers.pop("Input SGST")  # deleted in Tally after the last sync
    failed = office.post(f"/api/vouchers/{v['id']}/post")
    assert failed.status_code == 200 and failed.json()["status"] == "post_failed", failed.text
    ledgers["Input SGST"] = sgst
    assert office.post(f"/api/vouchers/{v['id']}/post").json()["status"] == "posted"

    items = _history(office, v["id"])
    posts = [i for i in items if i["action"] in ("voucher.posted", "voucher.post_failed")]
    assert [p["summary"] for p in posts] == [
        f"Posting to Tally failed: Ledger 'Input SGST' does not exist! {SYNC_HINT}",
        "Dev Admin posted invoice SE/2026/0042 from Sharma Electronics to Tally (Tally voucher 1).",
    ]
    first, second = posts[0]["posting"], posts[1]["posting"]
    assert first["success"] is False
    assert first["error"] == f"Ledger 'Input SGST' does not exist! {SYNC_HINT}"
    assert "<LINEERROR>Ledger 'Input SGST' does not exist!</LINEERROR>" in first["response_payload"]
    assert second["success"] is True and second["id"] != first["id"]
    assert all("SE/2026/0042" in p["request_payload"] for p in (first, second))
    assert not [i for i in items if i["action"].startswith("posting.")]  # all attached

    rejected = _activity(office, company, entity_type="transaction")["items"][-1]
    assert rejected["summary"] == (
        "Tally rejected invoice SE/2026/0042 from Sharma Electronics: "
        f"Ledger 'Input SGST' does not exist! {SYNC_HINT}"
    )
    assert rejected["document_id"] == doc["id"]


def test_attempts_without_a_matching_event_are_still_shown(
    office: TestClient, company: int, fake_ai
):
    v = flows._voucher(office, flows._upload_invoice(office, company)["id"])
    with SessionLocal() as db:  # e.g. the server stopped right after Tally answered
        db.add(
            PostingAttempt(
                company_id=company,
                kind="voucher",
                reference=v["voucher_uid"],
                request_payload="<ENVELOPE>request</ENVELOPE>",
                response_payload="<RESPONSE>reply</RESPONSE>",
                success=True,
                error=None,
            )
        )
        db.commit()
    last = _history(office, v["id"])[-1]
    assert last["action"] == "posting.voucher"
    assert last["summary"] == "Tally accepted invoice SE/2026/0042 from Sharma Electronics."
    assert last["posting"]["request_payload"] == "<ENVELOPE>request</ENVELOPE>"
    assert last["posting"]["response_payload"] == "<RESPONSE>reply</RESPONSE>"


def test_review_decisions_and_choices(office: TestClient, company: int, fake_ai):
    v = flows._voucher(office, flows._upload_invoice(office, company)["id"])
    v = _edit(office, v, choices={"direction": "purchase", "item_ledger": "Office Expenses"})
    rejected = office.post(f"/api/vouchers/{v['id']}/reject", json={"reason": "Personal purchase"})
    assert rejected.status_code == 200, rejected.text
    assert office.post(f"/api/vouchers/{v['id']}/reopen").status_code == 200
    assert office.post(f"/api/vouchers/{v['id']}/extract").status_code == 200
    flows._process_all()

    items = _history(office, v["id"])
    ref = "invoice SE/2026/0042 from Sharma Electronics"
    assert [i["summary"] for i in items[3:]] == [
        "Dev Admin changed the direction and item ledger.",
        f"Dev Admin rejected {ref} (reason: Personal purchase).",
        f"Dev Admin reopened {ref} for review.",
        f"Dev Admin asked the AI to read {ref} again.",
        "Claude read the invoice (claude-opus-5-5, 1,200 + 600 tokens, about $0.02).",
    ]
    assert items[3]["changes"] == [
        {
            "field": "choices.direction",
            "label": "Direction",
            "before": "Automatic",
            "after": "Purchase",
        },
        {
            "field": "choices.item_ledger",
            "label": "Item ledger",
            "before": "Automatic",
            "after": "Office Expenses",
        },
    ]


def test_extraction_problems_are_explained(office: TestClient, company: int, fake_ai):
    fake_ai["next"] = extractor.ExtractionNotConfigured()
    v = flows._voucher(office, flows._upload_invoice(office, company, "scan.pdf")["id"])
    fake_ai["next"] = extractor.ExtractionError("Claude is busy right now.")
    assert office.post(f"/api/vouchers/{v['id']}/extract").status_code == 200
    flows._process_all()

    items = _history(office, v["id"])
    assert [i["action"] for i in items] == [
        "document.uploaded",
        "document.parsed",
        "voucher.extraction_skipped",
        "voucher.reextract",
        "voucher.extraction_failed",
    ]
    # run_extraction records the service in use: Claude, as nothing else is set up here.
    assert items[2]["summary"] == (
        "The invoice was not read because Claude is not set up yet. It will be read "
        "automatically once an administrator finishes the setup in Settings."
    )
    assert items[4]["summary"] == "Claude could not read the invoice: Claude is busy right now."


def test_extraction_summaries_name_the_service_that_got_the_document(
    office: TestClient, company: int, fake_ai
):
    """The audit trail must name the third party a client's document went to."""
    v = flows._voucher(office, flows._upload_invoice(office, company)["id"])
    not_set_up = "Invoice reading is not set up because no API key was found."
    events = [
        (
            "voucher.extracted",
            {
                "service": "gemini",
                "model": "gemini-2.5-flash",
                "tokens": [1234, 567],
                "cost_usd": 0.0123,
            },
        ),
        (
            "voucher.extraction_failed",
            {"service": "gemini", "error": "Gemini's usage limit has been reached for now."},
        ),
        (
            "voucher.extracted",
            {"service": "custom", "model": "llama3.1:8b", "tokens": [900, 300], "cost_usd": 0},
        ),
        ("voucher.extraction_skipped", {"service": "custom", "error": not_set_up}),
        ("voucher.extraction_skipped", {"service": "mistral", "error": not_set_up}),
        ("voucher.extracted", {"service": "openrouter", "model": "anthropic/claude-sonnet-4.5"}),
        ("voucher.extraction_failed", {"service": "deepseek", "error": "Scans can't be read."}),
        ("voucher.extraction_skipped", {"service": "anthropic", "error": not_set_up}),
        # Recorded before the service was: only a Claude model shows who read it.
        ("voucher.extracted", {"model": "claude-opus-4-8", "tokens": [10, 5]}),
        ("voucher.extracted", {"model": "gemini-2.5-pro"}),
        ("voucher.extraction_failed", {"error": "Timed out."}),
        ("voucher.extraction_skipped", {"error": not_set_up}),
        # A service this version doesn't know is not guessed from the model either.
        ("voucher.extracted", {"service": "retired", "model": "claude-opus-5-5"}),
    ]
    _add_events(company, *((action, "voucher", v["id"], None, data) for action, data in events))

    later = [i["summary"] for i in reversed(_activity(office, company)["items"][: len(events)])]
    waits = "It will be read automatically once an administrator finishes the setup in Settings."
    assert later == [
        "Gemini read the invoice (gemini-2.5-flash, 1,234 + 567 tokens, about $0.01).",
        "Gemini could not read the invoice: Gemini's usage limit has been reached for now.",
        "The AI service read the invoice (llama3.1:8b, 900 + 300 tokens).",
        f"The invoice was not read because the AI service is not set up yet. {waits}",
        f"The invoice was not read because Mistral is not set up yet. {waits}",
        "OpenRouter read the invoice (anthropic/claude-sonnet-4.5).",
        "DeepSeek could not read the invoice: Scans can't be read.",
        f"The invoice was not read because Claude is not set up yet. {waits}",
        "Claude read the invoice (claude-opus-4-8, 10 + 5 tokens).",
        "The AI service read the invoice (gemini-2.5-pro).",
        "The AI service could not read the invoice: Timed out.",
        f"The invoice was not read because the AI service is not set up yet. {waits}",
        "The AI service read the invoice (claude-opus-5-5).",
    ]
    history = [i["summary"] for i in _history(office, v["id"])]
    assert history[-len(events) :] == later


def test_settings_labels_name_each_service(office: TestClient):
    labels = audit_trail.SETTINGS_LABELS
    assert (labels["gemini_api_key"], labels["gemini_model"]) == ("Gemini API key", "Gemini model")
    assert (labels["xai_api_key"], labels["xai_model"]) == ("xAI API key", "Grok model")
    assert (labels["custom_api_key"], labels["custom_model"]) == (
        "AI service API key",
        "AI service model",
    )
    _add_events(
        None,
        (
            "settings.updated",
            "settings",
            "office",
            _my_id(office),
            {"changed": ["ai_provider", "custom_api_key", "custom_base_url", "custom_model"]},
        ),
    )
    assert _office_activity()[0]["summary"] == (
        "Dev Admin changed the AI service, AI service API key, AI service address and AI "
        "service model in the office settings."
    )


# One of each format the AI services use. None of these is a real key.
FAKE_KEYS = {
    "anthropic": SECRET_KEY,
    "openai": "sk-proj-" + "aB3_x-" * 10,
    "openrouter": "sk-or-v1-" + "0123456789abcdef" * 4,
    "deepseek": "sk-" + "0123456789abcdef" * 2,
    "gemini": "AIzaSy" + "Bx7" * 11,
    "gemini new": "AQ.Ab8RN6" + "xY2" * 15,
    "groq": "gsk_" + "aB3" * 17 + "c",
    "xai": "xai-" + "Zq9" * 26 + "ab",
    "mistral": "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF",
}


def _public(error: str) -> str:
    event = AuditEvent(
        action="voucher.extraction_failed",
        entity_type="voucher",
        entity_id="1",
        data={"error": error},
    )
    return audit_trail.public_data(event)["error"]


@pytest.mark.parametrize("key", FAKE_KEYS.values(), ids=FAKE_KEYS.keys())
def test_every_services_api_key_is_hidden(key: str):
    # e.g. a key pasted into the model field, quoted back by the service in its error
    error = f"Mistral could not find the model {key} (Invalid model: {key})."
    assert _public(error) == "Mistral could not find the model [hidden] (Invalid model: [hidden])."
    for text in (key, f"key={key}", f"'{key}'", f"Bearer {key}", f"/v1/models/{key}"):
        assert key not in _public(text), text


@pytest.mark.parametrize(
    "value",
    [
        "SE/2026/0042",
        "INV-2026-000123456789",
        "29ABCDE1234F1ZW",
        "gpt-5-mini",
        "gpt-5-mini-2025-08-07",
        "gemini-2.5-flash",
        "claude-opus-5-5",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
        "llama3.1:8b-instruct-q4_K_M",
        "xai-grok-4-fast-reasoning",
        "task-0123456789abcdefghij",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
        "A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4",  # upper-case hex
        "3f2b9c1e-8a4d-4e6f-9b7a-1c2d3e4f5a6b",
        "Sharma Electronics Pvt Ltd, Bengaluru 560001",
    ],
)
def test_ordinary_values_are_not_hidden(value: str):
    assert _public(f"Could not read {value}.") == f"Could not read {value}."


def test_keys_are_hidden_from_summaries_too(office: TestClient, company: int, fake_ai):
    v = flows._voucher(office, flows._upload_invoice(office, company)["id"])
    mistral = FAKE_KEYS["mistral"]
    _add_events(
        company,
        (
            "voucher.extraction_failed",
            "voucher",
            v["id"],
            None,
            {"service": "mistral", "error": f"Mistral could not find the model {mistral}."},
        ),
    )
    latest = _activity(office, company, limit=1)["items"][0]
    assert latest["summary"] == (
        "Mistral could not read the invoice: Mistral could not find the model [hidden]."
    )
    assert mistral not in json.dumps(latest)
    assert mistral not in office.get(f"/api/vouchers/{v['id']}/history").text


def test_activity_pages_newest_first(office: TestClient, company: int, fake_ai):
    for n in range(3):
        fake_ai["next"] = flows.extraction(number=f"SE/2026/01{n}")
        flows._upload_invoice(office, company, f"inv{n}.pdf")

    everything = _activity(office, company, limit=200)
    ids = [i["id"] for i in everything["items"]]
    assert len(ids) == 2 + 3 * 3  # company + sync, then upload/read/extract per invoice
    assert ids == sorted(ids, reverse=True) and everything["next_before_id"] is None
    assert len(_activity(office, company)["items"]) == len(ids)  # default limit is 50
    assert _activity(office, company, limit=len(ids))["next_before_id"] is None

    pages, before = [], None
    while True:
        params = {"limit": 4} | ({"before_id": before} if before else {})
        page = _activity(office, company, **params)
        pages.append([i["id"] for i in page["items"]])
        before = page["next_before_id"]
        if before is None:
            break
        assert before == page["items"][-1]["id"]
    assert [len(p) for p in pages] == [4, 4, 3]
    assert [i for p in pages for i in p] == ids


def test_activity_filters(office: TestClient, company: int, fake_ai):
    for n in range(2):
        fake_ai["next"] = flows.extraction(number=f"SE/2026/02{n}")
        flows._upload_invoice(office, company, f"inv{n}.pdf")
    me = _my_id(office)

    documents = _activity(office, company, entity_type="document")["items"]
    assert len(documents) == 4 and {i["entity_type"] for i in documents} == {"document"}
    extracted = _activity(office, company, action_prefix="voucher.")["items"]
    assert [i["action"] for i in extracted] == ["voucher.extracted"] * 2
    # "_" is a literal character in the prefix, not a wildcard.
    assert _activity(office, company, action_prefix="document_")["items"] == []
    mine = _activity(office, company, actor_id=me)["items"]
    assert len(mine) == 4 and {i["actor_name"] for i in mine} == {"Dev Admin"}
    uploads = _activity(office, company, actor_id=me, entity_type="document")["items"]
    assert [i["summary"] for i in uploads] == [
        "Dev Admin uploaded inv1.pdf.",
        "Dev Admin uploaded inv0.pdf.",
    ]
    assert (
        _activity(office, company, entity_type="")["items"] == _activity(office, company)["items"]
    )

    for bad in (0, 201, "many"):
        resp = office.get(f"/api/companies/{company}/activity", params={"limit": bad})
        assert resp.status_code == 422, (bad, resp.text)

    other = office.post(
        "/api/companies", json={"name": "Other Co", "external_company_name": "Other Co"}
    ).json()
    assert [i["summary"] for i in _activity(office, other["id"])["items"]] == [
        "Dev Admin added the company Other Co."
    ]


def test_unknown_ids_and_access(office: TestClient, company: int, fake_ai):
    v = flows._voucher(office, flows._upload_invoice(office, company)["id"])
    missing = office.get("/api/companies/9999/activity")
    assert missing.status_code == 404
    gone = office.get("/api/vouchers/9999/history")
    assert gone.status_code == 404 and gone.json()["detail"].endswith(".")

    office.post(
        "/api/users",
        json={
            "email": "prep@ca.test",
            "full_name": "Priya Shah",
            "password": "prep-pass-1",
            "role": "preparer",
        },
    )
    office.cookies.clear()
    assert office.get(f"/api/companies/{company}/activity").status_code == 401
    assert office.get(f"/api/vouchers/{v['id']}/history").status_code == 401
    office.post("/api/auth/login", json={"email": "prep@ca.test", "password": "prep-pass-1"})
    assert office.get(f"/api/companies/{company}/activity").status_code == 200
    assert office.get(f"/api/vouchers/{v['id']}/history").status_code == 200


def test_duplicate_and_deleted_documents(office: TestClient, company: int):
    pdf = samples.text_pdf()  # PDFs embed timestamps, so build the bytes once
    first = _upload_raw(office, company, "a.pdf", pdf)
    copy = _upload_raw(office, company, "copy of a.pdf", pdf)
    assert office.delete(f"/api/documents/{copy['id']}").status_code == 204

    deleted, duplicate, uploaded = _activity(office, company, entity_type="document")["items"]
    assert deleted["summary"] == "Dev Admin deleted copy of a.pdf."
    assert duplicate["summary"] == (
        "Dev Admin uploaded copy of a.pdf, which is a copy of a.pdf, so it was not read again."
    )
    assert uploaded["summary"] == "Dev Admin uploaded a.pdf."
    # No link to a file that no longer exists.
    assert (deleted["document_id"], duplicate["document_id"]) == (None, None)
    assert uploaded["document_id"] == first["id"]


def _add_events(company_id: int | None, *events: tuple) -> None:
    with SessionLocal() as db:
        for action, entity_type, entity_id, actor_id, data in events:
            db.add(
                AuditEvent(
                    action=action,
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    actor_id=actor_id,
                    company_id=company_id,
                    data=data,
                )
            )
        db.commit()


def _office_activity(**params) -> list[dict]:
    with SessionLocal() as db:
        page = audit_trail.company_activity(db, None, **params)
    return [item.model_dump(mode="json") for item in page.items]


def test_office_events_from_their_real_writers(office: TestClient, company: int):
    """Team, settings and backup events belong to no company. They stay out of the company
    feed and make up the office-wide one."""
    me = _my_id(office)
    priya = office.post(
        "/api/users",
        json={"email": "priya@ca.test", "full_name": "Priya Shah", "password": "prep-pass-1"},
    ).json()["id"]
    for change in ({"role": "reviewer"}, {"full_name": "Priya Mehta"}, {"is_active": False}):
        resp = office.patch(f"/api/users/{priya}", json=change)
        assert resp.status_code == 200, resp.text
    resp = office.post(f"/api/users/{priya}/password", json={"password": "reset-pass-1"})
    assert resp.status_code == 204, resp.text
    resp = office.post(
        "/api/auth/password", json={"current_password": PASSWORD, "new_password": "newer-pass-1"}
    )
    assert resp.status_code == 204, resp.text
    resp = office.put(
        "/api/settings",
        json={"anthropic_api_key": SECRET_KEY, "claude_model": "claude-sonnet-5-5"},
    )
    assert resp.status_code == 200, resp.text
    backup = "app-20261003-020000-scheduled.db"
    _add_events(
        None,
        # As services/backups.py (the schedule) and api/backups.py write them.
        (
            "backup.created",
            "backup",
            backup,
            None,
            {
                "reason": "scheduled",
                "size_bytes": 2048,
                "pruned": ["app-20260901-020000-scheduled.db"],
            },
        ),
        ("backup.downloaded", "backup", backup, me, {"size_bytes": 2048}),
        # A careless writer that logs the key itself must not leak it.
        (
            "settings.updated",
            "settings",
            "office",
            me,
            {"changed": ["anthropic_api_key"], "anthropic_api_key": SECRET_KEY},
        ),
    )

    items = _office_activity(limit=200)
    assert [i["summary"] for i in reversed(items)] == [
        "Dev Admin set up this office and became its first administrator.",
        "Dev Admin added Priya Mehta to the team as a preparer.",
        "Dev Admin made Priya Mehta a reviewer.",
        "Dev Admin renamed Priya Shah to Priya Mehta.",
        "Dev Admin deactivated the account of Priya Mehta.",
        "Dev Admin set a new password for Priya Mehta.",
        "Dev Admin changed their password.",
        "Dev Admin changed the Claude API key and Claude model in the office settings.",
        f"The system made the scheduled backup ({backup}) and removed 1 older backup.",
        f"Dev Admin downloaded the backup {backup}.",
        "Dev Admin changed the Claude API key in the office settings.",
    ]
    assert all(i["company_id"] is None and i["document_id"] is None for i in items)
    settings = [i["data"] for i in items if i["action"] == "settings.updated"]
    assert settings == [
        {"changed": ["anthropic_api_key"]},
        {"changed": ["anthropic_api_key", "claude_model"]},
    ]
    everything = json.dumps(items)
    for secret in (SECRET_KEY, "sk-ant-", PASSWORD, "prep-pass-1", "reset-pass-1", "newer-pass-1"):
        assert secret not in everything, secret

    company_feed = _activity(office, company, limit=200)["items"]
    assert [i["action"] for i in company_feed] == ["ledgers.synced", "company.created"]


def test_system_posts_unknown_actions_and_no_secrets(office: TestClient, company: int, fake_ai):
    v = flows._voucher(office, flows._upload_invoice(office, company)["id"])
    me = _my_id(office)
    _add_events(
        company,
        # As vouchers.post_voucher writes it when auto-posting (no person involved).
        (
            "voucher.posted",
            "voucher",
            v["id"],
            None,
            {
                "voucher_uid": v["voucher_uid"],
                "ledgers_created": [],
                "errors": [],
                "external_id": "7",
            },
        ),
        (
            "widget.frobbed",
            "widget",
            7,
            me,
            {"api_key": SECRET_KEY, "password": "hunter2-hunter2", "note": f"key {SECRET_KEY}"},
        ),
    )

    items = _activity(office, company, limit=2)["items"]
    assert [i["summary"] for i in reversed(items)] == [
        "The system posted invoice SE/2026/0042 from Sharma Electronics to Tally (Tally voucher 7).",
        "Widget frobbed by Dev Admin (widget 7).",
    ]
    assert items[1]["actor_name"] is None and items[1]["document_id"] == v["document_id"]
    assert items[0]["data"] == {"note": "key [hidden]"}

    everything = office.get(f"/api/companies/{company}/activity", params={"limit": 200}).text
    for secret in (SECRET_KEY, "sk-ant-", "hunter2", PASSWORD, "argon2", "$2b$"):
        assert secret not in everything, secret
    history = office.get(f"/api/vouchers/{v['id']}/history").text
    assert SECRET_KEY not in history and PASSWORD not in history


def test_names_are_resolved_per_page_not_per_item(office: TestClient, company: int, fake_ai):
    for n in range(4):
        fake_ai["next"] = flows.extraction(number=f"SE/2026/03{n}")
        doc = flows._upload_invoice(office, company, f"inv{n}.pdf")
        v = flows._voucher(office, doc["id"])
        _edit(office, v, invoice={"invoice_number": f"SE/2026/04{n}"})

    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        page = _activity(office, company, limit=200)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(page["items"]) == 2 + 4 * 4
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # login user + company + events + users + documents + companies + vouchers
    assert len(selects) <= 8, selects


def test_company_rule_changes(office: TestClient, company: int):
    resp = office.patch(
        f"/api/companies/{company}", json={"review_above_amount": "50000", "auto_post": True}
    )
    assert resp.status_code == 200, resp.text
    latest = _activity(office, company, action_prefix="company.")["items"][0]
    assert latest["summary"] == (
        "Dev Admin updated Demo Traders: required review for entries above ₹50,000.00 "
        "and turned on automatic posting."
    )


def test_labels_and_rupees():
    assert field_label("invoice_number") == "Invoice number"
    assert field_label("seller.gstin") == "Seller GSTIN"
    assert field_label("buyer.gstin") == "Buyer GSTIN"
    assert field_label("grand_total") == "Grand total"
    assert field_label("seller.state_code") == "Seller state code"
    assert field_label("lines.hsn_sac") == "Lines HSN SAC"
    assert rupees(Decimal("123456")) == "₹1,23,456.00"
    assert rupees("12345678.9") == "₹1,23,45,678.90"
    assert rupees("999") == "₹999.00"
    assert rupees("-0.4") == "-₹0.40"


def test_a_ledger_made_in_tally_since_the_last_sync_is_left_alone(
    office: TestClient, company: int, mock_tally: MockTally, fake_ai
):
    v = _mehta_voucher(office, company, fake_ai)
    # Someone made the ledger by hand in Tally after the last sync. Creating it again would
    # change that ledger (Tally treats Create on a name in use as Alter), so nothing is sent.
    ledgers = mock_tally.companies[DEMO_COMPANY]["ledgers"]
    by_hand = {
        "name": "mehta steel",  # Tally compares names without case
        "parent": "Sundry Creditors",
        "gstin": flows.MEHTA_GSTIN,
        "state": "Gujarat",
    }
    ledgers["mehta steel"] = dict(by_hand)
    assert _post(office, v) == "post_failed"
    assert ledgers["mehta steel"] == by_hand
    assert mock_tally.companies[DEMO_COMPANY]["vouchers"] == []

    failed = _history(office, v["id"])
    assert _story(failed[4:]) == [
        (
            "voucher.post_failed",
            "Posting to Tally failed: Tally already has a ledger named 'Mehta Steel'. Sync "
            "ledgers from Tally, then choose it on this entry instead of creating a new one.",
        ),
    ]
    assert failed[4]["posting"] is None
    ledgers["Mehta Steel"] = ledgers.pop("mehta steel") | {"name": "Mehta Steel"}

    # The way out the error suggests: sync, then post with the ledger that is now in Tally.
    # Both change what the voucher proposes, but not what already happened.
    assert office.post(f"/api/companies/{company}/ledgers/sync").status_code == 200
    assert _history(office, v["id"]) == failed
    v = _edit(
        office,
        flows._voucher(office, v["document_id"]),
        choices={"create_party_ledger": False, "party_ledger": "Mehta Steel"},
    )
    assert _post(office, v) == "posted"
    items = _history(office, v["id"])
    assert items[: len(failed)] == failed
    assert [i["action"] for i in items[len(failed) :]] == ["voucher.edited", "voucher.posted"]


def test_ledger_attempts_belong_to_the_posting_that_made_them(
    office: TestClient, company: int, mock_tally: MockTally, fake_ai
):
    a = _mehta_voucher(office, company, fake_ai)
    b = _mehta_voucher(
        office,
        company,
        fake_ai,
        number="MS/1200",
        taxable="40000.00",
        igst="7200.00",
        total="47200.00",
    )
    groups = mock_tally.companies[DEMO_COMPANY]["groups"]
    creditors = groups.pop("Sundry Creditors")
    # The same person, the same failure, one voucher after the other.
    assert [_post(office, v) for v in (a, b)] == ["post_failed", "post_failed"]
    groups["Sundry Creditors"] = creditors
    assert _post(office, b) == "posted"  # creates the ledger
    assert _post(office, a) == "posted"  # finds it already there

    refused = (
        "ledger.create_failed",
        "Creating the ledger Mehta Steel in Tally failed: Group 'Sundry Creditors' does not exist.",
    )
    post_failed = (
        "voucher.post_failed",
        "Posting to Tally failed: Group 'Sundry Creditors' does not exist.",
    )
    history_a, history_b = _history(office, a["id"]), _history(office, b["id"])
    assert _story(history_a)[0] == ("document.uploaded", "Dev Admin uploaded MS_1187.pdf.")
    assert _story(history_a)[4:] == [
        refused,
        post_failed,
        (
            "voucher.posted",
            "Dev Admin posted invoice MS/1187 from Mehta Steel to Tally (Tally voucher 2).",
        ),
    ]
    assert _story(history_b)[0] == ("document.uploaded", "Dev Admin uploaded MS_1200.pdf.")
    assert _story(history_b)[4:] == [
        refused,
        post_failed,
        (
            "ledger.created",
            "Dev Admin created the ledger Mehta Steel under Sundry Creditors in Tally.",
        ),
        (
            "voucher.posted",
            "Dev Admin posted invoice MS/1200 from Mehta Steel to Tally (Tally voucher 1) "
            "and created the ledger Mehta Steel.",
        ),
    ]
    ledger_posts = [
        [i["posting"] for i in h if i["action"].startswith("ledger.")]
        for h in (history_a, history_b)
    ]
    assert [[(p["success"], p["reference"]) for p in posts] for posts in ledger_posts] == [
        [(False, "Mehta Steel")],
        [(False, "Mehta Steel"), (True, "Mehta Steel")],
    ]
    ids = [p["id"] for posts in ledger_posts for p in posts]
    assert len(set(ids)) == 3  # each Tally request is shown once, with its own voucher


def test_out_of_range_ids_are_refused(office: TestClient, company: int):
    huge = 2**70
    for params in ({"before_id": huge}, {"actor_id": huge}, {"before_id": 0}):
        resp = office.get(f"/api/companies/{company}/activity", params=params)
        assert resp.status_code == 422, (params, resp.text)
    assert office.get(f"/api/vouchers/{huge}/history").status_code == 422
    assert office.get(f"/api/vouchers/{2**63 - 1}/history").status_code == 404


def test_deleted_documents_keep_their_names(office: TestClient, company: int, fake_ai):
    doc = flows._upload_invoice(office, company, "Sharma_Electronics.pdf")
    pdf = samples.text_pdf()
    original = _upload_raw(office, company, "a.pdf", pdf)
    copy = _upload_raw(office, company, "copy of a.pdf", pdf)
    for gone in (doc, original):
        assert office.delete(f"/api/documents/{gone['id']}").status_code == 204

    items = list(reversed(_activity(office, company, entity_type="document")["items"]))
    assert [i["summary"] for i in items] == [
        "Dev Admin uploaded Sharma_Electronics.pdf.",
        "The system read Sharma_Electronics.pdf (1 page).",
        "Dev Admin uploaded a.pdf.",
        "Dev Admin uploaded copy of a.pdf, which is a copy of a.pdf, so it was not read again.",
        "Dev Admin deleted Sharma_Electronics.pdf.",
        "Dev Admin deleted a.pdf.",
    ]
    # The names come back, the links to files that are gone do not.
    assert [i["document_id"] for i in items] == [None, None, None, copy["id"], None, None]


def test_events_of_an_earlier_document_with_the_same_id_are_kept_apart(
    admin_client: TestClient, company_id: int, fake_ai
):
    """Before 2026-10-02 a deleted document's id could be given to a new upload. The old
    file's events must not show up as, or be named after, the new document."""
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import AuditEvent, Document

    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    doc = flows._upload_invoice(admin_client, company_id)
    with SessionLocal() as db:
        created = db.get(Document, doc["id"]).created_at
        for minutes, action, data in (
            (30, "document.uploaded", {"filename": "old_statement.csv"}),
            (29, "document.parsed", {"pages": 0}),
            (28, "document.deleted", {"filename": "old_statement.csv"}),
        ):
            db.add(
                AuditEvent(
                    action=action,
                    entity_type="document",
                    entity_id=str(doc["id"]),
                    company_id=company_id,
                    actor_id=None,
                    data=data,
                    created_at=created - timedelta(minutes=minutes),
                )
            )
        db.commit()

    voucher = admin_client.get(f"/api/documents/{doc['id']}/voucher").json()
    history = admin_client.get(f"/api/vouchers/{voucher['id']}/history").json()["items"]
    assert not any("old_statement" in i["summary"] for i in history)
    assert len([i for i in history if i["action"] == "document.uploaded"]) == 1

    feed = admin_client.get(f"/api/companies/{company_id}/activity").json()["items"]
    old = [i for i in feed if i["entity_type"] == "document" and i["created_at"] < history[0]["at"]]
    assert {i["action"] for i in old} == {
        "document.uploaded",
        "document.parsed",
        "document.deleted",
    }
    assert all(i["document_id"] is None for i in old)  # no link to the new document
    parsed = next(i for i in old if i["action"] == "document.parsed")
    assert doc["original_filename"] not in parsed["summary"]  # not named after the new file
