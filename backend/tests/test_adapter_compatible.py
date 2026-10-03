"""The adapter for OpenAI-compatible services (OpenRouter, Groq, xAI, DeepSeek, Mistral and
"Other"), with fake clients and with the real openai SDK answered by a local transport: no
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
from openai.lib._pydantic import to_strict_json_schema

from app.extraction import extractor
from app.extraction import services as catalog
from app.extraction.adapters import compatible
from app.extraction.adapters.compatible import SCHEMA_TEXT, TEXT_ONLY_NOTE, CompatibleAdapter
from app.extraction.base import ExtractionError, Limits, Prepared, prepare
from app.extraction.extractor import extract_invoice
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
INSTRUCTIONS = build_instructions(COMPANY, COMPANY_GSTIN)
OPENROUTER_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
LOCAL_URL = "http://127.0.0.1:11434/v1"
PDF_BYTES = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n" + bytes(range(256)) * 4
PDF_TEXT = "TAX INVOICE\nInvoice No: INV/2026-27/0042\nGrand Total: 1,180.00"


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
ANSWER = SAMPLE.model_dump_json()
INCOMPLETE = '{"is_invoice": true}'
JSON_SCHEMA_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "invoice_extraction",
        "strict": True,
        "schema": to_strict_json_schema(InvoiceExtraction),
    },
}


# -- fakes ------------------------------------------------------------------------------------


def _completion(
    text: str | None = ANSWER,
    *,
    finish: str = "stop",
    model: str | None = "google/gemini-2.5-pro",
    refusal: str | None = None,
    prompt_tokens: int = 1200,
    completion_tokens: int = 800,
    cached: int | None = None,
    cost: float | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        model=model,
        choices=[
            types.SimpleNamespace(
                finish_reason=finish,
                message=types.SimpleNamespace(content=text, refusal=refusal),
            )
        ],
        usage=types.SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=types.SimpleNamespace(cached_tokens=cached),
            cost=cost,
        ),
    )


class FakeClient:
    """Exposes chat.completions.create, models.list and get like the SDK; records calls and
    replays scripted results (an exception is raised)."""

    def __init__(self, *results: object, models: object = (), key: object = None) -> None:
        self._results = list(results)
        self._models = models
        self._key = key
        self.calls: list[dict] = []
        self.gets: list[str] = []
        self.lists = 0
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        self.models = types.SimpleNamespace(list=self._list)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def _list(self):
        self.lists += 1
        if isinstance(self._models, BaseException):
            raise self._models
        data = [
            model if isinstance(model, dict) else types.SimpleNamespace(id=model)
            for model in self._models
        ]
        return types.SimpleNamespace(data=data)

    def get(self, path: str, *, cast_to: type):
        self.gets.append(path)
        if isinstance(self._key, BaseException):
            raise self._key
        return {"data": {"label": "test"}}


_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def _status_error(cls: type, status: int, message: str | None = None) -> Exception:
    # The SDK keeps the body's "error" object and puts the whole body in the message.
    body = {"message": message, "type": "invalid_request_error"} if message else None
    return cls(
        f"Error code: {status} - {{'error': {body}}}",
        response=httpx2.Response(status, request=_REQUEST),
        body=body,
    )


def _adapter(
    service_id: str = "openrouter",
    client: object = None,
    *,
    key: str | None = OPENROUTER_KEY,
    model: str | None = None,
    base_url: str | None = None,
) -> CompatibleAdapter:
    service = catalog.get(service_id)
    return CompatibleAdapter(
        service,
        key,
        model or service.default_model or "llava",
        base_url or service.base_url or LOCAL_URL,
        client,
    )


def _pages(tmp_path: Path, count: int) -> list[Path]:
    pages = []
    for number in range(1, count + 1):
        page = tmp_path / f"page-{number}.png"
        page.write_bytes(f"page {number} image bytes".encode() + bytes(range(64)))
        pages.append(page)
    return pages


@pytest.fixture
def pdf_file(tmp_path: Path) -> Path:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)
    return path


@pytest.fixture
def docx_file(tmp_path: Path) -> Path:
    path = tmp_path / "invoice.docx"
    path.write_bytes(b"PK zip")
    return path


def _pdf_doc(adapter: CompatibleAdapter, pdf_file: Path, pages: list[Path], text: str | None):
    """The document as the extractor prepares it for this adapter's service."""
    doc = prepare("pdf", pdf_file, pages, text, service=adapter.service, limits=adapter.limits())
    return doc, doc.instructions(INSTRUCTIONS)


def _user(call: dict) -> object:
    messages = call["messages"]
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    return messages[1]["content"]


def _image_bytes(part: dict) -> bytes:
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    return base64.b64decode(url.removeprefix("data:image/png;base64,"), validate=True)


# -- settings and extract_invoice -------------------------------------------------------------

EFFECTIVE = app_settings.Effective(
    tally_url="http://127.0.0.1:9000",
    tally_url_source="env",
    anthropic_api_key=None,
    api_key_source=None,
    claude_model="claude-opus-5-5",
    claude_effort="medium",
)


def _use_service(
    monkeypatch: pytest.MonkeyPatch,
    service_id: str,
    *,
    key: str | None = OPENROUTER_KEY,
    model: str | None = None,
    base_url: str | None = None,
) -> None:
    service = catalog.get(service_id)
    state = app_settings.ServiceSettings(
        service_id,
        key,
        "settings" if key else None,
        model or service.default_model,
        base_url or service.base_url,
    )
    effective = replace(EFFECTIVE, ai_provider=service_id, other_services={service_id: state})
    monkeypatch.setattr(app_settings, "current", lambda: effective)


def _extract(client: object = None, *, kind: str = "pdf", file_path: Path, **kwargs):
    return extract_invoice(
        kind=kind,
        file_path=file_path,
        page_images=kwargs.get("page_images", []),
        text=kwargs.get("text"),
        company_name=COMPANY,
        company_gstin=COMPANY_GSTIN,
        client=client,
    )


# -- the real SDK over a local transport ------------------------------------------------------

_REAL_OPENAI = openai.OpenAI


def _api_completion(
    text: str | None = ANSWER,
    *,
    finish: str = "stop",
    model: str = "google/gemini-2.5-pro",
    **usage: object,
) -> dict:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": text, "refusal": None},
            }
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 800, "total_tokens": 2000, **usage},
    }


class Server:
    """Answers HTTP requests locally, in order, and records them. A reply is a JSON body
    (200), or (status, JSON body or text)."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []
        self.built: list[dict] = []  # how the adapter constructed its SDK clients

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        if isinstance(body, str):
            return httpx2.Response(status, text=body, headers={"content-type": "text/html"})
        return httpx2.Response(status, json=body)

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}:{r.url.port or 443}{r.url.path}" for r in self.requests]

    def client(self, base_url: str, key: str = OPENROUTER_KEY) -> openai.OpenAI:
        return _REAL_OPENAI(
            api_key=key,
            base_url=base_url,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self)),
            max_retries=0,
        )


def _serve(monkeypatch: pytest.MonkeyPatch, *replies: object) -> Server:
    """SDK clients the adapter builds itself talk to a local Server instead of the network."""
    server = Server(*replies)

    def build(**kwargs):
        server.built.append(kwargs)
        # No retries: a scripted error is answered once.
        return _REAL_OPENAI(
            **{**kwargs, "max_retries": 0},
            http_client=httpx2.Client(transport=httpx2.MockTransport(server)),
        )

    monkeypatch.setattr(compatible.openai, "OpenAI", build)
    return server


# Request on the wire


def test_openrouter_request_on_the_wire(monkeypatch, pdf_file: Path, tmp_path: Path):
    _use_service(monkeypatch, "openrouter")
    server = _serve(monkeypatch, _api_completion(cost=0.0123))
    pages = _pages(tmp_path, 2)

    outcome = _extract(file_path=pdf_file, page_images=pages, text=PDF_TEXT)

    assert server.built == [
        {
            "api_key": OPENROUTER_KEY,
            "base_url": "https://openrouter.ai/api/v1",
            "timeout": 180.0,
            "max_retries": 2,
        }
    ]
    assert server.paths() == ["POST openrouter.ai:443/api/v1/chat/completions"]
    assert server.requests[0].headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
    body = server.body()
    assert set(body) == {"model", "messages", "response_format"}  # max_tokens left to it
    assert body["model"] == "google/gemini-2.5-pro"
    assert body["response_format"] == JSON_SCHEMA_FORMAT
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    content = body["messages"][1]["content"]
    assert [part["type"] for part in content] == ["image_url", "image_url", "text"]
    assert [_image_bytes(part) for part in content[:2]] == [p.read_bytes() for p in pages]
    text = content[-1]["text"]
    assert text.startswith(f"<document_text>\n{PDF_TEXT}\n</document_text>")
    assert text.endswith(INSTRUCTIONS)
    assert outcome.extraction == SAMPLE
    assert outcome.model == "google/gemini-2.5-pro"
    assert (outcome.input_tokens, outcome.output_tokens) == (1200, 800)
    assert outcome.cost_usd == 0.0123  # reported by OpenRouter itself


def test_custom_local_server_needs_no_key(monkeypatch, tmp_path: Path):
    _use_service(monkeypatch, "custom", key=None, model="llava:13b", base_url=LOCAL_URL)
    server = _serve(monkeypatch, _api_completion(model="llava:13b"))
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"\x89PNG photo bytes")

    outcome = _extract(kind="image", file_path=photo)

    assert server.built[0]["api_key"] == "not-needed"  # never OPENAI_API_KEY from the env
    assert server.built[0]["base_url"] == LOCAL_URL
    assert server.paths() == ["POST 127.0.0.1:11434/v1/chat/completions"]
    assert server.requests[0].headers["authorization"] == "Bearer not-needed"
    body = server.body()
    assert body["model"] == "llava:13b"
    assert body["response_format"] == JSON_SCHEMA_FORMAT
    content = body["messages"][1]["content"]
    assert [part["type"] for part in content] == ["image_url", "text"]
    assert _image_bytes(content[0]) == photo.read_bytes()
    assert outcome.extraction == SAMPLE
    assert outcome.model == "llava:13b"
    assert outcome.cost_usd == 0.0  # unknown, not guessed


def test_local_server_gets_the_longer_timeout():
    local = FakeClient(_completion())
    _adapter("custom", local, key=None).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )
    hosted = FakeClient(_completion())
    _adapter("groq", hosted).request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert local.calls[0]["timeout"] == 360.0
    assert "timeout" not in hosted.calls[0]  # the client's 180 seconds


def test_text_without_images_is_sent_as_a_plain_string():
    client = FakeClient(_completion())

    _adapter("mistral", client).request(
        Prepared(lead="<document>x</document>"), "Read it", budget=None
    )

    assert _user(client.calls[0]) == "Read it"


# Answer format


def test_rejected_schema_falls_back_to_json_mode_then_to_no_format():
    client = FakeClient(
        _status_error(openai.BadRequestError, 400, "response_format json_schema is not supported"),
        _status_error(openai.BadRequestError, 400, "json_object response format is unavailable"),
        _completion(),
        _completion(),
    )
    adapter = _adapter("custom", client, key=None)
    doc = Prepared(lead="<document>x</document>")

    reply = adapter.request(doc, "Read it", budget=None)

    assert reply.extraction == SAMPLE
    formats = [call.get("response_format") for call in client.calls]
    assert formats == [JSON_SCHEMA_FORMAT, {"type": "json_object"}, None]
    systems = [call["messages"][0]["content"] for call in client.calls]
    assert systems[0] == SYSTEM_PROMPT
    for system in systems[1:]:
        assert system.startswith(SYSTEM_PROMPT)
        assert "Answer with one JSON object and nothing else" in system
        schema = system.rsplit("JSON schema:\n", 1)[1]
        assert json.loads(schema) == to_strict_json_schema(InvoiceExtraction)

    # The working format is remembered for the next request.
    adapter.request(doc, "Read it", budget=None)
    assert len(client.calls) == 4
    assert "response_format" not in client.calls[3]


def test_schema_rejected_with_422_also_falls_back():
    client = FakeClient(
        _status_error(openai.UnprocessableEntityError, 422, "Invalid schema for response"),
        _completion(),
    )

    reply = _adapter("mistral", client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.extraction == SAMPLE
    assert client.calls[1]["response_format"] == {"type": "json_object"}


def test_fallback_chain_over_the_wire():
    server = Server(
        (400, {"error": {"message": "This response_format type is unavailable now"}}),
        (400, {"error": "'response_format.type' must be 'json_schema' or 'text'"}),
        _api_completion(model="deepseek-chat"),
    )
    client = server.client("https://api.deepseek.com/v1")

    reply = _adapter("deepseek", client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.extraction == SAMPLE
    assert server.paths() == ["POST api.deepseek.com:443/v1/chat/completions"] * 3
    assert [server.body(i).get("response_format") for i in range(3)] == [
        JSON_SCHEMA_FORMAT,
        {"type": "json_object"},
        None,
    ]


def test_unrelated_bad_request_is_not_retried():
    client = FakeClient(_status_error(openai.BadRequestError, 400, "Too many messages"))

    with pytest.raises(ExtractionError) as exc_info:
        _adapter(client=client).request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert exc_info.value.message == "OpenRouter could not process this document: Too many messages"
    assert exc_info.value.retryable is False
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        f"Here is the record:\n```json\n{ANSWER}\n```\nLet me know if you need more.",
        f"<think>The seller is {{probably}} Acme.</think>\n{ANSWER}",
        f"Sure. {ANSWER}",
    ],
    ids=["code-fence", "thinking", "leading-sentence"],
)
def test_json_is_found_inside_the_answer(text: str):
    client = FakeClient(_completion(text))

    reply = _adapter(client=client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.extraction == SAMPLE
    assert len(client.calls) == 1


def test_answer_in_parts_is_joined():
    response = _completion(None)
    response.choices[0].message.content = [{"type": "text", "text": ANSWER}]
    client = FakeClient(response)

    reply = _adapter(client=client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.extraction == SAMPLE


# Correcting an answer that does not fit


def test_answer_that_does_not_fit_is_sent_back_once_to_be_corrected():
    client = FakeClient(
        _completion(INCOMPLETE, prompt_tokens=1000, completion_tokens=10),
        _completion(ANSWER, prompt_tokens=1100, completion_tokens=800),
    )

    reply = _adapter(client=client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.extraction == SAMPLE and reply.stop == "done"
    assert len(client.calls) == 2
    first, second = (call["messages"] for call in client.calls)
    assert second[:2] == first
    assert second[2] == {"role": "assistant", "content": INCOMPLETE}
    correction = second[3]
    assert correction["role"] == "user"
    assert "- document_type: Field required" in correction["content"]
    assert "Reply with only the corrected JSON object" in correction["content"]
    assert SCHEMA_TEXT in correction["content"]  # in case the server ignored the schema
    assert client.calls[1]["response_format"] == JSON_SCHEMA_FORMAT
    assert (reply.input_tokens, reply.output_tokens) == (2100, 810)


def test_correction_in_json_mode_does_not_repeat_the_schema():
    client = FakeClient(
        _status_error(openai.BadRequestError, 400, "json_schema not supported"),
        _completion("not json at all"),
        _completion(ANSWER),
    )

    reply = _adapter("xai", client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.extraction == SAMPLE
    correction = client.calls[2]["messages"][-1]["content"]
    assert "Invalid JSON" in correction
    assert SCHEMA_TEXT not in correction  # already in the system message


def test_correction_that_still_does_not_fit_gives_no_extraction(monkeypatch, docx_file: Path):
    _use_service(monkeypatch, "openrouter")
    client = FakeClient(_completion(INCOMPLETE), _completion(INCOMPLETE))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="docx", file_path=docx_file, text=PDF_TEXT)

    assert "did not return any invoice details" in exc_info.value.message
    assert exc_info.value.retryable is False
    assert len(client.calls) == 2


# Page images


def test_rejected_images_are_replaced_by_the_text_layer(pdf_file: Path, tmp_path: Path):
    client = FakeClient(
        _status_error(openai.BadRequestError, 400, "This model does not support image input"),
        _completion(),
        _completion(),
    )
    adapter = _adapter(client=client)
    doc, instructions = _pdf_doc(adapter, pdf_file, _pages(tmp_path, 2), PDF_TEXT)

    reply = adapter.request(doc, instructions, budget=None)

    assert reply.extraction == SAMPLE
    assert [part["type"] for part in _user(client.calls[0])] == ["image_url", "image_url", "text"]
    text_only = _user(client.calls[1])
    assert text_only == f"{instructions}\n\n{TEXT_ONLY_NOTE}"
    assert f"<document_text>\n{PDF_TEXT}\n</document_text>" in text_only
    assert client.calls[1]["response_format"] == JSON_SCHEMA_FORMAT  # not a format problem

    # Remembered: the next request goes straight to the text.
    adapter.request(doc, instructions, budget=None)
    assert _user(client.calls[2]) == text_only


def test_openrouter_without_an_image_provider_reads_the_text(pdf_file: Path, tmp_path: Path):
    client = FakeClient(
        _status_error(openai.NotFoundError, 404, "No endpoints found that support image input"),
        _completion(),
    )
    adapter = _adapter(client=client, model="deepseek/deepseek-chat")
    doc, instructions = _pdf_doc(adapter, pdf_file, _pages(tmp_path, 1), PDF_TEXT)

    reply = adapter.request(doc, instructions, budget=None)

    assert reply.extraction == SAMPLE
    assert isinstance(_user(client.calls[1]), str)


@pytest.mark.parametrize(
    "lead",
    [None, "The PDF is too large to send whole, so only page images are attached."],
    ids=["photo", "scan-without-text"],
)
def test_rejected_images_without_text_fail_clearly(tmp_path: Path, lead: str | None):
    client = FakeClient(_status_error(openai.BadRequestError, 400, "Image input not supported"))
    doc = Prepared(images=_pages(tmp_path, 1), lead=lead)

    with pytest.raises(ExtractionError) as exc_info:
        _adapter("groq", client).request(doc, doc.instructions("Go"), budget=None)

    error = exc_info.value
    assert error.retryable is False
    assert error.message == (
        "Groq could not take the images of this document (Image input not supported). Choose "
        "a model in Settings that can read images, or enter this document manually."
    )
    assert len(client.calls) == 1


def test_deepseek_reads_the_text_layer_only(monkeypatch, pdf_file: Path, tmp_path: Path):
    _use_service(monkeypatch, "deepseek", key="sk-" + "0" * 32)
    client = FakeClient(
        _status_error(openai.BadRequestError, 400, "This response_format type is unavailable now"),
        _completion(model="deepseek-chat"),
    )

    outcome = _extract(client, file_path=pdf_file, page_images=_pages(tmp_path, 2), text=PDF_TEXT)

    assert outcome.extraction == SAMPLE
    assert outcome.model == "deepseek-chat"
    for call in client.calls:
        assert _user(call) == f"<document>\n{PDF_TEXT}\n</document>\n\n{INSTRUCTIONS}"
    assert client.calls[1]["response_format"] == {"type": "json_object"}


def test_deepseek_cannot_read_a_photo(monkeypatch, tmp_path: Path):
    _use_service(monkeypatch, "deepseek", key="sk-" + "0" * 32)
    client = FakeClient()

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="image", file_path=_pages(tmp_path, 1)[0])

    assert "can only read text" in exc_info.value.message
    assert client.calls == []


def test_groq_is_sent_at_most_five_page_images(monkeypatch, pdf_file: Path, tmp_path: Path):
    _use_service(monkeypatch, "groq", key="gsk_test")
    pages = _pages(tmp_path, 8)
    client = FakeClient(_completion())

    _extract(client, file_path=pdf_file, page_images=pages)

    content = _user(client.calls[0])
    images = [part for part in content if part["type"] == "image_url"]
    assert [_image_bytes(part) for part in images] == [
        p.read_bytes() for p in [*pages[:4], pages[7]]
    ]
    assert "pages 1 to 4 and page 8 of 8" in content[-1]["text"]


@pytest.mark.parametrize(
    ("service_id", "images", "payload"),
    [
        ("openrouter", 20, 15_000_000),
        ("groq", 5, 4_000_000),
        ("xai", 20, 15_000_000),
        ("deepseek", 20, 15_000_000),
        ("mistral", 8, 15_000_000),
        ("custom", 20, 15_000_000),
    ],
)
def test_limits_never_take_a_pdf_file(service_id: str, images: int, payload: int):
    assert _adapter(service_id).limits() == Limits(
        max_pdf_bytes=0, pdf_label="", max_image_payload=payload, max_images=images
    )


# Answer length


def test_cut_off_answer_is_asked_again_with_a_larger_budget(monkeypatch, docx_file: Path):
    _use_service(monkeypatch, "openrouter")
    client = FakeClient(
        _completion(
            '{"is_invoice": tr', finish="length", prompt_tokens=1000, completion_tokens=4096
        ),
        _completion(ANSWER, prompt_tokens=1000, completion_tokens=6000),
    )

    outcome = _extract(client, kind="docx", file_path=docx_file, text=PDF_TEXT)

    assert outcome.extraction == SAMPLE
    assert "max_tokens" not in client.calls[0] and "timeout" not in client.calls[0]
    assert client.calls[1]["max_tokens"] == 16384
    assert client.calls[1]["timeout"] == 360.0
    assert client.calls[1]["messages"] == client.calls[0]["messages"]
    assert (outcome.input_tokens, outcome.output_tokens) == (2000, 10096)


def test_server_that_caps_the_budget_is_asked_with_its_own_limit(monkeypatch, pdf_file: Path):
    _use_service(monkeypatch, "deepseek", key="sk-" + "0" * 32)
    cap = "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]"
    client = FakeClient(
        _completion(INCOMPLETE[:10], finish="length"),
        _status_error(openai.BadRequestError, 400, cap),
        _completion(ANSWER),
    )

    outcome = _extract(client, file_path=pdf_file, text=PDF_TEXT, page_images=[])

    assert outcome.extraction == SAMPLE
    assert [call.get("max_tokens") for call in client.calls] == [None, 16384, 8192]


def test_server_that_refuses_the_larger_budget_reports_a_cut_off(monkeypatch, docx_file: Path):
    _use_service(monkeypatch, "groq", key="gsk_test")
    client = FakeClient(
        _completion(INCOMPLETE[:10], finish="length"),
        _status_error(openai.BadRequestError, 400, "max_tokens is too large for this model"),
    )

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="docx", file_path=docx_file, text=PDF_TEXT)

    assert "cut off" in exc_info.value.message
    assert exc_info.value.retryable is False
    assert len(client.calls) == 2


def test_cut_off_twice_fails(monkeypatch, docx_file: Path):
    _use_service(monkeypatch, "mistral", key="mistral-test")
    client = FakeClient(
        _completion(INCOMPLETE[:10], finish="length"),
        _completion(INCOMPLETE[:10], finish="model_length"),
    )

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="docx", file_path=docx_file, text=PDF_TEXT)

    assert exc_info.value.message.startswith("Mistral's answer for this document was cut off")
    assert len(client.calls) == 2


# Refusals


def test_refusal_is_not_retried_and_gives_the_reason(monkeypatch, docx_file: Path):
    _use_service(monkeypatch, "openrouter")
    client = FakeClient(_completion(None, refusal="I can't help with that request."))

    with pytest.raises(ExtractionError) as exc_info:
        _extract(client, kind="docx", file_path=docx_file, text=PDF_TEXT)

    assert exc_info.value.message == (
        "OpenRouter declined to read this document (reason: I can't help with that request). "
        "Enter it manually."
    )
    assert len(client.calls) == 1


def test_content_filter_is_a_refusal():
    client = FakeClient(_completion("", finish="content_filter"))

    reply = _adapter(client=client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert (reply.stop, reply.refusal_reason, reply.extraction) == (
        "refused",
        "content filter",
        None,
    )
    assert len(client.calls) == 1


def test_input_flagged_by_moderation_is_a_refusal():
    client = FakeClient(
        _status_error(openai.PermissionDeniedError, 403, "Your input was flagged by moderation")
    )

    reply = _adapter(client=client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert (reply.stop, reply.refusal_reason) == ("refused", "flagged by moderation")


# Usage


def test_cached_tokens_are_counted_apart_and_a_reported_cost_is_kept():
    client = FakeClient(
        _completion(prompt_tokens=1000, completion_tokens=500, cached=400, cost=0.0123)
    )

    reply = _adapter(client=client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert (reply.input_tokens, reply.cache_read_tokens, reply.output_tokens) == (600, 400, 500)
    assert reply.cost == Decimal("0.0123")


def test_answer_without_model_or_usage_is_attributed_to_the_configured_model():
    response = _completion(model=None)
    response.usage = None
    client = FakeClient(response)

    reply = _adapter("xai", client).request(
        Prepared(lead="<document>x</document>"), "Go", budget=None
    )

    assert reply.model == "grok-4"
    assert (reply.input_tokens, reply.output_tokens, reply.cost) == (0, 0, Decimal("0"))


# Errors


@pytest.mark.parametrize(
    ("error", "retryable", "expected"),
    [
        (
            _status_error(openai.AuthenticationError, 401),
            False,
            "The OpenRouter API key was rejected. An administrator can check it in Settings",
        ),
        (
            _status_error(openai.PermissionDeniedError, 403),
            False,
            "OpenRouter API key was rejected",
        ),
        (
            _status_error(openai.BadRequestError, 400, "messages: bad shape"),
            False,
            "OpenRouter could not process this document: messages: bad shape",
        ),
        (
            _status_error(openai.UnprocessableEntityError, 422, "extra fields not permitted"),
            False,
            "OpenRouter could not process this document: extra fields not permitted",
        ),
        (
            _status_error(openai.NotFoundError, 404, "google/nope is not a valid model ID"),
            False,
            "OpenRouter could not find the model google/gemini-2.5-pro (google/nope is not a",
        ),
        (_status_error(openai.APIStatusError, 413), False, "too large to send to OpenRouter"),
        (_status_error(openai.APIStatusError, 402, "Insufficient credits"), False, "no credit"),
        (_status_error(openai.RateLimitError, 429), True, "OpenRouter's usage limit"),
        (openai.APITimeoutError(request=_REQUEST), True, "OpenRouter took too long"),
        (
            openai.APIConnectionError(request=_REQUEST),
            True,
            "Could not reach OpenRouter. Check the internet connection",
        ),
        (_status_error(openai.InternalServerError, 500), True, "temporarily unavailable"),
        (_status_error(openai.APIStatusError, 503, "upstream"), True, "upstream"),
        (_status_error(openai.APIStatusError, 418, "teapot"), False, "returned an error: teapot"),
    ],
    ids=[
        "auth",
        "permission",
        "bad-request",
        "unprocessable",
        "not-found",
        "too-large",
        "no-credit",
        "rate-limit",
        "timeout",
        "connection",
        "server-error",
        "other-5xx",
        "other-4xx",
    ],
)
def test_sdk_errors_are_mapped(error: Exception, retryable: bool, expected: str):
    client = FakeClient(error)

    with pytest.raises(ExtractionError) as exc_info:
        _adapter(client=client).request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert exc_info.value.retryable is retryable
    assert expected in exc_info.value.message
    assert exc_info.value.__cause__ is error
    assert OPENROUTER_KEY not in exc_info.value.message


def test_rejected_key_waits_for_a_new_one():
    client = FakeClient(_status_error(openai.AuthenticationError, 401, "Invalid API key"))

    with pytest.raises(ExtractionError) as exc_info:
        _adapter("groq", client, key="gsk_bad").request(
            Prepared(lead="<document>x</document>"), "Go", budget=None
        )

    assert extractor.waiting_for_ai(exc_info.value.message)
    assert "GROQ_API_KEY" in exc_info.value.message


def test_unreachable_local_server_names_its_address_and_not_its_password():
    client = FakeClient(openai.APIConnectionError(request=_REQUEST))
    adapter = _adapter(
        "custom", client, key=None, base_url="http://office:s3cret@192.168.1.30:11434/v1"
    )

    with pytest.raises(ExtractionError) as exc_info:
        adapter.request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert exc_info.value.message == (
        "Could not reach the AI service at 192.168.1.30:11434. Check the address in Settings "
        "and that the server is running."
    )
    assert exc_info.value.retryable is True


def test_a_key_echoed_by_the_server_is_not_repeated():
    error = _status_error(openai.BadRequestError, 400, f"Bad request for key {OPENROUTER_KEY}")
    client = FakeClient(error)

    with pytest.raises(ExtractionError) as exc_info:
        _adapter(client=client).request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert OPENROUTER_KEY not in exc_info.value.message
    assert "Bad request for key [key]" in exc_info.value.message


def test_a_web_page_at_the_address_is_not_an_answer():
    server = Server((200, "<html><body>Router login</body></html>"))
    client = server.client("http://192.168.1.1/v1", key="not-needed")

    with pytest.raises(ExtractionError) as exc_info:
        _adapter("custom", client, key=None).request(
            Prepared(lead="<document>x</document>"), "Go", budget=None
        )

    assert "Check the address in Settings" in exc_info.value.message
    assert exc_info.value.retryable is False


def test_empty_answer_can_be_tried_again():
    response = _completion()
    response.choices = []
    client = FakeClient(response)

    with pytest.raises(ExtractionError) as exc_info:
        _adapter(client=client).request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert exc_info.value.retryable is True
    assert "did not finish its answer" in exc_info.value.message


# Settings check


def test_check_finds_the_model_in_the_list():
    model = "meta-llama/llama-4-maverick-17b-128e-instruct"
    client = FakeClient(models=["whisper-large-v3", model])

    result = _adapter("groq", client, key="gsk_test").check()

    assert result.ok is True
    assert result.detail == f"Connected. {model} is available."
    assert client.calls == []  # no tokens spent


def test_check_reports_a_model_the_key_cannot_use():
    client = FakeClient(models=["mistral-small-latest"])

    result = _adapter("mistral", client, key="m-test").check()

    assert result.ok is False
    assert "mistral-medium-latest is not one of the models Mistral offers" in result.detail


def test_check_accepts_an_ollama_model_without_its_tag():
    client = FakeClient(models=["llava:latest", "qwen2.5vl:7b"])

    assert _adapter("custom", client, key=None, model="llava").check().ok is True


@pytest.mark.parametrize(
    "models",
    [[], _status_error(openai.NotFoundError, 404, "404 page not found")],
    ids=["empty-list", "no-list"],
)
def test_check_without_a_model_list_asks_for_a_short_answer(models: object):
    client = FakeClient(_completion("OK"), models=models)

    result = _adapter("custom", client, key=None, model="local-model").check()

    assert result.ok is True
    assert result.detail == "Connected. local-model answered."
    call = client.calls[0]
    assert call["model"] == "local-model" and call["max_tokens"] == 5
    assert call["messages"] == [{"role": "user", "content": "Reply with the word OK."}]


def test_check_maps_a_rejected_key():
    client = FakeClient(models=_status_error(openai.AuthenticationError, 401, "bad key"))

    result = _adapter("xai", client, key="xai-bad").check()

    assert result.ok is False
    assert result.detail.startswith("The key was rejected by Grok.")


def test_check_says_when_a_local_server_wants_a_key():
    client = FakeClient(models=_status_error(openai.AuthenticationError, 401))

    result = _adapter("custom", client, key=None).check()

    assert result.detail == (
        "The AI service at 127.0.0.1:11434 needs an API key. Enter its key above, save it, "
        "and test again."
    )


def test_check_says_a_local_server_may_not_be_running():
    client = FakeClient(models=openai.APIConnectionError(request=_REQUEST))

    result = _adapter("custom", client, key=None).check()

    assert result.ok is False
    assert "127.0.0.1:11434" in result.detail and "the server is running" in result.detail


def test_check_unknown_model_on_a_local_server():
    client = FakeClient(
        _status_error(openai.NotFoundError, 404, 'model "nope" not found'),
        models=[],
    )

    result = _adapter("custom", client, key=None, model="nope").check()

    assert result.ok is False
    assert result.detail.startswith("nope was not found at 127.0.0.1:11434.")
    assert "/v1" in result.detail


@pytest.mark.parametrize(
    ("key_reply", "ok", "detail"),
    [
        ({"data": {"label": "office"}}, True, "Connected. google/gemini-2.5-pro is available."),
        (
            (401, {"error": {"message": "No auth credentials found"}}),
            False,
            "rejected by OpenRouter",
        ),
    ],
    ids=["good-key", "bad-key"],
)
def test_check_on_openrouter_also_proves_the_key(monkeypatch, key_reply, ok: bool, detail: str):
    listing = {"data": [{"id": "openai/gpt-5"}, {"id": "google/gemini-2.5-pro"}]}
    server = _serve(monkeypatch, listing, key_reply)

    result = _adapter("openrouter").check()

    assert (result.ok, detail in result.detail) == (ok, True)
    assert server.paths() == [
        "GET openrouter.ai:443/api/v1/models",
        "GET openrouter.ai:443/api/v1/key",
    ]
    assert server.requests[1].headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert server.built == [
        {
            "api_key": OPENROUTER_KEY,
            "base_url": "https://openrouter.ai/api/v1",
            "timeout": 20.0,
            "max_retries": 0,
        }
    ]


def test_check_on_a_local_server_without_a_model_list(monkeypatch):
    server = _serve(monkeypatch, (404, "404 page not found"), _api_completion("OK", model="llava"))

    result = _adapter("custom", key=None, model="llava").check()

    assert result.ok is True
    assert server.paths() == [
        "GET 127.0.0.1:11434/v1/models",
        "POST 127.0.0.1:11434/v1/chat/completions",
    ]
    assert server.body(1)["max_tokens"] == 5
    assert server.requests[1].headers["authorization"] == "Bearer not-needed"


def test_check_against_something_that_is_not_an_ai_service(monkeypatch):
    _serve(monkeypatch, (200, "<html>Welcome</html>"), (200, "<html>Welcome</html>"))

    result = _adapter("custom", key=None, base_url="http://192.168.1.1/v1").check()

    assert result.ok is False
    assert "not like an OpenAI-compatible AI service" in result.detail


# Model list


def test_openrouter_models_that_read_images_come_first(monkeypatch):
    listing = {
        "data": [
            {"id": "deepseek/deepseek-chat", "architecture": {"input_modalities": ["text"]}},
            {"id": "openai/gpt-5", "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "meta/llama-text"},
            {
                "id": "google/gemini-2.5-pro",
                "architecture": {"input_modalities": ["image", "text", "file"]},
            },
            {"id": "openai/gpt-5", "architecture": {"input_modalities": ["text", "image"]}},
        ]
    }
    server = _serve(monkeypatch, listing)

    models = _adapter("openrouter").list_models()

    assert models == [
        "openai/gpt-5",
        "google/gemini-2.5-pro",
        "deepseek/deepseek-chat",
        "meta/llama-text",
    ]
    assert server.paths() == ["GET openrouter.ai:443/api/v1/models"]


def test_mistral_vision_models_come_first():
    client = FakeClient(
        models=[
            {"id": "mistral-embed", "capabilities": {"vision": False}},
            {"id": "codestral-latest", "capabilities": {"completion_chat": True}},
            {"id": "mistral-medium-latest", "capabilities": {"vision": True}},
        ]
    )

    assert _adapter("mistral", client).list_models() == [
        "mistral-medium-latest",
        "mistral-embed",
        "codestral-latest",
    ]


def test_text_only_service_keeps_the_listed_order():
    client = FakeClient(models=["deepseek-reasoner", "deepseek-chat"])

    assert _adapter("deepseek", client).list_models() == ["deepseek-reasoner", "deepseek-chat"]


# Configuration


def test_custom_service_without_an_address_is_not_set_up(monkeypatch, tmp_path: Path):
    _use_service(monkeypatch, "custom", key=None, model="llava")  # no base_url

    with pytest.raises(ExtractionError) as exc_info:
        _extract(kind="image", file_path=_pages(tmp_path, 1)[0])

    assert exc_info.value.message.startswith("Invoice reading is not set up")


def test_adapter_without_an_address_never_falls_back_to_openai():
    adapter = CompatibleAdapter(catalog.get("custom"), "sk-secret", "llava", None)

    with pytest.raises(ExtractionError) as exc_info:
        adapter.request(Prepared(lead="<document>x</document>"), "Go", budget=None)

    assert "no address" in exc_info.value.message
    assert adapter.check().ok is False
