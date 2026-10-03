"""The Gemini adapter, with fake clients and with the real google-genai SDK wired to a local
httpx transport: no network calls are made."""

import base64
import json
import re
import types as pytypes
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from google.genai import errors
from google.genai import types as gtypes

from app.extraction import services as catalog
from app.extraction.adapters import gemini
from app.extraction.adapters.gemini import RESPONSE_SCHEMA, GeminiAdapter
from app.extraction.base import Prepared
from app.extraction.extractor import (
    ExtractionError,
    ExtractionNotConfigured,
    build_adapter,
    extract_invoice,
    key_rejected_message,
    waiting_for_ai,
)
from app.extraction.prompt import SYSTEM_PROMPT
from app.schemas.extraction import (
    ExtractedLineItem,
    ExtractedParty,
    ExtractedTotals,
    ExtractedValue,
    InvoiceExtraction,
)
from app.services import app_settings

SERVICE = catalog.BY_ID["gemini"]
KEY = "AIzaSyTESTKEY-not-real-0123456789abcdefg"
MODEL = "gemini-2.5-pro"
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
    ai_provider="gemini",
    other_services={
        "gemini": app_settings.ServiceSettings("gemini", KEY, "settings", MODEL),
    },
)


@pytest.fixture(autouse=True)
def use_settings(monkeypatch: pytest.MonkeyPatch):
    """The effective settings the extractor sees: Gemini selected, with a key and a model."""

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


def _extract(client: Any, *, file_path: Path, kind: str = "pdf", **kwargs):
    return extract_invoice(
        kind=kind,
        file_path=file_path,
        page_images=kwargs.get("page_images", []),
        text=kwargs.get("text"),
        company_name=COMPANY,
        company_gstin=COMPANY_GSTIN,
        client=client,
    )


# -- fake SDK client ---------------------------------------------------------------------


def _response(
    text: str | None = None,
    *,
    finish: str | None = "STOP",
    model_version: str | None = MODEL,
    prompt: int = 1200,
    candidates: int = 800,
    thoughts: int | None = 400,
    cached: int | None = 200,
    parts: list[gtypes.Part] | None = None,
    block_reason: str | None = None,
    no_candidates: bool = False,
) -> gtypes.GenerateContentResponse:
    if parts is None:
        parts = [gtypes.Part(text=SAMPLE.model_dump_json() if text is None else text)]
    return gtypes.GenerateContentResponse(
        candidates=None
        if no_candidates
        else [
            gtypes.Candidate(
                content=gtypes.Content(role="model", parts=parts),
                finish_reason=gtypes.FinishReason(finish) if finish else None,
            )
        ],
        prompt_feedback=gtypes.GenerateContentResponsePromptFeedback(
            block_reason=gtypes.BlockedReason(block_reason)
        )
        if block_reason
        else None,
        usage_metadata=gtypes.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            thoughts_token_count=thoughts,
            cached_content_token_count=cached,
        ),
        model_version=model_version,
    )


class FakeClient:
    """Exposes .models.generate_content / get / list like genai.Client; records calls and
    replays scripted results (an exception is raised)."""

    def __init__(self, *results: object, models: list[gtypes.Model] | None = None) -> None:
        self._results = list(results)
        self._models = models or []
        self.calls: list[dict] = []
        self.closed = False
        self.models = pytypes.SimpleNamespace(
            generate_content=self._generate, get=self._get, list=self._list
        )

    def _next(self) -> Any:
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def _generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._next()

    def _get(self, **kwargs):
        self.calls.append(kwargs)
        return self._next()

    def _list(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._models)

    def close(self) -> None:
        self.closed = True


def _adapter(client: Any = None, model: str = MODEL, key: str | None = KEY) -> GeminiAdapter:
    return GeminiAdapter(SERVICE, key, model, client)


def _parts(call: dict) -> list[gtypes.Part]:
    contents = call["contents"]
    assert len(contents) == 1 and contents[0].role == "user"
    return contents[0].parts


def _api_error(code: int, message: str, status: str, reason: str | None = None):
    body: dict = {"error": {"code": code, "message": message, "status": status}}
    if reason:
        body["error"]["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": reason,
                "domain": "googleapis.com",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            }
        ]
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, body)


INVALID_KEY = _api_error(
    400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", "API_KEY_INVALID"
)


# -- request shape -------------------------------------------------------------------------


def test_pdf_request_sends_the_pdf_then_the_instructions_with_the_schema(pdf_file: Path):
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert set(call) == {"model", "contents", "config"}
    assert call["model"] == MODEL
    pdf, text = _parts(call)
    assert pdf.inline_data.mime_type == "application/pdf"
    assert pdf.inline_data.data == PDF_BYTES
    assert COMPANY in text.text and COMPANY_GSTIN in text.text
    config: gtypes.GenerateContentConfig = call["config"]
    assert config.system_instruction == SYSTEM_PROMPT
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema is RESPONSE_SCHEMA
    assert config.response_schema is None
    assert config.max_output_tokens == 32768
    assert config.temperature is None  # left to the model; see the module docstring
    assert config.thinking_config is None
    assert config.automatic_function_calling.disable is True
    assert config.http_options.timeout == 180_000
    assert config.http_options.headers == {"X-Server-Timeout": "180"}


def test_large_pdf_goes_as_page_images_including_the_last(
    pdf_file: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(gemini, "MAX_PDF_BYTES", 10)
    pages = []
    for number in range(1, 26):
        page = tmp_path / f"page-{number}.png"
        page.write_bytes(f"page {number}".encode())
        pages.append(page)
    client = FakeClient(_response())

    _extract(client, file_path=pdf_file, page_images=pages, text="Invoice text layer")

    *images, text = _parts(client.calls[0])
    assert len(images) == 20
    assert all(p.inline_data.mime_type == "image/png" for p in images)
    assert images[0].inline_data.data == b"page 1"
    assert images[-1].inline_data.data == b"page 25"
    assert "Pages 20 to 24 are left out" in text.text
    assert "Invoice text layer" not in text.text  # Gemini reads the PDF itself


def test_image_is_sent_with_its_media_type(tmp_path: Path):
    photo = tmp_path / "bill.jpg"
    photo.write_bytes(b"\xff\xd8\xff jpeg bytes")
    client = FakeClient(_response())

    _extract(client, kind="image", file_path=photo)

    image, _ = _parts(client.calls[0])
    assert image.inline_data.mime_type == "image/jpeg"
    assert image.inline_data.data == b"\xff\xd8\xff jpeg bytes"


def test_word_document_is_sent_as_text_only(tmp_path: Path):
    client = FakeClient(_response())

    _extract(client, kind="docx", file_path=tmp_path / "bill.docx", text="Tax Invoice 42")

    (text,) = _parts(client.calls[0])
    assert text.text.startswith("<document>\nTax Invoice 42\n</document>")


def test_limits_fit_gemini_inline_request_size():
    limits = _adapter().limits()

    assert limits.max_pdf_bytes == 14 * 1024 * 1024
    assert limits.pdf_label == "14 MB"
    # base64 of the largest PDF, plus the prompt and schema, stays under 20 MB.
    assert 4 * -(-limits.max_pdf_bytes // 3) + 100_000 < 20 * 1024 * 1024
    assert limits.max_image_payload + 100_000 < 20 * 1024 * 1024
    assert limits.max_images == 20


# -- the schema ----------------------------------------------------------------------------


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _schema_keys(schema: dict) -> set[str]:
    """Keywords used anywhere in the schema (not property names)."""
    keys = set(schema) - {"properties"}
    for sub in schema.get("properties", {}).values():
        keys |= _schema_keys(sub)
    for sub in schema.get("anyOf", []):
        keys |= _schema_keys(sub)
    if isinstance(schema.get("items"), dict):
        keys |= _schema_keys(schema["items"])
    return keys


def _valid(instance: Any, schema: dict) -> bool:
    """Just enough JSON Schema for the keywords this schema uses."""
    if "anyOf" in schema:
        return any(_valid(instance, sub) for sub in schema["anyOf"])
    if "enum" in schema and instance not in schema["enum"]:
        return False
    kind = schema.get("type")
    checks = {
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "null": lambda v: v is None,
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    if kind and not checks[kind](instance):
        return False
    if kind == "array":
        return all(_valid(item, schema["items"]) for item in instance)
    if kind == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(instance) - set(props):
            return False
        if set(schema.get("required", [])) - set(instance):
            return False
        return all(_valid(instance[name], props[name]) for name in instance)
    return True


def test_schema_is_self_contained_with_supported_keywords_only():
    supported = {"type", "description", "enum", "items", "anyOf", "required"}
    supported |= {"additionalProperties"}

    assert _schema_keys(RESPONSE_SCHEMA) <= supported
    assert "$ref" not in json.dumps(RESPONSE_SCHEMA)
    # A field's own description survives the inlining, next to the shared definition's
    # properties.
    sgst = RESPONSE_SCHEMA["properties"]["totals"]["properties"]["sgst"]
    assert sgst["description"] == "SGST or UTGST amount."
    assert set(sgst["properties"]) == {"value", "confidence", "source_text", "page"}
    assert sgst["required"] == ["value", "confidence"]
    assert all(
        node.get("additionalProperties") is False
        for node in _walk(RESPONSE_SCHEMA)
        if node.get("type") == "object"
    )


def test_schema_accepts_an_extraction_and_rejects_an_unknown_field():
    answer = json.loads(SAMPLE.model_dump_json())

    assert _valid(answer, RESPONSE_SCHEMA)
    assert not _valid({**answer, "extra": 1}, RESPONSE_SCHEMA)
    assert not _valid({**answer, "document_type": "invoice"}, RESPONSE_SCHEMA)
    del answer["totals"]
    assert not _valid(answer, RESPONSE_SCHEMA)


# -- replies -------------------------------------------------------------------------------


def test_answer_is_parsed_with_usage_and_cost():
    reply = _adapter(FakeClient(_response())).request(Prepared(lead="x"), "Read", budget=32768)

    assert reply.extraction == SAMPLE
    assert reply.stop == "done"
    assert reply.model == MODEL
    assert reply.input_tokens == 1200  # includes the 200 cached
    assert reply.output_tokens == 1200  # 800 answer + 400 thinking
    assert reply.cache_read_tokens == 200
    # (1000 x 1.25 + 200 x 0.125 + 1200 x 10) / 1M
    assert reply.cost == Decimal("0.013275")
    assert reply.refusal_reason is None


def test_model_falls_back_to_the_configured_one_and_strips_the_prefix():
    adapter = _adapter(
        FakeClient(
            _response(model_version=None), _response(model_version="models/gemini-2.5-flash")
        )
    )

    assert adapter.request(Prepared(lead="x"), "Read", budget=1).model == MODEL
    assert adapter.request(Prepared(lead="x"), "Read", budget=1).model == "gemini-2.5-flash"


def test_missing_usage_counts_as_zero():
    response = _response()
    response.usage_metadata = None

    reply = _adapter(FakeClient(response)).request(Prepared(lead="x"), "Read", budget=1)

    assert (reply.input_tokens, reply.output_tokens, reply.cache_read_tokens) == (0, 0, 0)
    assert reply.cost == Decimal("0")


def test_extraction_outcome_reports_gemini_usage(pdf_file: Path):
    outcome = _extract(FakeClient(_response()), file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    assert outcome.model == MODEL
    assert (outcome.input_tokens, outcome.output_tokens, outcome.cache_read_tokens) == (
        1200,
        1200,
        200,
    )
    assert outcome.cost_usd == pytest.approx(0.013275)


def test_code_fence_around_the_answer_is_stripped():
    fenced = f"```json\n{SAMPLE.model_dump_json()}\n```"

    reply = _adapter(FakeClient(_response(fenced))).request(Prepared(lead="x"), "Read", budget=1)

    assert reply.extraction == SAMPLE


def test_thought_parts_are_not_part_of_the_answer():
    parts = [
        gtypes.Part(text="Let me read the totals first.", thought=True),
        gtypes.Part(text=SAMPLE.model_dump_json()),
    ]

    reply = _adapter(FakeClient(_response(parts=parts))).request(
        Prepared(lead="x"), "Read", budget=1
    )

    assert reply.extraction == SAMPLE


@pytest.mark.parametrize("text", ["not json at all", '{"is_invoice": true}', "", "```\n```"])
def test_answer_that_is_not_an_extraction_is_done_without_one(text: str):
    reply = _adapter(FakeClient(_response(text))).request(Prepared(lead="x"), "Read", budget=1)

    assert reply.extraction is None
    assert reply.stop == "done"


def test_invalid_answer_fails_without_a_retry(pdf_file: Path):
    client = FakeClient(_response('{"is_invoice": true, "document_type": "bill"}'))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.message == (
        "Gemini did not return any invoice details for this document. Enter it manually."
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "CONTINUATION"])
def test_unfinished_answer_is_cut_off(finish: str):
    cut = '{"is_invoice": true, "document_ty'

    reply = _adapter(FakeClient(_response(cut, finish=finish))).request(
        Prepared(lead="x"), "Read", budget=1
    )

    assert reply.stop == "cut_off"
    assert reply.extraction is None
    assert reply.output_tokens == 1200  # still billed


def test_cut_off_answer_is_asked_again_with_the_larger_budget(pdf_file: Path):
    client = FakeClient(_response("{", finish="MAX_TOKENS"), _response())

    outcome = _extract(client, file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    budgets = [c["config"].max_output_tokens for c in client.calls]
    assert budgets == [32768, 65536]
    timeouts = [c["config"].http_options.timeout for c in client.calls]
    assert timeouts == [180_000, 360_000]
    assert client.calls[1]["config"].http_options.headers == {"X-Server-Timeout": "360"}
    assert outcome.input_tokens == 2400  # both attempts are billed
    assert outcome.cost_usd == pytest.approx(2 * 0.013275)


def test_cut_off_twice_fails(pdf_file: Path):
    client = FakeClient(_response("{", finish="MAX_TOKENS"), _response("{", finish="MAX_TOKENS"))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.message.startswith(
        "Gemini's answer for this document was cut off before it finished."
    )
    assert exc_info.value.retryable is False
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("finish", "reason"),
    [
        ("SAFETY", "safety filter"),
        ("PROHIBITED_CONTENT", "prohibited content"),
        ("BLOCKLIST", "blocked terms"),
        ("SPII", "personal information"),
        ("RECITATION", "resembles published material"),
        ("IMAGE_SAFETY", "image safety filter"),
        ("IMAGE_PROHIBITED_CONTENT", "prohibited image content"),
    ],
)
def test_blocked_answer_is_refused_and_not_retried(pdf_file: Path, finish: str, reason: str):
    client = FakeClient(_response("", finish=finish))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.message == (
        f"Gemini declined to read this document (reason: {reason}). Enter it manually."
    )
    assert exc_info.value.retryable is False
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        ("PROHIBITED_CONTENT", "prohibited content"),
        ("SAFETY", "safety filter"),
        ("OTHER", "blocked by Google"),
        ("JAILBREAK", "instructions hidden in the document"),
    ],
)
def test_blocked_prompt_is_refused(block: str, reason: str):
    response = _response(no_candidates=True, block_reason=block, candidates=0, thoughts=0)

    reply = _adapter(FakeClient(response)).request(Prepared(lead="x"), "Read", budget=1)

    assert reply.stop == "refused"
    assert reply.refusal_reason == reason
    assert reply.input_tokens == 1200


def test_no_answer_and_no_block_reason_returns_nothing(pdf_file: Path):
    client = FakeClient(_response(no_candidates=True))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert "did not return any invoice details" in exc_info.value.message


# -- cost ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # 1M prompt tokens of which 0 cached, plus 1M output tokens.
        ("gemini-2.5-pro", Decimal("11.25")),
        ("gemini-2.5-flash", Decimal("2.80")),
        ("gemini-2.5-flash-lite", Decimal("0.50")),
        ("gemini-3-pro-preview", Decimal("14")),
        ("gemini-3.1-pro-preview", Decimal("14")),
        ("gemini-3-flash-preview", Decimal("3.50")),
        # Suffixed ids take the longest matching price.
        ("gemini-2.5-flash-preview-09-2025", Decimal("2.80")),
        ("gemini-2.5-flash-lite-preview-09-2025", Decimal("0.50")),
        ("gemini-9-ultra", Decimal("0")),
        ("gemini-flash-latest", Decimal("0")),
    ],
)
def test_cost_per_million_tokens(model: str, expected: Decimal):
    # Below the long-context threshold: 100k prompt, 100k output, scaled by 10.
    assert gemini._cost(model, 100_000, 100_000, 0) * 10 == expected


def test_cached_tokens_are_billed_at_the_cache_rate():
    # 100k prompt of which 60k cached: 40k x 1.25 + 60k x 0.125, no output.
    assert gemini._cost("gemini-2.5-pro", 100_000, 0, 60_000) == Decimal("0.0575")


def test_long_prompts_use_the_long_context_price():
    assert gemini._cost("gemini-2.5-pro", 300_000, 10_000, 0) == Decimal("0.9")
    assert gemini._cost("gemini-3-pro-preview", 300_000, 10_000, 0) == Decimal("1.38")
    # Flash has one price at any length.
    assert gemini._cost("gemini-2.5-flash", 300_000, 0, 0) == Decimal("0.09")


# -- errors --------------------------------------------------------------------------------

_REJECTED = key_rejected_message(SERVICE)


@pytest.mark.parametrize(
    ("error", "retryable", "expected"),
    [
        (INVALID_KEY, False, _REJECTED),
        (
            _api_error(400, "API key expired. Please renew the API key.", "INVALID_ARGUMENT"),
            False,
            _REJECTED,
        ),
        (
            _api_error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED"),
            False,
            _REJECTED,
        ),
        (
            _api_error(403, "Method doesn't allow unregistered callers.", "PERMISSION_DENIED"),
            False,
            _REJECTED,
        ),
        (
            _api_error(
                404, "models/gemini-2.5-pro is not found for API version v1beta.", "NOT_FOUND"
            ),
            False,
            "Gemini could not read this document because the model gemini-2.5-pro is not "
            "available to this API key. An administrator can choose another model in Settings.",
        ),
        (
            _api_error(
                429, "Resource has been exhausted (e.g. check quota).", "RESOURCE_EXHAUSTED"
            ),
            True,
            "Gemini's usage limit for this key has been reached for now. Try again in a minute.",
        ),
        (
            _api_error(400, "The document has no pages.", "INVALID_ARGUMENT"),
            False,
            "Gemini could not process this document: The document has no pages.",
        ),
        (
            _api_error(
                400,
                "Request payload size exceeds the limit: 20971520 bytes.",
                "INVALID_ARGUMENT",
            ),
            False,
            "This document is too large to send to Gemini in one request. Split it into "
            "smaller files and upload them again, or enter it manually.",
        ),
        (
            _api_error(500, "An internal error has occurred.", "INTERNAL"),
            True,
            "Gemini is temporarily unavailable. Try again in a few minutes.",
        ),
        (
            _api_error(503, "The model is overloaded. Please try again later.", "UNAVAILABLE"),
            True,
            "Gemini is temporarily unavailable. Try again in a few minutes.",
        ),
        (
            _api_error(504, "Deadline exceeded.", "DEADLINE_EXCEEDED"),
            True,
            "Gemini took too long to answer. Try again.",
        ),
        (
            _api_error(408, "Request timed out.", "DEADLINE_EXCEEDED"),
            True,
            "The Gemini API returned an error: Request timed out.",
        ),
        (
            httpx.ReadTimeout("timed out"),
            True,
            "Gemini took too long to answer. Try again.",
        ),
        (
            httpx2.ConnectTimeout("timed out"),
            True,
            "Gemini took too long to answer. Try again.",
        ),
        (
            httpx.ConnectError("getaddrinfo failed"),
            True,
            "Could not reach the Gemini API. Check the internet connection and try again.",
        ),
        (
            httpx2.RemoteProtocolError("Server disconnected"),
            True,
            "Could not reach the Gemini API. Check the internet connection and try again.",
        ),
        (
            errors.UnknownApiResponseError("Failed to parse response as JSON."),
            True,
            "The Gemini API sent an answer that could not be read. Try again in a few minutes.",
        ),
    ],
)
def test_sdk_errors_are_mapped(pdf_file: Path, error: Exception, retryable: bool, expected: str):
    client = FakeClient(error)

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, file_path=pdf_file)

    assert exc_info.value.message == expected
    assert exc_info.value.retryable is retryable
    assert "Gemini" in exc_info.value.message
    assert len(client.calls) == 1


def test_rejected_key_leaves_the_document_waiting_for_a_new_key(pdf_file: Path):
    with pytest.raises(ExtractionError) as exc_info:
        _extract(FakeClient(INVALID_KEY), file_path=pdf_file)

    assert waiting_for_ai(exc_info.value.message)


def test_api_key_never_appears_in_an_error_message(pdf_file: Path):
    echo = _api_error(400, f"Bad request for key {KEY}.", "INVALID_ARGUMENT")
    other = _api_error(418, f"Odd reply for {KEY}", "UNKNOWN")

    for error in (echo, other):
        with pytest.raises(ExtractionError) as exc_info:
            _extract(FakeClient(error), file_path=pdf_file)
        assert KEY not in exc_info.value.message
        assert "[API key]" in exc_info.value.message


# -- settings ------------------------------------------------------------------------------


def test_build_adapter_uses_the_gemini_key_and_model():
    adapter = build_adapter(SERVICE, app_settings.current())

    assert isinstance(adapter, GeminiAdapter)
    assert adapter.model == MODEL
    assert adapter.budgets == (32768, 65536)


def test_not_configured_without_a_gemini_key(use_settings, pdf_file: Path):
    use_settings(other_services={})

    with pytest.raises(ExtractionNotConfigured) as exc_info:
        _extract(None, file_path=pdf_file)

    assert "no Gemini API key was found" in exc_info.value.message
    assert waiting_for_ai(exc_info.value.message)


def test_client_is_built_from_the_key_with_timeouts_and_no_sdk_retries(pdf_file: Path, monkeypatch):
    built: list[dict] = []
    client = FakeClient(_response())

    def fake_client(**kwargs):
        built.append(kwargs)
        return client

    monkeypatch.setattr(gemini.genai, "Client", fake_client)

    _extract(None, file_path=pdf_file)

    assert len(built) == 1
    kwargs = built[0]
    assert kwargs["api_key"] == KEY
    assert kwargs["vertexai"] is False
    assert kwargs["http_options"].timeout == 180_000  # milliseconds
    assert kwargs["http_options"].retry_options.attempts == 1
    assert len(client.calls) == 1


# -- the real SDK on a local transport ------------------------------------------------------

_REAL_CLIENT = gemini.genai.Client


def _gemini_body(text: str, *, finish: str = "STOP") -> dict:
    """A generateContent response as the Gemini API sends it."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text, "thoughtSignature": "c2lnbmF0dXJlLWJ5dGVz"}],
                    "role": "model",
                },
                "finishReason": finish,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 3104,
            "candidatesTokenCount": 912,
            "totalTokenCount": 5210,
            "cachedContentTokenCount": 2048,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 2846},
                {"modality": "DOCUMENT", "tokenCount": 258},
            ],
            "cacheTokensDetails": [{"modality": "TEXT", "tokenCount": 2048}],
            "thoughtsTokenCount": 1194,
        },
        "modelVersion": "gemini-2.5-pro",
        "responseId": "kq7aaPfXBdOzz7IPhLDV8Qs",
    }


def _wire(monkeypatch, *replies: Any) -> list[httpx.Request]:
    """Makes the adapter's own genai.Client send over a local transport that answers with
    the given replies (a dict is a 200 JSON body, an (int, dict) an error, an exception is
    raised). Returns the requests sent."""
    sent: list[httpx.Request] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, tuple):
            return httpx.Response(reply[0], json=reply[1])
        return httpx.Response(200, json=reply)

    def client_on_transport(**kwargs):
        kwargs["http_options"].httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
        return _REAL_CLIENT(**kwargs)

    monkeypatch.setattr(gemini.genai, "Client", client_on_transport)
    return sent


def test_wire_request_and_answer(pdf_file: Path, monkeypatch):
    sent = _wire(monkeypatch, _gemini_body(SAMPLE.model_dump_json()))

    outcome = _extract(None, file_path=pdf_file)

    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path == "/v1beta/models/gemini-2.5-pro:generateContent"
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    assert request.headers["x-server-timeout"] == "180"
    assert request.extensions["timeout"]["read"] == 180.0

    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"] == [{"text": SYSTEM_PROMPT}]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == RESPONSE_SCHEMA
    assert "responseSchema" not in config
    assert config["maxOutputTokens"] == 32768
    assert "temperature" not in config
    assert "tools" not in body

    (content,) = body["contents"]
    assert content["role"] == "user"
    pdf, text = content["parts"]
    inline = pdf["inlineData"]
    assert (inline.get("mimeType") or inline.get("mime_type")) == "application/pdf"
    # The SDK writes bytes as URL-safe base64, which the API's JSON mapping accepts.
    assert re.fullmatch(r"[A-Za-z0-9_-]+=*", inline["data"])
    assert base64.urlsafe_b64decode(inline["data"]) == PDF_BYTES
    assert COMPANY in text["text"]

    assert outcome.extraction == SAMPLE
    assert outcome.model == "gemini-2.5-pro"
    assert outcome.input_tokens == 3104
    assert outcome.output_tokens == 912 + 1194
    assert outcome.cache_read_tokens == 2048
    # (1056 x 1.25 + 2048 x 0.125 + 2106 x 10) / 1M
    assert outcome.cost_usd == pytest.approx(0.022636)


def test_wire_cut_off_retry_gets_the_longer_deadline(pdf_file: Path, monkeypatch):
    sent = _wire(
        monkeypatch,
        _gemini_body('{"is_invoice": tr', finish="MAX_TOKENS"),
        _gemini_body(SAMPLE.model_dump_json()),
    )

    outcome = _extract(None, file_path=pdf_file)

    assert outcome.extraction == SAMPLE
    budgets = [json.loads(r.content)["generationConfig"]["maxOutputTokens"] for r in sent]
    assert budgets == [32768, 65536]
    assert [r.headers["x-server-timeout"] for r in sent] == ["180", "360"]
    assert [r.extensions["timeout"]["read"] for r in sent] == [180.0, 360.0]


def test_wire_safety_block_is_refused(pdf_file: Path, monkeypatch):
    body = _gemini_body("", finish="SAFETY")
    body["candidates"][0]["safetyRatings"] = [
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH", "blocked": True}
    ]
    _wire(monkeypatch, body)

    with pytest.raises(ExtractionError) as exc_info:
        _extract(None, file_path=pdf_file)

    assert exc_info.value.message == (
        "Gemini declined to read this document (reason: safety filter). Enter it manually."
    )


@pytest.mark.parametrize(
    ("status", "body", "retryable", "expected"),
    [
        (
            400,
            {
                "error": {
                    "code": 400,
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "API_KEY_INVALID",
                            "domain": "googleapis.com",
                            "metadata": {"service": "generativelanguage.googleapis.com"},
                        }
                    ],
                }
            },
            False,
            _REJECTED,
        ),
        (
            429,
            {
                "error": {
                    "code": 429,
                    "message": "You exceeded your current quota.",
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
            True,
            "Gemini's usage limit for this key has been reached for now. Try again in a minute.",
        ),
        (
            503,
            {
                "error": {
                    "code": 503,
                    "message": "The model is overloaded. Please try again later.",
                    "status": "UNAVAILABLE",
                }
            },
            True,
            "Gemini is temporarily unavailable. Try again in a few minutes.",
        ),
    ],
)
def test_wire_errors_are_mapped_without_sdk_retries(
    pdf_file: Path, monkeypatch, status: int, body: dict, retryable: bool, expected: str
):
    sent = _wire(monkeypatch, (status, body), (status, body), (status, body))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(None, file_path=pdf_file)

    assert exc_info.value.message == expected
    assert exc_info.value.retryable is retryable
    assert len(sent) == 1  # the worker retries later; the SDK does not


def test_wire_timeout_is_retryable_and_not_retried_by_the_sdk(pdf_file: Path, monkeypatch):
    sent = _wire(monkeypatch, httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out"))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(None, file_path=pdf_file)

    assert exc_info.value.message == "Gemini took too long to answer. Try again."
    assert exc_info.value.retryable is True
    assert len(sent) == 1


def test_wire_check_looks_the_model_up(monkeypatch):
    sent = _wire(
        monkeypatch,
        {
            "name": "models/gemini-2.5-pro",
            "displayName": "Gemini 2.5 Pro",
            "inputTokenLimit": 1048576,
            "outputTokenLimit": 65536,
            "supportedGenerationMethods": ["generateContent", "countTokens", "createCachedContent"],
        },
    )

    result = _adapter().check()

    assert result.ok is True
    assert result.detail == "Connected. gemini-2.5-pro is available."
    (request,) = sent
    assert request.method == "GET"
    assert request.url.path == "/v1beta/models/gemini-2.5-pro"
    assert request.headers["x-goog-api-key"] == KEY
    assert request.extensions["timeout"]["read"] == 20.0


def test_wire_list_models_reads_every_page(monkeypatch):
    sent = _wire(
        monkeypatch,
        {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ],
            "nextPageToken": "page-2",
        },
        {
            "models": [
                {
                    "name": "models/gemini-2.5-pro",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        },
    )

    assert _adapter().list_models() == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert [r.url.path for r in sent] == ["/v1beta/models", "/v1beta/models"]
    assert sent[1].url.params["pageToken"] == "page-2"
    assert all(r.headers["x-goog-api-key"] == KEY for r in sent)


# -- check() and list_models() --------------------------------------------------------------


def _model(name: str, actions: list[str] | None = None) -> gtypes.Model:
    return gtypes.Model(name=f"models/{name}", supported_actions=actions or ["generateContent"])


def test_check_succeeds_when_the_model_is_found():
    client = FakeClient(_model(MODEL))

    result = _adapter(client).check()

    assert result.ok is True
    assert result.detail == "Connected. gemini-2.5-pro is available."
    assert client.calls == [{"model": MODEL}]


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            INVALID_KEY,
            "The key was rejected by Google. Check that it was copied in full and has not "
            "been revoked, then save it again.",
        ),
        (
            _api_error(403, "Your API key was reported as leaked.", "PERMISSION_DENIED"),
            "The key was rejected by Google. Check that it was copied in full and has not "
            "been revoked, then save it again.",
        ),
        (
            _api_error(404, "models/gemini-2.5-pro is not found.", "NOT_FOUND"),
            "gemini-2.5-pro is not available to this key. Choose another model, or check the "
            "model name.",
        ),
        (
            _api_error(429, "Quota exceeded.", "RESOURCE_EXHAUSTED"),
            "This key's Gemini usage limit has been reached for now. Try again later.",
        ),
        (
            _api_error(
                400, "User location is not supported for the API use.", "FAILED_PRECONDITION"
            ),
            "The Gemini API returned an error (HTTP 400): User location is not supported for "
            "the API use.",
        ),
        (
            _api_error(500, "Internal error.", "INTERNAL"),
            "The Gemini API returned an error (HTTP 500). Try again in a few minutes.",
        ),
        (
            httpx.ConnectError("getaddrinfo failed"),
            "Could not reach the Gemini API. Check this computer's internet connection and "
            "try again.",
        ),
        (
            httpx.ConnectTimeout("timed out"),
            "Could not reach the Gemini API. Check this computer's internet connection and "
            "try again.",
        ),
    ],
)
def test_check_failures_are_explained(error: Exception, detail: str):
    result = _adapter(FakeClient(error)).check()

    assert result.ok is False
    assert result.detail == detail


def test_check_rejects_a_model_that_cannot_read_documents():
    result = _adapter(FakeClient(_model(MODEL, ["embedContent"]))).check()

    assert result.ok is False
    assert result.detail == "gemini-2.5-pro can't read documents. Choose another model."


def test_check_uses_a_short_lived_client_with_a_short_timeout(monkeypatch):
    built: list[dict] = []
    client = FakeClient(_model(MODEL))

    def fake_client(**kwargs):
        built.append(kwargs)
        return client

    monkeypatch.setattr(gemini.genai, "Client", fake_client)

    assert _adapter().check().ok is True
    assert built[0]["http_options"].timeout == 20_000
    assert built[0]["http_options"].retry_options.attempts == 1
    assert client.closed is True


def test_list_models_offers_gemini_reading_models_best_first():
    names = [
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-flash-image",
        "gemini-2.5-pro",
        "gemini-2.5-pro-preview-06-05",
        "gemini-2.5-computer-use-preview-10-2025",
        "gemini-3-flash-preview",
        "gemini-3-pro-preview",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-pro-latest",
        "gemma-3-27b-it",
    ]
    models = [_model(n) for n in names]
    models.append(_model("gemini-embedding-001", ["embedContent"]))
    models.append(_model("gemini-2.5-pro"))  # listed twice: offered once
    client = FakeClient(models=models)

    assert _adapter(client).list_models() == [
        "gemini-3-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-pro-preview-06-05",
        "gemini-pro-latest",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
        "gemini-flash-lite-latest",
    ]


def test_list_models_closes_its_own_client(monkeypatch):
    client = FakeClient(models=[_model(MODEL)])
    monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: client)

    assert _adapter().list_models() == [MODEL]
    assert client.closed is True
