"""Reads documents with any service that speaks the OpenAI chat-completions protocol at its
own address: OpenRouter, Groq, xAI, DeepSeek, Mistral, and the "Other" service an office
enters itself (Together, Fireworks, or Ollama / LM Studio on its own network, which need no
key).

None of them takes a PDF file, so a document arrives as page images (with the PDF's text
layer) or as text alone. The servers differ in what else they accept, so a request steps
down until one is accepted and the adapter remembers the step: a strict JSON schema, then
JSON mode with the schema in the system message, then no answer format at all; page images,
then the text layer alone. An answer that doesn't fit the schema is sent back once to be
corrected.
"""

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import openai
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from app.extraction.base import (
    Check,
    ExtractionError,
    ExtractionNotConfigured,
    Limits,
    Prepared,
    Reply,
    Stop,
    b64,
    image_media_type,
    too_large,
)
from app.extraction.extractor import key_rejected_message
from app.extraction.prompt import SYSTEM_PROMPT
from app.extraction.services import Service
from app.schemas.extraction import InvoiceExtraction

CLIENT_TIMEOUT_SECONDS = 180.0
# A longer answer, or a model running on one of the office's own computers, takes longer.
SLOW_TIMEOUT_SECONDS = 360.0
CHECK_TIMEOUT_SECONDS = 20.0
CHECK_ANSWER_TIMEOUT_SECONDS = 60.0  # a local server may first have to load the model
RETRY_MAX_TOKENS = 16384
# Local servers ignore the key, and with None the SDK would send OPENAI_API_KEY instead.
NO_KEY = "not-needed"

MAX_IMAGES = 20
MAX_IMAGE_PAYLOAD = 15_000_000  # base64 characters across all page images
# Services that publish tighter per-request limits: (images, base64 characters).
IMAGE_LIMITS: dict[str, tuple[int, int]] = {
    "groq": (5, 4_000_000),
    "mistral": (8, MAX_IMAGE_PAYLOAD),
}

SCHEMA_NAME = "invoice_extraction"
SCHEMA = to_strict_json_schema(InvoiceExtraction)
SCHEMA_TEXT = json.dumps(SCHEMA, separators=(",", ":"))
# Answer formats, strictest first; a server that rejects one gets the next.
MODES = ("json_schema", "json_object", "plain")
SCHEMA_PROMPT = (
    "\n# Answer format\n\n"
    "Answer with one JSON object and nothing else: no explanation and no code fences. It "
    f"must follow this JSON schema:\n{SCHEMA_TEXT}\n"
)
TEXT_ONLY_NOTE = (
    "The page images could not be sent to this model, so only the PDF's text layer above "
    "is available. Read the document from that text, and say in notes that the page images "
    "were not seen."
)

# Words in a rejection that name what the server would not take.
_FORMAT_WORDS = (
    "response_format",
    "response format",
    "json_schema",
    "json schema",
    "json_object",
    "json mode",
    "structured output",
    "schema",
)
_IMAGE_WORDS = ("image", "vision", "multimodal", "multi-modal", "mmproj", "must be a string")
_LENGTH_WORDS = (
    "max_tokens",
    "max_completion_tokens",
    "max tokens",
    "maximum context length",
    "context length",
    "context_length",
    "context window",
)
_CUT_OFF = {"length", "model_length", "max_tokens"}
_NO_MODEL_LIST = {404, 405, 501}  # servers without GET /models
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class CompatibleAdapter:
    # First leave the answer length to the server; a cut-off answer is asked again with a
    # fixed, larger budget.
    budgets = (None, RETRY_MAX_TOKENS)

    def __init__(
        self,
        service: Service,
        key: str | None,
        model: str,
        base_url: str | None,
        client: Any = None,
    ) -> None:
        self.service = service
        self.model = model
        self.base_url = base_url
        self._key = key
        self._client = client
        self._injected = client is not None  # a test's client also serves check()
        self._mode = 0  # index into MODES
        self._images_refused = False

    def limits(self) -> Limits:
        # Read at call time: tests (and future tuning) change the module values.
        images, payload = IMAGE_LIMITS.get(self.service.id, (MAX_IMAGES, MAX_IMAGE_PAYLOAD))
        return Limits(max_pdf_bytes=0, pdf_label="", max_image_payload=payload, max_images=images)

    # -- reading ---------------------------------------------------------------------------

    def request(self, doc: Prepared, instructions: str, *, budget: int | None) -> Reply:
        try:
            return self._read(doc, instructions, budget)
        except openai.PermissionDeniedError as exc:
            if _mentions(exc, ("flagged", "moderation")):
                return Reply(None, "refused", self.model, refusal_reason="flagged by moderation")
            raise self._error(exc) from exc
        except openai.APIError as exc:
            raise self._error(exc) from exc

    def _read(self, doc: Prepared, instructions: str, budget: int | None) -> Reply:
        turns = self._turns(doc, instructions)
        try:
            response = self._complete(turns, budget)
        except openai.APIStatusError as exc:
            if self._images_refused or not doc.images or not _refuses_images(exc):
                raise
            if not _has_text(doc):
                raise ExtractionError(
                    f"{self.service.short} could not take the images of this document "
                    f"({self._message(exc)}). Choose a model in Settings that can read "
                    "images, or enter this document manually.",
                    retryable=False,
                ) from exc
            self._images_refused = True
            turns = self._turns(doc, instructions)
            response = self._complete(turns, budget)
        return self._answer(response, turns, budget)

    def _turns(self, doc: Prepared, instructions: str) -> list[dict]:
        if self._images_refused:
            return [_user_turn([], f"{instructions}\n\n{TEXT_ONLY_NOTE}")]
        return [_user_turn(doc.images, instructions)]

    def _answer(self, response: Any, turns: list[dict], budget: int | None) -> Reply:
        """Reads the answer. One that doesn't fit the schema is sent back once, with what is
        wrong with it, to be corrected; both are billed."""
        usage = _Usage()
        reply, invalid = self._reply(response, usage)
        if invalid is None:
            return reply
        text, exc = invalid
        turns = [
            *turns,
            {"role": "assistant", "content": text},
            {"role": "user", "content": self._correction(exc)},
        ]
        reply, _ = self._reply(self._complete(turns, budget), usage)
        return reply

    def _reply(
        self, response: Any, usage: "_Usage"
    ) -> tuple[Reply, tuple[str, ValidationError] | None]:
        """The reply to one answer, plus the answer's text and problems when it doesn't fit
        the schema."""
        if response is None:  # the server won't give an answer this long
            return usage.reply(None, "cut_off", self.model), None
        usage.add(response)
        model = getattr(response, "model", None) or self.model
        message, finish = self._choice(response)
        refusal = getattr(message, "refusal", None)
        if refusal or finish == "content_filter":
            return usage.reply(None, "refused", model, _reason(refusal)), None
        if finish in _CUT_OFF:
            return usage.reply(None, "cut_off", model), None
        text = _content(message)
        try:
            extraction = InvoiceExtraction.model_validate_json(_json_text(text))
        except ValidationError as exc:
            return usage.reply(None, "done", model), (text, exc)
        return usage.reply(extraction, "done", model), None

    def _complete(self, turns: list[dict], budget: int | None) -> Any | None:
        """One chat completion in the strictest answer format the server accepts. None when
        the server won't give an answer of this length."""
        shrunk = False
        while True:
            try:
                return self._sdk().chat.completions.create(**self._params(turns, budget))
            except openai.APIStatusError as exc:
                rejected = exc.status_code in (400, 422)
                if rejected and self._mode < len(MODES) - 1 and _mentions(exc, _FORMAT_WORDS):
                    self._mode += 1
                    continue
                if rejected and budget is not None and _mentions(exc, _LENGTH_WORDS):
                    smaller = None if shrunk else _allowed_budget(exc, budget)
                    if smaller is None:
                        return None
                    budget, shrunk = smaller, True
                    continue
                raise

    def _params(self, turns: list[dict], budget: int | None) -> dict[str, Any]:
        mode = MODES[self._mode]
        system = SYSTEM_PROMPT if mode == "json_schema" else SYSTEM_PROMPT + SCHEMA_PROMPT
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *turns],
        }
        if mode == "json_schema":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": SCHEMA},
            }
        elif mode == "json_object":
            params["response_format"] = {"type": "json_object"}
        if budget is not None:
            params["max_tokens"] = budget
        if budget is not None or self.service.custom_base_url:
            params["timeout"] = SLOW_TIMEOUT_SECONDS
        return params

    def _correction(self, exc: ValidationError) -> str:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        problems = "\n".join(
            f"- {'.'.join(str(part) for part in error['loc']) or 'the answer'}: {error['msg']}"
            for error in errors[:20]
        )
        text = (
            f"Your answer could not be read as the invoice record:\n{problems}\n"
            "Reply with only the corrected JSON object, with every field the record needs, "
            "and nothing else."
        )
        if MODES[self._mode] == "json_schema":
            # A server that ignored the schema needs it spelled out.
            text += f" It must follow this JSON schema:\n{SCHEMA_TEXT}"
        return text

    def _choice(self, response: Any) -> tuple[Any, str | None]:
        if isinstance(response, str):  # the SDK hands back a body that isn't JSON as text
            raise ExtractionError(
                f"{self.service.short} sent back a page this program can't read. "
                + (
                    "Check the address in Settings."
                    if self.service.custom_base_url
                    else "Check the internet connection and try again."
                ),
                retryable=not self.service.custom_base_url,
            )
        choices = getattr(response, "choices", None) or []
        finish = getattr(choices[0], "finish_reason", None) if choices else None
        if not choices or finish == "error":  # OpenRouter: the model failed mid-answer
            raise ExtractionError(
                f"{self.service.short} did not finish its answer. Try again in a few minutes.",
                retryable=True,
            )
        return getattr(choices[0], "message", None), finish

    def _sdk(self) -> Any:
        if self._client is None:
            self._client = self._new_client(CLIENT_TIMEOUT_SECONDS, max_retries=2)
        return self._client

    def _new_client(self, timeout: float, max_retries: int) -> openai.OpenAI:
        if not self.base_url:
            # Without one the SDK would send the key to api.openai.com.
            raise ExtractionNotConfigured(
                "Invoice reading is not set up because no address was entered for the AI "
                "service. An administrator can add it in Settings."
            )
        return openai.OpenAI(
            api_key=self._key or NO_KEY,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    # -- errors ------------------------------------------------------------------------------

    def _error(self, exc: openai.APIError) -> ExtractionError:
        # Order matters: APITimeoutError is an APIConnectionError, and every HTTP error is an
        # APIStatusError.
        name = self.service.short
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return ExtractionError(key_rejected_message(self.service), retryable=False)
        status = exc.status_code if isinstance(exc, openai.APIStatusError) else None
        if status == 413:
            return ExtractionError(too_large(self.service), retryable=False)
        if isinstance(exc, openai.NotFoundError):
            where = " and the address" if self.service.custom_base_url else ""
            return ExtractionError(
                f"{name} could not find the model {self.model} ({self._message(exc)}). Check "
                f"the model name{where} in Settings.",
                retryable=False,
            )
        if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
            return ExtractionError(
                f"{name} could not process this document: {self._message(exc)}",
                retryable=False,
            )
        if status == 402:
            return ExtractionError(
                f"{name} says the account has no credit left. Add credit to the account, "
                "then send the document again.",
                retryable=False,
            )
        if isinstance(exc, openai.RateLimitError):
            return ExtractionError(
                f"{name}'s usage limit has been reached for now. Try again in a minute.",
                retryable=True,
            )
        if isinstance(exc, openai.APITimeoutError):
            return ExtractionError(f"{name} took too long to answer. Try again.", retryable=True)
        if isinstance(exc, openai.APIConnectionError):
            return ExtractionError(self._unreachable(), retryable=True)
        if isinstance(exc, openai.InternalServerError):
            return ExtractionError(
                f"{name} is temporarily unavailable. Try again in a few minutes.", retryable=True
            )
        if status is not None:
            return ExtractionError(
                f"{name} returned an error: {self._message(exc)}",
                retryable=status in (408, 409) or status >= 500,
            )
        return ExtractionError(f"{name} returned an error: {self._message(exc)}", retryable=False)

    def _unreachable(self) -> str:
        if self.service.custom_base_url:
            return (
                f"Could not reach the AI service at {self._where()}. Check the address in "
                "Settings and that the server is running."
            )
        return f"Could not reach {self.service.short}. Check the internet connection and try again."

    def _where(self) -> str:
        """The server's host and port, for messages; never any credentials in the URL."""
        try:
            parts = urlsplit(self.base_url or "")
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            return "the address in Settings"
        return f"{parts.hostname}{port}" if parts.hostname else "the address in Settings"

    def _message(self, exc: openai.APIError) -> str:
        text = " ".join(_api_message(exc).split())
        if self._key:
            text = text.replace(self._key, "[key]")
        return text[:300]

    # -- settings screen ---------------------------------------------------------------------

    def check(self) -> Check:
        """Looks the model up in the service's model list, which proves the key and address
        without spending tokens; a server without a list is asked for a one-word answer."""
        try:
            client = self._check_client()
        except ExtractionError as exc:
            return Check(ok=False, detail=exc.message)
        try:
            return self._check(client)
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            if not self._key and self.service.key_optional:
                return Check(
                    ok=False,
                    detail=f"The AI service at {self._where()} needs an API key. Enter its key "
                    "above, save it, and test again.",
                )
            return Check(
                ok=False,
                detail=f"The key was rejected by {self._name()}. Check that it was copied in "
                "full and has not been revoked, then save it again.",
            )
        except openai.NotFoundError:
            if self.service.custom_base_url:
                return Check(
                    ok=False,
                    detail=f"{self.model} was not found at {self._where()}. Check the model "
                    "name and the address; for Ollama and LM Studio the address usually ends "
                    "in /v1.",
                )
            return Check(
                ok=False,
                detail=f"{self.model} is not available to this key. Choose another model.",
            )
        except openai.RateLimitError:
            return Check(
                ok=False,
                detail=f"{self.service.short} is busy or its usage limit has been reached. "
                "Try again in a minute.",
            )
        except openai.APIConnectionError:  # includes timeouts
            if self.service.custom_base_url:
                return Check(
                    ok=False,
                    detail=f"Could not reach the AI service at {self._where()}. Check the "
                    "address and that the server is running, then test again.",
                )
            return Check(
                ok=False,
                detail=f"Could not reach {self.service.short}. Check this computer's internet "
                "connection and try again.",
            )
        except openai.APIStatusError as exc:
            if exc.status_code == 402:
                return Check(
                    ok=False,
                    detail=f"{self.service.short} says the account has no credit left. Add "
                    "credit to the account and test again.",
                )
            if exc.status_code >= 500:
                return Check(
                    ok=False,
                    detail=f"{self.service.short} returned an error (HTTP {exc.status_code}). "
                    "Try again in a few minutes.",
                )
            return Check(
                ok=False,
                detail=f"{self.service.short} returned an error (HTTP {exc.status_code}): "
                f"{self._message(exc)}",
            )
        except openai.OpenAIError:
            return Check(
                ok=False,
                detail=f"{self.service.short} could not be checked. Try again in a few minutes.",
            )
        finally:
            if not self._injected:
                client.close()

    def _check(self, client: Any) -> Check:
        try:
            ids = _model_ids(client.models.list())
        except openai.APIStatusError as exc:
            if exc.status_code not in _NO_MODEL_LIST:
                raise
            ids = []
        except openai.OpenAIError:
            raise
        except Exception:  # noqa: BLE001 - a list the SDK can't read (not JSON, or no "data")
            ids = []
        if not ids:
            return self._check_answer(client)
        if self.model not in ids and f"{self.model}:latest" not in ids:  # Ollama's tags
            return Check(
                ok=False,
                detail=f"{self.model} is not one of the models {self._name()} offers this key. "
                "Choose one from the list, or check the spelling.",
            )
        if self.service.id == "openrouter":
            # OpenRouter lists its models to anyone, so the key is proved on its own.
            try:
                client.get("/key", cast_to=object)
            except openai.NotFoundError:
                return self._check_answer(client)
        return Check(ok=True, detail=f"Connected. {self.model} is available.")

    def _check_answer(self, client: Any) -> Check:
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
            max_tokens=5,
            timeout=CHECK_ANSWER_TIMEOUT_SECONDS,
        )
        if isinstance(response, str) or not getattr(response, "choices", None):
            if self.service.custom_base_url:
                return Check(
                    ok=False,
                    detail=f"Something answered at {self._where()}, but not like an OpenAI-"
                    "compatible AI service. Check the address; it usually ends in /v1.",
                )
            return Check(
                ok=False,
                detail=f"{self.service.short} sent back a page this program can't read. Check "
                "this computer's internet connection and try again.",
            )
        return Check(ok=True, detail=f"Connected. {self.model} answered.")

    def list_models(self) -> list[str]:
        """Model ids from the service's list; where the list says which models read images
        (OpenRouter, Mistral) and this service is sent images, those come first."""
        client = self._check_client()
        try:
            listed = _listed_models(client.models.list())
        finally:
            if not self._injected:
                client.close()
        reads_images: dict[str, bool] = {}
        for model_id, reads in listed:
            reads_images.setdefault(model_id, reads)
        ids = list(reads_images)
        if self.service.reads_images:
            ids.sort(key=lambda model_id: not reads_images[model_id])  # stable: keeps order
        return ids

    def _check_client(self) -> Any:
        if self._injected:
            return self._client
        return self._new_client(CHECK_TIMEOUT_SECONDS, max_retries=0)

    def _name(self) -> str:
        """The service, for use mid-sentence."""
        if self.service.custom_base_url:
            return f"the AI service at {self._where()}"
        return self.service.short


# -- helpers --------------------------------------------------------------------------------


class _Usage:
    """Totals across the answer and its correction: both are billed. Input tokens exclude
    cached ones, as Claude reports them."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cost = Decimal("0")

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt = _count(getattr(usage, "prompt_tokens", None))
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _count(getattr(details, "cached_tokens", None)) or _count(
            getattr(usage, "prompt_cache_hit_tokens", None)  # DeepSeek
        )
        cached = min(cached, prompt)
        self.input_tokens += prompt - cached
        self.cache_read_tokens += cached
        self.output_tokens += _count(getattr(usage, "completion_tokens", None))
        # Prices differ per model and provider, so only a cost the service reports itself
        # (OpenRouter does, in US dollars) is counted; otherwise it stays unknown (0).
        cost = getattr(usage, "cost", None)
        if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
            self.cost += Decimal(str(cost))

    def reply(
        self,
        extraction: InvoiceExtraction | None,
        stop: Stop,
        model: str,
        reason: str | None = None,
    ) -> Reply:
        return Reply(
            extraction=extraction,
            stop=stop,
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cost=self.cost,
            refusal_reason=reason,
        )


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _user_turn(images: list, text: str) -> dict:
    if not images:
        # Plain text is what every server takes, including text-only ones.
        return {"role": "user", "content": text}
    parts: list[dict] = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image_media_type(path)};base64,{b64(path)}"},
        }
        for path in images
    ]
    return {"role": "user", "content": [*parts, {"type": "text", "text": text}]}


def _has_text(doc: Prepared) -> bool:
    """Whether the document's own text was sent along (a PDF's text layer), not only a note
    about left-out pages: without it, a text-only request would have nothing to read."""
    return "<document" in (doc.lead or "")


def _content(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, list):  # some servers answer in parts
        content = "".join(part.get("text") or "" for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def _json_text(text: str) -> str:
    """The JSON object in an answer, without a reasoning model's thinking, code fences or
    the sentence some models put before it."""
    text = _THINKING.sub("", text).strip()
    if fenced := _FENCE.search(text):
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def _reason(refusal: Any) -> str:
    if isinstance(refusal, str) and refusal.strip():
        return " ".join(refusal.split())[:200].rstrip(".")
    return "content filter"


def _api_message(exc: openai.APIError) -> str:
    # The SDK keeps the "error" object of the body; some servers send a bare string instead.
    body = exc.body
    if isinstance(body, str) and body.strip():
        return body
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        message = body["message"]
        metadata = body.get("metadata")
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        # OpenRouter puts the upstream provider's own words here.
        return f"{message}: {raw}" if isinstance(raw, str) and raw.strip() else message
    return exc.message


def _mentions(exc: openai.APIError, words: tuple[str, ...]) -> bool:
    body = exc.body if isinstance(exc.body, str) else json.dumps(exc.body, default=str)
    text = f"{exc.message} {body}".lower()
    return any(word in text for word in words)


def _refuses_images(exc: openai.APIStatusError) -> bool:
    # OpenRouter answers 404 when no provider of the model takes images; llama.cpp 500.
    return exc.status_code in (400, 404, 415, 422, 500) and _mentions(exc, _IMAGE_WORDS)


def _allowed_budget(exc: openai.APIError, budget: int) -> int | None:
    """The largest answer length the server says it allows, when it names one below what
    was asked ("the valid range of max_tokens is [1, 8192]")."""
    if not _mentions(exc, ("max_tokens", "max_completion_tokens")):
        return None
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", _api_message(exc))]
    allowed = [n for n in numbers if 1024 <= n < budget]
    return max(allowed) if allowed else None


def _model_ids(page: Any) -> list[str]:
    return [model_id for model_id, _ in _listed_models(page)]


def _listed_models(page: Any) -> list[tuple[str, bool]]:
    """(id, reads images) for each listed model. A server whose list isn't the usual
    {"data": [...]} lists nothing."""
    data = getattr(page, "data", None)
    if not isinstance(data, list):
        return []
    return [
        (model_id, _reads_images(model))
        for model in data
        if isinstance(model_id := _field(model, "id"), str) and model_id
    ]


def _reads_images(model: Any) -> bool:
    modalities = _field(_field(model, "architecture"), "input_modalities")  # OpenRouter
    if isinstance(modalities, list) and "image" in modalities:
        return True
    return _field(_field(model, "capabilities"), "vision") is True  # Mistral


def _field(value: Any, name: str) -> Any:
    # The SDK keeps fields it doesn't know as plain dicts.
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
