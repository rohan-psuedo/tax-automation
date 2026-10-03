"""Invoice extraction with a fake Anthropic client: no network calls are made."""

import base64
import json
import types
from dataclasses import replace
from pathlib import Path

import anthropic
import httpx2
import pytest

from app.config import get_settings
from app.extraction import extractor
from app.extraction.extractor import (
    ExtractionError,
    ExtractionNotConfigured,
    cost_usd,
    extract_invoice,
)
from app.extraction.prompt import SYSTEM_PROMPT, build_instructions
from app.schemas.extraction import (
    ExtractedLineItem,
    ExtractedParty,
    ExtractedTotals,
    ExtractedValue,
    InvoiceExtraction,
)
from app.services import app_settings

COMPANY = "Demo Traders"
COMPANY_GSTIN = "29AAACD1234A1Z9"
PDF_BYTES = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n" + bytes(range(256)) * 4


def _value(value: str | None) -> ExtractedValue:
    return ExtractedValue(value=value, confidence="high")


SAMPLE = InvoiceExtraction(
    is_invoice=True,
    document_type="tax_invoice",
    invoice_number=_value("INV/2026-27/0042"),
    invoice_date=_value("2026-09-28"),
    seller=ExtractedParty(name=_value("Acme Supplies"), gstin=_value("29ABCDE1234F1Z5")),
    buyer=ExtractedParty(name=_value(COMPANY), gstin=_value(COMPANY_GSTIN)),
    line_items=[ExtractedLineItem(description="Widgets", taxable_value="1000.00", gst_rate="18")],
    totals=ExtractedTotals(
        taxable_value=_value("1000.00"),
        cgst=_value("90.00"),
        sgst=_value("90.00"),
        igst=_value("0"),
        cess=_value("0"),
        round_off=_value("0"),
        grand_total=_value("1180.00"),
    ),
)


def _response(
    parsed: InvoiceExtraction | None = SAMPLE,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    input_tokens: int = 1200,
    output_tokens: int = 800,
    cache_read: int | None = None,
    cache_write: int | None = None,
    stop_details: object = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=types.SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


class FakeClient:
    """Exposes .beta.messages.with_raw_response.parse(**kwargs) like the SDK; records calls
    and replays scripted results. Answers the SDK fails to validate are covered by the tests
    that use a real SDK client (_sdk_client)."""

    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.calls: list[dict] = []
        raw = types.SimpleNamespace(parse=self._parse)
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(with_raw_response=raw))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return types.SimpleNamespace(parse=lambda: result)


_REAL_CURRENT = app_settings.current
EFFECTIVE = app_settings.Effective(
    tally_url="http://127.0.0.1:9000",
    tally_url_source="env",
    anthropic_api_key=None,
    api_key_source=None,
    claude_model="claude-opus-5-5",
    claude_effort="medium",
)


@pytest.fixture(autouse=True)
def use_settings(monkeypatch: pytest.MonkeyPatch):
    """Sets the effective settings the extractor sees, e.g. use_settings(claude_effort="high")."""

    def apply(**changes) -> None:
        effective = replace(EFFECTIVE, **changes)
        monkeypatch.setattr(app_settings, "current", lambda: effective)

    apply()
    return apply


@pytest.fixture
def pdf_file(tmp_path: Path) -> Path:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)
    return path


def _pages(tmp_path: Path, count: int, suffix: str = ".png") -> list[Path]:
    pages = []
    for number in range(1, count + 1):
        page = tmp_path / f"page-{number}{suffix}"
        page.write_bytes(f"page {number} image bytes".encode() + bytes(range(64)))
        pages.append(page)
    return pages


def _extract(client: FakeClient | None, *, kind: str = "pdf", file_path: Path, **kwargs):
    return extract_invoice(
        kind=kind,
        file_path=file_path,
        page_images=kwargs.get("page_images", []),
        text=kwargs.get("text"),
        company_name=COMPANY,
        company_gstin=kwargs.get("company_gstin", COMPANY_GSTIN),
        client=client,
    )


def _content(client: FakeClient, call: int = 0) -> list[dict]:
    messages = client.calls[call]["messages"]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    return messages[0]["content"]


def _decoded(block: dict) -> bytes:
    data = block["source"]["data"]
    assert "\n" not in data
    return base64.b64decode(data, validate=True)


# Request shape


def test_pdf_request_uses_the_documented_call_shape(pdf_file: Path):
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file)

    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert set(kwargs) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "output_format",
        "output_config",
        "betas",
        "fallbacks",
    }
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_format"] is InvoiceExtraction
    assert kwargs["output_config"] == {"effort": "medium"}
    assert "format" not in kwargs["output_config"]
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]


def test_pdf_is_sent_as_a_document_block_before_the_text(pdf_file: Path):
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file)

    content = _content(client)
    assert [block["type"] for block in content] == ["document", "text"]
    document = content[0]
    assert document["source"]["type"] == "base64"
    assert document["source"]["media_type"] == "application/pdf"
    assert _decoded(document) == PDF_BYTES


def test_model_and_effort_come_from_settings(use_settings, pdf_file: Path):
    use_settings(claude_model="claude-sonnet-5-5", claude_effort="high")
    client = FakeClient(_response(model="claude-sonnet-5-5"))

    _extract(client, file_path=pdf_file)

    assert client.calls[0]["model"] == "claude-sonnet-5-5"
    assert client.calls[0]["output_config"] == {"effort": "high"}


def test_text_block_names_the_company_and_says_either_party_is_possible(pdf_file: Path):
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file)

    text = _content(client)[-1]["text"]
    assert text == build_instructions(COMPANY, COMPANY_GSTIN)
    assert COMPANY in text and COMPANY_GSTIN in text
    assert "either the seller or the buyer" in text
    assert "exactly what is printed" in text


def test_missing_company_gstin_reads_as_not_set(pdf_file: Path):
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, company_gstin=None)

    assert "(GSTIN not set)" in _content(client)[-1]["text"]


def test_system_prompt_is_stable_and_free_of_request_details(pdf_file: Path):
    client = FakeClient(_response(), _response())

    _extract(client, file_path=pdf_file)
    _extract(client, file_path=pdf_file, company_gstin=None)

    assert client.calls[0]["system"] == client.calls[1]["system"]
    assert COMPANY not in SYSTEM_PROMPT and COMPANY_GSTIN not in SYSTEM_PROMPT
    for topic in ("IGST", "CGST", "UTGST", "cess", "HSN", "reverse charge", "round_off"):
        assert topic in SYSTEM_PROMPT


# Input kinds


def test_large_pdf_falls_back_to_rendered_page_images(pdf_file: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", len(PDF_BYTES) - 1)
    pages = _pages(tmp_path, 3)
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, page_images=pages)

    content = _content(client)
    assert [block["type"] for block in content] == ["image", "image", "image", "text"]
    assert [_decoded(block) for block in content[:3]] == [p.read_bytes() for p in pages]
    assert all(block["source"]["media_type"] == "image/png" for block in content[:3])
    assert content[-1]["text"] == build_instructions(COMPANY, COMPANY_GSTIN)


def test_large_pdf_with_many_pages_sends_twenty_including_the_last(
    pdf_file: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", 10)
    pages = _pages(tmp_path, 25)
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, page_images=pages)

    content = _content(client)
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 20
    assert [_decoded(block) for block in images] == [
        p.read_bytes() for p in [*pages[:19], pages[24]]
    ]
    assert content[-1]["type"] == "text"
    assert "pages 1 to 19 and page 25 of 25" in content[-1]["text"]
    assert content[-1]["text"].endswith(build_instructions(COMPANY, COMPANY_GSTIN))


def test_large_pdf_without_page_images_is_rejected(pdf_file: Path, monkeypatch):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", 10)
    client = FakeClient()

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.retryable is False
    assert "20 MB" in exc_info.value.message
    assert client.calls == []


def test_image_sends_the_rendered_page_png(tmp_path: Path):
    original = tmp_path / "photo.jpg"
    original.write_bytes(b"\xff\xd8\xff original jpeg")
    pages = _pages(tmp_path, 1)
    client = FakeClient(_response())

    _extract(client, kind="image", file_path=original, page_images=pages)

    content = _content(client)
    assert [block["type"] for block in content] == ["image", "text"]
    assert content[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(pages[0].read_bytes()).decode(),
    }


def test_image_without_rendered_pages_sends_the_original(tmp_path: Path):
    original = tmp_path / "photo.jpg"
    original.write_bytes(b"\xff\xd8\xff original jpeg")
    client = FakeClient(_response())

    _extract(client, kind="image", file_path=original)

    image = _content(client)[0]
    assert image["source"]["media_type"] == "image/jpeg"
    assert _decoded(image) == original.read_bytes()


def test_docx_text_is_wrapped_in_document_tags(tmp_path: Path):
    docx = tmp_path / "invoice.docx"
    docx.write_bytes(b"PK zip")
    text = "TAX INVOICE\nInvoice No: INV-42\nGrand Total: 1,180.00"
    client = FakeClient(_response())

    _extract(client, kind="docx", file_path=docx, text=text)

    content = _content(client)
    assert len(content) == 1 and content[0]["type"] == "text"
    assert content[0]["text"] == (
        f"<document>\n{text}\n</document>\n\n{build_instructions(COMPANY, COMPANY_GSTIN)}"
    )


def test_docx_without_text_is_rejected(tmp_path: Path):
    docx = tmp_path / "invoice.docx"
    docx.write_bytes(b"PK zip")
    client = FakeClient()

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="docx", file_path=docx, text="  \n ")

    assert exc_info.value.retryable is False
    assert client.calls == []


def test_unsupported_kind_is_rejected(tmp_path: Path):
    sheet = tmp_path / "ledger.xlsx"
    sheet.write_bytes(b"PK zip")
    client = FakeClient()

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="sheet", file_path=sheet)

    assert exc_info.value.message == "This kind of file can't be read as an invoice."
    assert exc_info.value.retryable is False
    assert client.calls == []


def test_missing_file_is_reported(tmp_path: Path):
    client = FakeClient()

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=tmp_path / "gone.pdf")

    assert exc_info.value.retryable is False
    assert "Upload it again" in exc_info.value.message


# Responses


def test_successful_extraction_returns_outcome(pdf_file: Path):
    client = FakeClient(_response(input_tokens=1200, output_tokens=800, cache_read=3000))

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.extraction is SAMPLE
    assert outcome.model == "claude-opus-5-5"
    assert outcome.input_tokens == 1200
    assert outcome.output_tokens == 800
    assert outcome.cache_read_tokens == 3000
    assert outcome.cost_usd == cost_usd("claude-opus-5-5", 1200, 800, 3000)


def test_outcome_reports_the_model_that_served_a_fallback(pdf_file: Path):
    client = FakeClient(_response(model="claude-opus-5", input_tokens=1000, output_tokens=1000))

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.model == "claude-opus-5"
    assert outcome.cost_usd == pytest.approx(0.03)


def test_refusal_is_not_retried_and_names_the_category(pdf_file: Path):
    details = types.SimpleNamespace(type="refusal", category="cyber", explanation=None)
    client = FakeClient(_response(None, stop_reason="refusal", stop_details=details))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    error = exc_info.value
    assert error.retryable is False
    assert "declined to read this document" in error.message
    assert "cyber" in error.message
    assert "manually" in error.message
    assert len(client.calls) == 1


def test_refusal_without_details(pdf_file: Path):
    client = FakeClient(_response(None, stop_reason="refusal", stop_details=None))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.message == ("Claude declined to read this document. Enter it manually.")


def test_max_tokens_is_retried_once_with_a_larger_budget(pdf_file: Path):
    client = FakeClient(
        _response(None, stop_reason="max_tokens", input_tokens=1000, output_tokens=16000),
        _response(input_tokens=1000, output_tokens=20000),
    )

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.extraction is SAMPLE
    assert [call["max_tokens"] for call in client.calls] == [16000, 32000]
    assert client.calls[1]["messages"] == client.calls[0]["messages"]
    assert outcome.input_tokens == 2000
    assert outcome.output_tokens == 36000
    assert outcome.cost_usd == pytest.approx(cost_usd("claude-opus-5-5", 2000, 36000))


def test_max_tokens_twice_fails(pdf_file: Path):
    client = FakeClient(
        _response(None, stop_reason="max_tokens"),
        _response(None, stop_reason="max_tokens"),
    )

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.retryable is False
    assert "cut off" in exc_info.value.message
    assert len(client.calls) == 2


def test_missing_parsed_output_fails(pdf_file: Path):
    client = FakeClient(_response(None))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.retryable is False
    assert len(client.calls) == 1


# Configuration


def test_not_configured_without_any_api_key(pdf_file: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ignored-the-effective-settings-decide")

    with pytest.raises(ExtractionNotConfigured) as exc_info:
        _extract(None, file_path=pdf_file)

    error = exc_info.value
    assert isinstance(error, ExtractionError)
    assert error.retryable is False
    assert "in Settings" in error.message
    assert "ANTHROPIC_API_KEY" in error.message and "backend/.env" in error.message


def _capture_client_construction(monkeypatch) -> list[dict]:
    built: list[dict] = []

    def fake_anthropic(**kwargs):
        built.append(kwargs)
        return FakeClient(_response())

    monkeypatch.setattr(extractor.anthropic, "Anthropic", fake_anthropic)
    return built


def test_client_is_built_from_the_effective_key(use_settings, pdf_file: Path, monkeypatch):
    use_settings(anthropic_api_key="sk-ant-from-settings", api_key_source="settings")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    built = _capture_client_construction(monkeypatch)

    outcome = _extract(None, file_path=pdf_file)

    assert outcome.extraction is SAMPLE
    assert built == [{"api_key": "sk-ant-from-settings", "timeout": 180.0, "max_retries": 2}]


def test_client_falls_back_to_the_environment_key(pdf_file: Path, monkeypatch):
    # Through the real effective settings: nothing saved, so the environment applies.
    monkeypatch.setattr(app_settings, "current", _REAL_CURRENT)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    app_settings.invalidate()
    built = _capture_client_construction(monkeypatch)

    _extract(None, file_path=pdf_file)

    assert built[0]["api_key"] == "sk-ant-from-env"


# Error mapping

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls: type, status: int, message: str | None = None) -> Exception:
    body = {"type": "error", "error": {"type": "error", "message": message}} if message else None
    return cls(
        f"Error code: {status}", response=httpx2.Response(status, request=_REQUEST), body=body
    )


@pytest.mark.parametrize(
    ("error", "retryable", "expected"),
    [
        (_status_error(anthropic.AuthenticationError, 401), False, "API key was rejected"),
        (_status_error(anthropic.PermissionDeniedError, 403), False, "API key was rejected"),
        (
            _status_error(anthropic.BadRequestError, 400, "PDF pages exceed the limit"),
            False,
            "PDF pages exceed the limit",
        ),
        (
            _status_error(anthropic.NotFoundError, 404, "model: claude-nope"),
            False,
            "model: claude-nope",
        ),
        (_status_error(anthropic.RateLimitError, 429), True, "Try again"),
        (anthropic.APITimeoutError(request=_REQUEST), True, "too long"),
        (anthropic.APIConnectionError(request=_REQUEST), True, "internet connection"),
        (_status_error(anthropic.OverloadedError, 529), True, "temporarily unavailable"),
        (_status_error(anthropic.InternalServerError, 500), True, "temporarily unavailable"),
        (_status_error(anthropic.APIStatusError, 503, "upstream"), True, "upstream"),
        (_status_error(anthropic.APIStatusError, 422, "bad field"), False, "bad field"),
    ],
    ids=[
        "auth",
        "permission",
        "bad-request",
        "not-found",
        "rate-limit",
        "timeout",
        "connection",
        "overloaded",
        "server-error",
        "other-5xx",
        "other-4xx",
    ],
)
def test_sdk_errors_are_mapped(pdf_file: Path, error: Exception, retryable: bool, expected: str):
    client = FakeClient(error)

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.retryable is retryable
    assert expected in exc_info.value.message
    assert exc_info.value.__cause__ is error
    assert str(exc_info.value) == exc_info.value.message


# Cost


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5", 24.0),
        ("claude-sonnet-5-5", 12.0),
        ("claude-opus-5", 30.0),
        ("claude-opus-4-8", 30.0),
        ("claude-haiku-4-5", 6.0),
    ],
)
def test_cost_per_million_input_and_output_tokens(model: str, expected: float):
    assert cost_usd(model, 1_000_000, 1_000_000) == expected


def test_cache_reads_and_writes_are_priced_off_the_input_rate():
    # 1000 x $4 + 500 x $20 + 10000 x $0.40 + 2000 x $5, per million tokens.
    assert cost_usd("claude-opus-5-5", 1000, 500, 10_000, 2000) == 0.028


def test_unknown_model_costs_nothing():
    assert cost_usd("some-other-model", 1_000_000, 1_000_000) == 0.0


def test_suffixed_model_id_uses_the_longest_matching_price():
    assert cost_usd("claude-opus-5-5-20260901", 1_000_000, 0) == 4.0
    assert cost_usd("claude-opus-5-20260101", 1_000_000, 0) == 5.0


def test_outcome_cost_includes_cache_tokens(pdf_file: Path):
    client = FakeClient(
        _response(input_tokens=1000, output_tokens=500, cache_read=10_000, cache_write=2000)
    )

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.cost_usd == 0.028
    assert outcome.cache_read_tokens == 10_000


# Answers through the real SDK, over a local transport


def _api_message(
    text: str,
    *,
    stop_reason: str = "end_turn",
    stop_details: dict | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "Reading the document.", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def _sdk_client(*bodies: dict) -> tuple[anthropic.Anthropic, list[dict]]:
    """A real SDK client whose HTTP calls are answered locally with the given bodies."""
    sent: list[dict] = []
    replies = list(bodies)

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=replies.pop(0))

    client = anthropic.Anthropic(
        api_key="sk-ant-test",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    return client, sent


_CUT_OFF_JSON = '{"is_invoice": true, "document_ty'


def test_sdk_answer_is_parsed_into_the_extraction(pdf_file: Path):
    client, sent = _sdk_client(_api_message(SAMPLE.model_dump_json()))

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    assert outcome.model == "claude-opus-5-5"
    assert len(sent) == 1
    assert sent[0]["fallbacks"] == "default"
    assert sent[0]["output_config"]["effort"] == "medium"
    assert sent[0]["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("text", ["I can't help with this document.", _CUT_OFF_JSON])
def test_sdk_refusal_with_text_is_not_retried(pdf_file: Path, text: str):
    details = {"type": "refusal", "category": "cyber", "explanation": None}
    client, sent = _sdk_client(_api_message(text, stop_reason="refusal", stop_details=details))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.message == (
        "Claude declined to read this document (reason: cyber). Enter it manually."
    )
    assert exc_info.value.retryable is False
    assert len(sent) == 1


def test_sdk_cut_off_attempt_is_retried_and_still_counted(pdf_file: Path):
    client, sent = _sdk_client(
        _api_message(
            _CUT_OFF_JSON, stop_reason="max_tokens", input_tokens=1000, output_tokens=16000
        ),
        _api_message(SAMPLE.model_dump_json(), input_tokens=1000, output_tokens=500),
    )

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    assert [body["max_tokens"] for body in sent] == [16000, 32000]
    assert outcome.input_tokens == 2000
    assert outcome.output_tokens == 16500
    # 2000 x $4 + 16500 x $20, per million tokens.
    assert outcome.cost_usd == pytest.approx(0.338)


def test_sdk_cut_off_twice_fails(pdf_file: Path):
    client, sent = _sdk_client(
        _api_message(_CUT_OFF_JSON, stop_reason="max_tokens"),
        _api_message(_CUT_OFF_JSON, stop_reason="max_tokens"),
    )

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.retryable is False
    assert "cut off" in exc_info.value.message
    assert len(sent) == 2


def test_sdk_answer_that_does_not_fit_the_schema_is_not_retried(pdf_file: Path):
    client, sent = _sdk_client(_api_message('{"is_invoice": true}'))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert "did not return any invoice details" in exc_info.value.message
    assert exc_info.value.retryable is False
    assert len(sent) == 1


def test_request_too_large_says_to_split_the_document(pdf_file: Path):
    error = _status_error(anthropic.RequestTooLargeError, 413, "request_too_large")
    client = FakeClient(error)

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.retryable is False
    assert "too large" in exc_info.value.message and "Split it" in exc_info.value.message


# Request size of the large-PDF fallback


def _sized_pages(tmp_path: Path, *sizes: int) -> list[Path]:
    pages = []
    for number, size in enumerate(sizes, start=1):
        page = tmp_path / f"scan-{number}.png"
        page.write_bytes(bytes([number]) * size)
        pages.append(page)
    return pages


def _sent_images(client: FakeClient) -> list[bytes]:
    return [_decoded(block) for block in _content(client) if block["type"] == "image"]


def test_large_scanned_pdf_fallback_fits_the_request_size_limit(
    pdf_file: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", 10)
    pages = _sized_pages(tmp_path, *[2_500_000] * 12)
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, page_images=pages)

    request_size = len(json.dumps(client.calls[0]["messages"])) + len(SYSTEM_PROMPT)
    assert request_size < 32_000_000
    assert _sent_images(client) == [p.read_bytes() for p in [*pages[:7], pages[11]]]
    assert "pages 1 to 7 and page 12 of 12" in _content(client)[-1]["text"]
    assert "Pages 8 to 11 are left out" in _content(client)[-1]["text"]


def test_fallback_pages_that_fit_the_budget_are_all_sent(
    pdf_file: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", 10)
    monkeypatch.setattr(extractor, "MAX_IMAGE_PAYLOAD_BYTES", 400)
    pages = _sized_pages(tmp_path, 75, 75, 75)  # 100 base64 characters each
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, page_images=pages)

    assert _sent_images(client) == [p.read_bytes() for p in pages]
    assert _content(client)[-1]["text"] == build_instructions(COMPANY, COMPANY_GSTIN)


def test_fallback_drops_a_single_middle_page_over_the_budget(
    pdf_file: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", 10)
    monkeypatch.setattr(extractor, "MAX_IMAGE_PAYLOAD_BYTES", 250)
    pages = _sized_pages(tmp_path, 75, 75, 75)
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, page_images=pages)

    assert _sent_images(client) == [pages[0].read_bytes(), pages[2].read_bytes()]
    text = _content(client)[-1]["text"]
    assert "page 1 and page 3 of 3" in text and "Page 2 is left out" in text


@pytest.mark.parametrize("sizes", [(150, 150), (300,)], ids=["first-and-last", "single-page"])
def test_fallback_too_large_for_one_request_is_rejected(
    pdf_file: Path, tmp_path: Path, monkeypatch, sizes: tuple[int, ...]
):
    monkeypatch.setattr(extractor, "MAX_PDF_BYTES", 10)
    monkeypatch.setattr(extractor, "MAX_IMAGE_PAYLOAD_BYTES", 300)
    client = FakeClient()

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file, page_images=_sized_pages(tmp_path, *sizes))

    assert exc_info.value.retryable is False
    assert "too large" in exc_info.value.message
    assert client.calls == []


# Prompt rules


def _section(heading: str) -> str:
    return SYSTEM_PROMPT.split(f"# {heading}\n", 1)[1].split("\n# ", 1)[0]


def test_seller_is_the_supplier_even_when_the_buyer_issued_the_document():
    who = _section("Who is who")
    assert "seller: the supplier of the goods or services" in who
    assert "self-invoice" in who and "the issuing recipient is the buyer" in who
    instructions = build_instructions(COMPANY, COMPANY_GSTIN)
    assert "the seller is the supplier of the goods or services" in instructions
    assert "the seller is whoever issued the document" not in instructions


def test_buyer_issued_debit_note_for_a_return_is_a_credit_note():
    notes = _section("Credit and debit notes")
    assert "the seller is the supplier of the original goods or services" in notes
    assert "whoever issued the note" in notes
    assert "not by its printed title" in notes
    assert '"Debit Note" that a buyer issues to its supplier for goods it returned' in notes
    assert "the party that issued the note" not in notes


def test_cash_memo_is_a_bill_and_receipt_means_a_payment_receipt():
    types_ = _section("Document type")
    assert "cash memo or retail bill that charges GST" in types_
    assert "cash memo or retail bill without GST" in types_
    assert "receipt: a receipt for money paid or received" in types_
    assert "receipt: a cash memo" not in types_
