"""The accuracy check: compares what the AI service reads with reviewed (posted) entries or
with hand-written answer files. Uses a fake service, so it costs nothing."""

import dataclasses
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.extraction import evaluate as ev
from app.extraction import services
from app.services import app_settings
from tests import samples
from tests import test_vouchers_api as flows

fake_ai = flows.fake_ai  # fixture


@pytest.mark.parametrize(
    ("path", "expected", "got", "ok"),
    [
        ("grand_total", "11800", "11800.00", True),
        ("grand_total", "11800", "11801.00", False),
        ("cgst", "0.00", None, True),  # no tax read = 0
        ("invoice_number", "SE/2026/0042", " se/2026/0042 ", True),
        ("invoice_number", "SE/2026/0042", "SE/2026/0043", False),
        ("seller.gstin", "29ABCDE1234F1ZW", "29abcde1234f1zw", True),
        ("seller.name", "Sharma Electronics", "M/s Sharma Electronics", True),
        ("seller.name", "Sharma Electronics", "Verma Traders", False),
        ("invoice_date", "2026-09-28", "2026-09-28", True),
        ("invoice_date", "2026-09-28", "2026-28-09", False),
    ],
)
def test_field_correct(path, expected, got, ok):
    assert ev.field_correct(path, expected, got) is ok


def _fake_extract(extraction, cost_usd=0.02):
    def extract(**kwargs):
        return SimpleNamespace(
            extraction=extraction, cost_usd=cost_usd, input_tokens=1200, output_tokens=600
        )

    return extract


def _posted_case(admin_client: TestClient, company_id: int):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    doc = flows._upload_invoice(admin_client, company_id)
    voucher = flows._voucher(admin_client, doc["id"])
    assert admin_client.post(f"/api/vouchers/{voucher['id']}/post").json()["status"] == "posted"
    with SessionLocal() as db:
        cases = ev.cases_from_posted(db)
    assert len(cases) == 1 and cases[0].posted_entries
    return cases[0]


def test_reading_that_matches_the_posted_entry_scores_full_marks(
    admin_client: TestClient, company_id: int, fake_ai
):
    case = _posted_case(admin_client, company_id)
    with SessionLocal() as db:
        from app.services.vouchers import accounting_context

        report = ev.evaluate(
            [case], _fake_extract(flows.extraction()), lambda c: accounting_context(db, c.company)
        )
    d = report.as_dict()
    assert d["key_field_accuracy"] == 1.0 and d["passed"]
    assert d["vouchers_identical"] == "1/1"
    assert d["per_field"]["grand_total"] == {"right": 1, "of": 1}


def test_a_misread_total_is_counted_and_breaks_the_voucher(
    admin_client: TestClient, company_id: int, fake_ai
):
    case = _posted_case(admin_client, company_id)
    misread = flows.extraction(total="11300.00")  # e.g. an 8 read as a 3
    with SessionLocal() as db:
        from app.services.vouchers import accounting_context

        report = ev.evaluate(
            [case], _fake_extract(misread), lambda c: accounting_context(db, c.company)
        )
    d = report.as_dict()
    assert d["key_field_accuracy"] < 1.0 and not d["passed"]
    assert d["vouchers_identical"] == "0/1"
    mistakes = d["documents_with_mistakes"][0]["mistakes"]
    assert any(m.startswith("grand_total:") for m in mistakes)
    assert "voucher: this reading would not give the voucher that was posted" in mistakes
    assert "11300" in report.text() and "Claude" not in report.text()


def test_an_unreadable_document_does_not_stop_the_run(admin_client, company_id, fake_ai):
    case = _posted_case(admin_client, company_id)

    def broken(**kwargs):
        raise RuntimeError("Claude is busy")

    report = ev.evaluate([case, case], broken)
    assert report.as_dict()["unreadable"] == 2


def test_folder_answer_files(tmp_path):
    (tmp_path / "inv1.pdf").write_bytes(samples.text_pdf())
    (tmp_path / "inv1.expected.json").write_text(
        json.dumps({"invoice_number": "SE/2026/0042", "grand_total": "11800"}), encoding="utf-8"
    )
    (tmp_path / "orphan.expected.json").write_text("{}", encoding="utf-8")  # no invoice file
    cases = ev.cases_from_folder(tmp_path, "Demo Traders Pvt Ltd", None)
    assert [c.label for c in cases] == ["inv1.pdf"]
    report = ev.evaluate(cases, _fake_extract(flows.extraction()))
    assert report.per_field() == {"invoice_number": (1, 1), "grand_total": (1, 1)}


def test_nothing_is_sent_without_yes(admin_client, company_id, fake_ai, monkeypatch, capsys):
    _posted_case(admin_client, company_id)
    calls = []
    monkeypatch.setattr("app.extraction.extractor.extract_invoice", lambda **k: calls.append(k))
    assert ev.main(["--from-posted"]) == 0
    out = capsys.readouterr().out
    assert calls == [] and "Nothing was sent" in out
    assert "1 document(s) to read with Claude (claude-" in out and "roughly $" in out


def _use(monkeypatch, service_id: str) -> None:
    """The office's settings, with another AI service in use (its default model)."""
    real = app_settings.current()
    chosen = dataclasses.replace(real, ai_provider=service_id)
    monkeypatch.setattr(app_settings, "current", lambda: chosen)


def test_the_run_names_the_service_in_use(admin_client, company_id, fake_ai, monkeypatch, capsys):
    _posted_case(admin_client, company_id)
    _use(monkeypatch, "gemini")
    assert ev.main(["--from-posted"]) == 0
    out = capsys.readouterr().out
    assert "1 document(s) to read with Gemini (gemini-2.5-pro), roughly $" in out
    assert "Claude" not in out

    with pytest.raises(SystemExit):
        ev.main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--yes really send the documents to Gemini" in help_text


@pytest.mark.parametrize(
    ("service_id", "name"),
    [("openrouter", "OpenRouter"), ("groq", "Groq"), ("custom", "the AI service")],
)
def test_no_estimate_when_the_prices_are_not_known(
    admin_client, company_id, fake_ai, monkeypatch, capsys, service_id, name
):
    _posted_case(admin_client, company_id)
    _use(monkeypatch, service_id)
    assert ev.main(["--from-posted"]) == 0
    out = capsys.readouterr().out
    assert f"1 document(s) to read with {name}" in out and f"{name}'s prices" in out
    assert "The cost is not known beforehand" in out
    assert "roughly" not in out and "Claude" not in out

    with pytest.raises(SystemExit):
        ev.main(["--help"])
    assert f"really send the documents to {name}" in " ".join(capsys.readouterr().out.split())


def test_estimates_use_each_services_prices():
    by_id = services.BY_ID
    estimate = ev.estimated_cost_per_document
    # 4,000 tokens in and 1,000 out, at each model's rates.
    assert estimate(by_id["anthropic"], "claude-opus-5-5") == pytest.approx(0.036)
    assert estimate(by_id["gemini"], "gemini-2.5-flash") == pytest.approx(0.0037)
    assert estimate(by_id["openai"], "gpt-5-mini") == pytest.approx(0.003)
    assert estimate(by_id["openai"], "gpt-5-mini-2025-08-07") == pytest.approx(0.003)
    # Prices not known: no guess.
    assert estimate(by_id["anthropic"], "claude-future-9") is None
    assert estimate(by_id["gemini"], "gemini-9-ultra") is None
    assert estimate(by_id["openai"], "some-fine-tune") is None
    for service_id in ("openrouter", "groq", "xai", "deepseek", "mistral", "custom"):
        assert estimate(by_id[service_id], by_id[service_id].default_model) is None


def test_a_cost_the_service_did_not_report_is_not_shown_as_free():
    unpriced = [
        ev.CaseResult("a.pdf", {}, [], tokens=1500),
        ev.CaseResult("b.pdf", {}, [], tokens=900),
    ]
    report = ev.Report(unpriced, service="OpenRouter")
    assert "Cost: not reported by OpenRouter (2,400 tokens in all)" in report.text()
    assert "$0.00" not in report.text()
    assert report.as_dict()["documents_without_cost"] == 2
    assert report.as_dict()["total_tokens"] == 2400

    mixed = ev.Report(
        [ev.CaseResult("a.pdf", {}, [], cost_usd=0.02, tokens=1500), unpriced[1]],
        service="Gemini",
    )
    assert (
        "Cost: $0.02 in all, not counting 1 document whose cost was not reported by Gemini"
        in mixed.text()
    )

    unread = ev.Report([ev.CaseResult("c.pdf", {}, ["not read: busy"], error="busy")])
    assert "Cost: $0.00 in all" in unread.text()
    assert unread.text().startswith("Documents read by the AI service: 1 (1 could not be read)")


def test_a_reported_cost_is_added_up(admin_client, company_id, fake_ai):
    case = _posted_case(admin_client, company_id)
    report = ev.evaluate([case, case], _fake_extract(flows.extraction()), service="Gemini")
    assert "Cost: $0.04 in all" in report.text()
    assert report.as_dict()["service"] == "Gemini"
    free = ev.evaluate([case], _fake_extract(flows.extraction(), cost_usd=0.0), service="Groq")
    assert "Cost: not reported by Groq (1,800 tokens in all)" in free.text()
