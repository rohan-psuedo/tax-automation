"""The OpenAI adapter, with fake clients and with the real SDK over a local transport: no
network calls are made."""

import base64
import json
import types
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from app.extraction import extractor
from app.extraction import services as catalog
from app.extraction.adapters import openai_api
from app.extraction.adapters.openai_api import OpenAIAdapter, cost
from app.extraction.base import ExtractionError, Prepared
from app.extraction.extractor import extract_invoice, waiting_for_ai
from app.extraction.prompt import SYSTEM_PROMPT
from app.schemas.extraction import (
    ExtractedLineItem,
    ExtractedParty,
    ExtractedTotals,
    ExtractedValue,
    InvoiceExtraction,
)
from app.services import app_settings

KEY = "sk-proj-test-key-0123456789abcdef"
SERVICE = catalog.BY_ID["openai"]
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

EFFECTIVE = app_settings.Effective(
    tally_url="http://127.0.0.1:9000",
    tally_url_source="env",
    anthropic_api_key=None,
    api_key_source=None,
    claude_model="claude-opus-5-5",
    claude_effort="medium",
    ai_provider="openai",
    other_services={
        "openai": app_settings.ServiceSettings(
            id="openai", api_key=KEY, key_source="settings", model="gpt-5"
        )
    },
)


@pytest.fixture(autouse=True)
def use_settings(monkeypatch: pytest.MonkeyPatch):
    """The office uses OpenAI with KEY and gpt-5; use_settings(model=...) changes them."""

    def apply(**changes) -> None:
        openai_settings = replace(EFFECTIVE.other_services["openai"], **changes)
        effective = replace(EFFECTIVE, other_services={"openai": openai_settings})
        monkeypatch.setattr(app_settings, "current", lambda: effective)

    apply()
    return apply


@pytest.fixture
def pdf_file(tmp_path: Path) -> Path:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)
    return path


def _extract(client, *, kind: str = "pdf", file_path: Path, **kwargs):
    return extract_invoice(
        kind=kind,
        file_path=file_path,
        page_images=kwargs.get("page_images", []),
        text=kwargs.get("text"),
        company_name=COMPANY,
        company_gstin=COMPANY_GSTIN,
        client=client,
    )


# -- answers ------------------------------------------------------------------------------


def _completion_body(
    content: str | None = None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    model: str = "gpt-5-2025-08-07",
    prompt_tokens: int = 5000,
    cached_tokens: int = 2048,
    completion_tokens: int = 1500,
) -> dict:
    """A chat.completion body as the API returns it."""
    if content is None and refusal is None:
        content = SAMPLE.model_dump_json()
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_759_480_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                    "annotations": [],
                },
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens, "audio_tokens": 0},
            "completion_tokens_details": {
                "reasoning_tokens": completion_tokens // 2,
                "audio_tokens": 0,
                "accepted_prediction_tokens": 0,
                "rejected_prediction_tokens": 0,
            },
        },
        "service_tier": "default",
        "system_fingerprint": None,
    }


def _parsed(
    parsed: InvoiceExtraction | None = SAMPLE,
    *,
    refusal: str | None = None,
    model: str = "gpt-5-2025-08-07",
    prompt_tokens: int = 5000,
    cached_tokens: int | None = 2048,
    completion_tokens: int = 1500,
) -> types.SimpleNamespace:
    """What chat.completions.parse() returns, as far as the adapter reads it."""
    details = types.SimpleNamespace(cached_tokens=cached_tokens)
    message = types.SimpleNamespace(parsed=parsed, refusal=refusal, content=None)
    return types.SimpleNamespace(
        model=model,
        choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
        usage=types.SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=details,
        ),
    )


def _completion(**kwargs) -> ChatCompletion:
    return ChatCompletion.model_validate(_completion_body(**kwargs))


class FakeClient:
    """Exposes chat.completions.parse, models.retrieve/list and with_options like the SDK;
    records calls and replays scripted results (an exception is raised)."""

    def __init__(self, *results: object, models: list | None = None) -> None:
        self._results = list(results)
        self._models = models or []
        self.calls: list[dict] = []
        self.options: list[dict] = []
        self.retrieved: list[str] = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(parse=self._parse))
        self.models = types.SimpleNamespace(retrieve=self._retrieve, list=self._list)

    def with_options(self, **options) -> "FakeClient":
        self.options.append(options)
        return self

    def _next(self):
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._next()

    def _retrieve(self, model: str):
        self.retrieved.append(model)
        return self._next()

    def _list(self):
        return iter(self._models)


def _user_content(client: FakeClient, call: int = 0) -> list[dict]:
    messages = client.calls[call]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    return messages[1]["content"]


class Wire:
    """A real openai.OpenAI client whose HTTP calls are answered locally, in order, with
    the given (status, body) replies or exceptions; records every request."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []
        self.transport = httpx2.MockTransport(self._handle)
        self.client = openai.OpenAI(
            api_key=KEY, http_client=httpx2.Client(transport=self.transport), max_retries=0
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx2.Response(status, json=body)

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)


# -- the request on the wire ----------------------------------------------------------------


def test_sdk_request_and_answer_on_the_wire(pdf_file: Path):
    wire = Wire(_completion_body())

    outcome = _extract(wire.client, file_path=pdf_file)

    assert len(wire.requests) == 1
    request = wire.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = wire.body()
    assert body["model"] == "gpt-5"
    assert body["max_completion_tokens"] == 32000
    assert "max_tokens" not in body
    assert body["store"] is False
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "InvoiceExtraction"
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    system, user = body["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["role"] == "user"
    file_part, text_part = user["content"]
    assert file_part == {
        "type": "file",
        "file": {
            "filename": "invoice.pdf",
            "file_data": "data:application/pdf;base64,"
            + base64.standard_b64encode(PDF_BYTES).decode("ascii"),
        },
    }
    assert text_part["type"] == "text"
    assert COMPANY in text_part["text"] and COMPANY_GSTIN in text_part["text"]

    assert outcome.extraction == SAMPLE
    assert outcome.model == "gpt-5-2025-08-07"
    assert outcome.input_tokens == 5000
    assert outcome.cache_read_tokens == 2048
    assert outcome.output_tokens == 1500
    # 2952 x $1.25 + 2048 cached x $0.125 + 1500 x $10, per million tokens.
    assert outcome.cost_usd == pytest.approx(0.018946)


def test_page_images_are_sent_as_data_urls_before_the_text(tmp_path: Path):
    pages = []
    for number, suffix in ((1, ".png"), (2, ".jpg")):
        page = tmp_path / f"page-{number}{suffix}"
        page.write_bytes(f"page {number}".encode())
        pages.append(page)
    client = FakeClient(_parsed())

    _extract(client, kind="image", file_path=pages[0], page_images=pages)

    content = _user_content(client)
    assert [part["type"] for part in content] == ["image_url", "image_url", "text"]
    assert content[0]["image_url"] == {
        "url": "data:image/png;base64," + base64.b64encode(b"page 1").decode(),
        "detail": "high",
    }
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_word_document_is_sent_as_text_only(tmp_path: Path):
    docx = tmp_path / "bill.docx"
    docx.write_bytes(b"PK")
    client = FakeClient(_parsed())

    _extract(client, kind="docx", file_path=docx, text="Invoice No 42 Total 1180")

    content = _user_content(client)
    assert len(content) == 1 and content[0]["type"] == "text"
    assert content[0]["text"].startswith("<document>\nInvoice No 42 Total 1180\n</document>")


def test_call_uses_the_documented_parse_arguments(pdf_file: Path, use_settings):
    use_settings(model="gpt-5-mini")
    client = FakeClient(_parsed())

    _extract(client, file_path=pdf_file)

    kwargs = client.calls[0]
    assert set(kwargs) == {
        "model",
        "messages",
        "response_format",
        "max_completion_tokens",
        "store",
    }
    assert kwargs["model"] == "gpt-5-mini"
    assert kwargs["response_format"] is InvoiceExtraction
    assert kwargs["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_limits_and_budgets():
    adapter = OpenAIAdapter(SERVICE, KEY, "gpt-5")

    limits = adapter.limits()

    assert limits.max_pdf_bytes == 20 * 1024 * 1024
    assert limits.pdf_label == "20 MB"
    assert limits.max_image_payload == 30_000_000
    assert limits.max_images == 20
    assert adapter.budgets == (32000, 64000)


# -- building the client --------------------------------------------------------------------


def test_reading_client_is_built_lazily_from_the_effective_key(
    pdf_file: Path, monkeypatch: pytest.MonkeyPatch
):
    built: list[dict] = []

    def fake_openai(**kwargs):
        built.append(kwargs)
        return FakeClient(_parsed())

    monkeypatch.setattr(openai_api.openai, "OpenAI", fake_openai)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example/v1")

    adapter = extractor.build_adapter(SERVICE, app_settings.current())
    assert isinstance(adapter, OpenAIAdapter)
    assert built == []  # nothing is built until a document is read

    outcome = _extract(None, file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    assert built == [
        {
            "api_key": KEY,
            "base_url": "https://api.openai.com/v1",
            "timeout": 180.0,
            "max_retries": 2,
        }
    ]


def test_real_reading_client_options():
    client = OpenAIAdapter(SERVICE, KEY, "gpt-5")._sdk()

    assert isinstance(client, openai.OpenAI)
    assert client.api_key == KEY
    assert str(client.base_url) == "https://api.openai.com/v1/"
    assert client.timeout == 180.0
    assert client.max_retries == 2
    client.close()


def test_not_configured_without_a_key(pdf_file: Path, use_settings):
    use_settings(api_key=None, key_source=None)

    with pytest.raises(extractor.ExtractionNotConfigured) as exc_info:
        _extract(None, file_path=pdf_file)

    assert "OpenAI API key" in exc_info.value.message
    assert waiting_for_ai(exc_info.value.message)


# -- stop reasons ---------------------------------------------------------------------------


def test_cut_off_answer_keeps_its_usage():
    error = openai.LengthFinishReasonError(
        completion=_completion(
            content='{"is_invoice": true, "document_ty',
            finish_reason="length",
            prompt_tokens=5000,
            cached_tokens=0,
            completion_tokens=32000,
        )
    )
    adapter = OpenAIAdapter(SERVICE, KEY, "gpt-5", FakeClient(error))

    reply = adapter.request(Prepared(lead="text"), "Read it.", budget=32000)

    assert reply.stop == "cut_off"
    assert reply.extraction is None
    assert reply.model == "gpt-5-2025-08-07"
    assert (reply.input_tokens, reply.output_tokens) == (5000, 32000)
    assert reply.cost == Decimal("0.326250")  # 5000 x $1.25 + 32000 x $10, per million


def test_cut_off_is_retried_with_the_larger_budget_and_more_time(pdf_file: Path):
    cut = _completion_body(
        '{"is_invoice": true, "document_ty',
        finish_reason="length",
        cached_tokens=0,
        completion_tokens=32000,
    )
    wire = Wire(cut, _completion_body(cached_tokens=0, completion_tokens=1500))

    outcome = _extract(wire.client, file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    assert [wire.body(i)["max_completion_tokens"] for i in (0, 1)] == [32000, 64000]
    assert wire.requests[1].headers["x-stainless-read-timeout"] == "360.0"
    assert wire.requests[0].headers.get("x-stainless-read-timeout") != "360.0"
    # Both attempts are billed.
    assert outcome.input_tokens == 10_000
    assert outcome.output_tokens == 33_500
    # 10000 x $1.25 + 33500 x $10, per million tokens.
    assert outcome.cost_usd == pytest.approx(0.3475)


def test_cut_off_twice_fails(pdf_file: Path):
    cut = _completion_body('{"is_invoice": tr', finish_reason="length")
    wire = Wire(cut, cut)

    with pytest.raises(ExtractionError) as exc_info:
        _extract(wire.client, file_path=pdf_file)

    assert "cut off" in exc_info.value.message
    assert exc_info.value.retryable is False
    assert len(wire.requests) == 2


def test_refusal_is_not_retried_and_gives_the_reason(pdf_file: Path):
    wire = Wire(_completion_body(refusal="I'm sorry, I can't help with that request."))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(wire.client, file_path=pdf_file)

    assert exc_info.value.message == (
        "OpenAI declined to read this document (reason: I'm sorry, I can't help with that "
        "request). Enter it manually."
    )
    assert exc_info.value.retryable is False
    assert len(wire.requests) == 1


def test_refusal_reply_from_parse():
    adapter = OpenAIAdapter(SERVICE, KEY, "gpt-5", FakeClient(_parsed(None, refusal="No.")))

    reply = adapter.request(Prepared(lead="text"), "Read it.", budget=None)

    assert reply.stop == "refused"
    assert reply.refusal_reason == "No"
    assert reply.input_tokens == 5000


def test_content_filter_counts_as_a_refusal(pdf_file: Path):
    wire = Wire(_completion_body("", finish_reason="content_filter"))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(wire.client, file_path=pdf_file)

    assert exc_info.value.message == (
        "OpenAI declined to read this document (reason: content filter). Enter it manually."
    )
    assert len(wire.requests) == 1


def test_content_filter_without_a_completion():
    error = openai.ContentFilterFinishReasonError()
    adapter = OpenAIAdapter(SERVICE, KEY, "gpt-5", FakeClient(error))

    reply = adapter.request(Prepared(lead="text"), "Read it.", budget=None)

    assert (reply.stop, reply.model, reply.input_tokens) == ("refused", "gpt-5", 0)


def test_answer_without_parsed_output_fails(pdf_file: Path):
    client = FakeClient(_parsed(None))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert "did not return any invoice details" in exc_info.value.message
    assert len(client.calls) == 1


def test_answer_that_does_not_fit_the_schema_is_not_retried(pdf_file: Path):
    wire = Wire(_completion_body('{"is_invoice": true}'))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(wire.client, file_path=pdf_file)

    assert "did not return any invoice details" in exc_info.value.message
    assert exc_info.value.retryable is False
    assert len(wire.requests) == 1


# -- errors ---------------------------------------------------------------------------------

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(
    cls: type, status: int, message: str | None = None, code: str | None = None
) -> Exception:
    """An error as the SDK builds it: body is the response's "error" object."""
    body = {"message": message, "type": code, "param": None, "code": code} if message else None
    return cls(
        f"Error code: {status} - {body}",
        response=httpx2.Response(status, request=_REQUEST),
        body=body,
    )


@pytest.mark.parametrize(
    ("error", "retryable", "expected"),
    [
        (
            _status_error(openai.AuthenticationError, 401, "Incorrect API key provided"),
            False,
            "The OpenAI API key was rejected. An administrator can check it in Settings",
        ),
        (
            _status_error(openai.PermissionDeniedError, 403, "Missing scopes: model.request"),
            False,
            "The OpenAI API key was rejected",
        ),
        (
            _status_error(openai.NotFoundError, 404, "The model `gpt-9` does not exist"),
            False,
            "The OpenAI model gpt-5 is not available to this key",
        ),
        (
            _status_error(openai.RateLimitError, 429, "Rate limit reached", "rate_limit_exceeded"),
            True,
            "usage limit has been reached for now",
        ),
        (
            _status_error(
                openai.RateLimitError, 429, "You exceeded your current quota", "insufficient_quota"
            ),
            False,
            "the account for this key has no credit left",
        ),
        (
            _status_error(openai.BadRequestError, 400, "Invalid 'messages[1]': empty array"),
            False,
            "OpenAI could not process this document: Invalid 'messages[1]': empty array",
        ),
        (
            _status_error(
                openai.BadRequestError,
                400,
                "Invalid content type. File input is not supported for this model.",
            ),
            False,
            "can't read PDF files or scans. An administrator can choose a model that reads "
            "PDFs, such as gpt-5, in Settings.",
        ),
        (
            _status_error(
                openai.BadRequestError,
                400,
                "This model's maximum context length is 400000 tokens.",
                "context_length_exceeded",
            ),
            False,
            "too large to send to OpenAI in one request",
        ),
        (_status_error(openai.APIStatusError, 413), False, "too large to send to OpenAI"),
        (openai.APITimeoutError(request=_REQUEST), True, "took too long"),
        (openai.APIConnectionError(request=_REQUEST), True, "internet connection"),
        (_status_error(openai.InternalServerError, 500), True, "temporarily unavailable"),
        (
            _status_error(openai.InternalServerError, 503, "upstream"),
            True,
            "temporarily unavailable",
        ),
        (_status_error(openai.APIStatusError, 502, "bad gateway"), True, "bad gateway"),
        (
            _status_error(openai.UnprocessableEntityError, 422, "bad field"),
            False,
            "The OpenAI API returned an error: bad field",
        ),
        (_status_error(openai.APIStatusError, 418), False, "error: HTTP 418"),
    ],
    ids=[
        "auth",
        "permission",
        "not-found",
        "rate-limit",
        "insufficient-quota",
        "bad-request",
        "no-pdf-input",
        "context-length",
        "request-too-large",
        "timeout",
        "connection",
        "server-error",
        "unavailable",
        "other-5xx",
        "other-4xx",
        "no-details",
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
    assert len(client.calls) == 1


def test_rejected_key_waits_for_new_settings(pdf_file: Path):
    client = FakeClient(_status_error(openai.AuthenticationError, 401, "Incorrect API key"))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert waiting_for_ai(exc_info.value.message)


def test_insufficient_quota_through_the_sdk(pdf_file: Path):
    error_body = {
        "error": {
            "message": "You exceeded your current quota, please check your plan and billing "
            "details.",
            "type": "insufficient_quota",
            "param": None,
            "code": "insufficient_quota",
        }
    }
    wire = Wire((429, error_body))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(wire.client, file_path=pdf_file)

    assert exc_info.value.retryable is False
    assert "no credit left" in exc_info.value.message
    assert "platform.openai.com" in exc_info.value.message


def test_bad_request_through_the_sdk_names_the_api_message(pdf_file: Path):
    error_body = {
        "error": {
            "message": "Invalid value: 'file'. Supported values are: 'text' and 'image_url'.",
            "type": "invalid_request_error",
            "param": "messages[1].content[0].type",
            "code": "invalid_value",
        }
    }
    wire = Wire((400, error_body))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(wire.client, file_path=pdf_file)

    assert "gpt-5 can't read PDF files" in exc_info.value.message
    assert exc_info.value.retryable is False


def test_key_is_never_echoed_in_a_message(pdf_file: Path):
    client = FakeClient(_status_error(openai.BadRequestError, 400, f"Bad key {KEY} in header"))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert KEY not in exc_info.value.message
    assert "Bad key [key] in header" in exc_info.value.message


# -- checking the key -----------------------------------------------------------------------

_MODEL = {"id": "gpt-5", "object": "model", "created": 1_754_425_777, "owned_by": "system"}


def test_check_looks_the_model_up_with_the_key_and_no_retries():
    wire = Wire(_MODEL)
    wire.client = wire.client.with_options(max_retries=2)  # the reading client retries

    result = OpenAIAdapter(SERVICE, KEY, "gpt-5", wire.client).check()

    assert result.ok is True
    assert result.detail == "Connected. gpt-5 is available."
    request = wire.requests[0]
    assert (request.method, request.url.path) == ("GET", "/v1/models/gpt-5")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-stainless-read-timeout"] == "20.0"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ((401, {"error": {"message": "Incorrect API key"}}), "The key was rejected by OpenAI"),
        ((403, {"error": {"message": "Missing scopes"}}), "If it is a restricted key"),
        ((404, {"error": {"message": "No such model"}}), "gpt-5 is not available to this key"),
        ((500, {"error": {"message": "boom"}}), "returned an error (HTTP 500)"),
        ((429, {"error": {"message": "slow down"}}), "returned an error (HTTP 429)"),
        (httpx2.ConnectError("no route"), "Could not reach the OpenAI API"),
        (httpx2.ReadTimeout("slow"), "Could not reach the OpenAI API"),
    ],
    ids=["auth", "permission", "not-found", "server-error", "rate-limit", "offline", "timeout"],
)
def test_check_failures_are_explained(reply, expected: str):
    wire = Wire(reply)
    wire.client = wire.client.with_options(max_retries=2)

    result = OpenAIAdapter(SERVICE, KEY, "gpt-5", wire.client).check()

    assert result.ok is False
    assert expected in result.detail
    assert KEY not in result.detail
    assert len(wire.requests) == 1  # not retried: someone is waiting in Settings


def test_check_builds_a_short_lived_client_of_its_own(monkeypatch: pytest.MonkeyPatch):
    wire = Wire(_MODEL)
    built: list[dict] = []
    real_openai = openai.OpenAI

    def capture(**kwargs):
        built.append(kwargs)
        client = real_openai(**kwargs, http_client=httpx2.Client(transport=wire.transport))
        built.append({"client": client})
        return client

    monkeypatch.setattr(openai_api.openai, "OpenAI", capture)

    result = OpenAIAdapter(SERVICE, KEY, "gpt-5").check()

    assert result.ok is True
    assert built[0] == {
        "api_key": KEY,
        "base_url": "https://api.openai.com/v1",
        "timeout": 20.0,
        "max_retries": 0,
    }
    assert built[1]["client"].is_closed()


def test_check_with_a_fake_client_uses_short_options():
    client = FakeClient(object())

    result = OpenAIAdapter(SERVICE, KEY, "gpt-5-mini", client).check()

    assert result.ok is True
    assert client.retrieved == ["gpt-5-mini"]
    assert client.options == [{"timeout": 20.0, "max_retries": 0}]


# -- listing models -------------------------------------------------------------------------


def _model(model_id: str, created: int) -> dict:
    return {"id": model_id, "object": "model", "created": created, "owned_by": "system"}


def test_list_models_offers_document_readers_best_first():
    listed = [
        _model("gpt-4o", 1_715_367_049),
        _model("gpt-5-nano", 1_754_426_384),
        _model("gpt-5-mini", 1_754_425_928),
        _model("gpt-5", 1_754_425_777),
        _model("gpt-5-2025-08-07", 1_754_425_777),
        _model("gpt-5.1", 1_762_800_000),
        _model("gpt-4.1", 1_744_316_542),
        _model("gpt-4.1-mini", 1_744_318_173),
        _model("o3", 1_744_225_308),
        _model("o4-mini", 1_744_225_351),
        _model("chatgpt-4o-latest", 1_723_515_131),
        # Not for reading documents:
        _model("gpt-4o-audio-preview", 1_727_460_443),
        _model("gpt-realtime", 1_756_271_701),
        _model("gpt-4o-mini-tts", 1_742_403_959),
        _model("gpt-4o-transcribe", 1_742_068_463),
        _model("gpt-image-1", 1_745_517_030),
        _model("text-embedding-3-large", 1_705_953_180),
        _model("gpt-4o-search-preview", 1_741_388_720),
        _model("omni-moderation-latest", 1_731_689_265),
        _model("gpt-3.5-turbo-instruct", 1_692_901_427),
        _model("gpt-3.5-turbo", 1_677_610_602),
        _model("gpt-4-turbo", 1_712_361_441),
        _model("gpt-4", 1_687_882_411),
        _model("o3-mini", 1_737_146_383),
        _model("o3-deep-research", 1_749_840_121),
        _model("gpt-5-pro", 1_759_469_707),
        _model("gpt-5-codex", 1_757_527_818),
        _model("whisper-1", 1_677_532_384),
        _model("dall-e-3", 1_698_785_189),
        _model("davinci-002", 1_692_634_301),
    ]
    wire = Wire({"object": "list", "data": listed})

    models = OpenAIAdapter(SERVICE, KEY, "gpt-5", wire.client).list_models()

    assert models == [
        "gpt-5.1",
        "gpt-5",
        "gpt-4.1",
        "o3",
        "chatgpt-4o-latest",
        "gpt-4o",
        "gpt-5-mini",
        "gpt-4.1-mini",
        "o4-mini",
        "gpt-5-nano",
        "gpt-5-2025-08-07",
    ]
    request = wire.requests[0]
    assert (request.method, request.url.path) == ("GET", "/v1/models")
    assert request.headers["x-stainless-read-timeout"] == "20.0"


def test_list_models_failure_propagates_for_the_settings_screen():
    wire = Wire((401, {"error": {"message": "Incorrect API key"}}))

    with pytest.raises(openai.AuthenticationError):
        OpenAIAdapter(SERVICE, KEY, "gpt-5", wire.client).list_models()


# -- cost -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5", "11.250000"),
        ("gpt-5-mini", "2.250000"),
        ("gpt-5-nano", "0.450000"),
        ("gpt-5.1", "11.250000"),
        ("gpt-4.1", "10.000000"),
        ("gpt-4.1-mini", "2.000000"),
        ("gpt-4o", "12.500000"),
        ("gpt-4o-mini", "0.750000"),
        ("o3", "10.000000"),
        ("o4-mini", "5.500000"),
    ],
)
def test_cost_per_million_input_and_output_tokens(model: str, expected: str):
    assert cost(model, 1_000_000, 1_000_000) == Decimal(expected)


def test_cached_tokens_are_part_of_the_input_and_priced_lower():
    # 600k uncached x $1.25 + 400k cached x $0.125, per million tokens.
    assert cost("gpt-5", 1_000_000, 0, 400_000) == Decimal("0.800000")
    # gpt-4o's cache discount is half the input price.
    assert cost("gpt-4o", 1_000_000, 0, 1_000_000) == Decimal("1.250000")


def test_dated_snapshots_use_the_longest_matching_price():
    assert cost("gpt-5-mini-2025-08-07", 1_000_000, 0) == Decimal("0.250000")
    assert cost("gpt-5-2025-08-07", 1_000_000, 0) == Decimal("1.250000")
    assert cost("gpt-4o-mini-2024-07-18", 1_000_000, 0) == Decimal("0.150000")
    # Variants with their own prices are not priced as the base model.
    assert cost("gpt-5-pro", 1_000_000, 0) == Decimal("15.000000")
    assert cost("o3-mini-2025-01-31", 1_000_000, 0) == Decimal("1.100000")


def test_unknown_model_costs_nothing():
    assert cost("gpt-99", 1_000_000, 1_000_000) == Decimal("0")
    assert cost("gpt-5x", 1_000_000, 1_000_000) == Decimal("0")
