"""OpenAI (GPT) adapter: Chat Completions with structured output.

The PDF goes as a file content part (or the pages as image data URLs) followed by the
instructions; the SDK's parse() turns the answer into an InvoiceExtraction. Mirrors the
Claude adapter in app.extraction.extractor: same budgets idea, error mapping and checks.
The budgets stay within each model's answer limit (gpt-4o writes at most 16,384 tokens and
refuses a request for more); a model with a limit this module doesn't know is asked once
more within the limit its refusal names.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import openai
from pydantic import ValidationError

from app.extraction.base import (
    Check,
    ExtractionError,
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

OPENAI_API_URL = "https://api.openai.com/v1"
# max_completion_tokens includes a reasoning model's hidden reasoning, so these are well
# above what the answer itself needs.
MAX_COMPLETION_TOKENS = 32000
RETRY_MAX_COMPLETION_TOKENS = 64000
# The most some models write in one answer, where it is below RETRY_MAX_COMPLETION_TOKENS:
# a request asking for more is refused outright. The longest matching name wins; other
# models (gpt-5 and later, the o-series) allow 100,000 or more.
OUTPUT_CAPS: dict[str, int] = {
    "gpt-4o": 16384,
    "gpt-4o-mini": 16384,
    "gpt-4o-2024-05-13": 4096,
    "chatgpt-4o": 16384,
    "gpt-4.1": 32768,
}
_CHAT_LATEST = re.compile(r"-chat(-latest)?$")  # gpt-5-chat-latest, gpt-5.1-chat-latest
CHAT_LATEST_CAP = 16384
# "max_tokens is too large: 32000. This model supports at most 16384 completion tokens"
_AT_MOST = re.compile(r"at most (\d[\d,]*)")
CLIENT_TIMEOUT_SECONDS = 180.0
# The larger retry produces up to twice the output, so it gets twice the time.
RETRY_TIMEOUT_SECONDS = 360.0
CHECK_TIMEOUT_SECONDS = 20.0

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_FALLBACK_PAGES = 20
MAX_IMAGE_PAYLOAD_BYTES = 30_000_000  # base64 characters across all page images

# USD per million tokens: (input, cached input, output). The cache discount differs by
# model family, so cached input has its own price.
PRICING: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "gpt-5.1": (Decimal("1.25"), Decimal("0.125"), Decimal("10")),
    "gpt-5": (Decimal("1.25"), Decimal("0.125"), Decimal("10")),
    "gpt-5-mini": (Decimal("0.25"), Decimal("0.025"), Decimal("2")),
    "gpt-5-nano": (Decimal("0.05"), Decimal("0.005"), Decimal("0.40")),
    "gpt-5-pro": (Decimal("15"), Decimal("15"), Decimal("120")),
    "gpt-4.1": (Decimal("2"), Decimal("0.50"), Decimal("8")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("0.10"), Decimal("1.60")),
    "gpt-4.1-nano": (Decimal("0.10"), Decimal("0.025"), Decimal("0.40")),
    "gpt-4o": (Decimal("2.50"), Decimal("1.25"), Decimal("10")),
    "gpt-4o-2024-05-13": (Decimal("5"), Decimal("5"), Decimal("15")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.075"), Decimal("0.60")),
    "chatgpt-4o-latest": (Decimal("5"), Decimal("5"), Decimal("15")),
    "o1": (Decimal("15"), Decimal("7.50"), Decimal("60")),
    "o1-mini": (Decimal("1.10"), Decimal("0.55"), Decimal("4.40")),
    "o1-pro": (Decimal("150"), Decimal("150"), Decimal("600")),
    "o3": (Decimal("2"), Decimal("0.50"), Decimal("8")),
    "o3-mini": (Decimal("1.10"), Decimal("0.55"), Decimal("4.40")),
    "o3-pro": (Decimal("20"), Decimal("20"), Decimal("80")),
    "o4-mini": (Decimal("1.10"), Decimal("0.275"), Decimal("4.40")),
}
_MILLION = Decimal(1_000_000)
_COST_STEP = Decimal("0.000001")

# Models that take text, images and PDFs in Chat Completions.
_DOCUMENT_MODEL = re.compile(r"(gpt-|chatgpt-|o1|o3|o4)")
_NOT_FOR_DOCUMENTS = re.compile(
    r"audio|realtime|tts|transcribe|image|embedding|search|moderation|instruct|codex"
    r"|gpt-3\.5|gpt-4(-|$)"  # no structured output, and gpt-3.5 sees no images
    r"|o1-mini|o1-preview|o3-mini"  # text only
    r"|-pro(-|$)|deep-research"  # Responses API only
)
_SNAPSHOT = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")  # gpt-4o-2024-08-06, gpt-4-0613
_UNSUPPORTED = ("not support", "unsupported", "only supported", "invalid content")
_INPUTS = ("file", "pdf", "image", "response_format", "json_schema")


class OpenAIAdapter:
    def __init__(self, service: Service, key: str | None, model: str, client: Any = None) -> None:
        self.service = service
        self.model = model
        self.budgets = budgets_for(model)
        self._key = key
        self._client = client
        # Learned from a refusal: the most this model writes, and the largest budget whose
        # answer was cut off. Asking again within both would only repeat the cut-off answer.
        self._cap: int | None = None
        self._cut_off_at: int | None = None

    def limits(self) -> Limits:
        # Read at call time, like the Claude adapter: tests change the module values.
        return Limits(
            max_pdf_bytes=MAX_PDF_BYTES,
            pdf_label="20 MB",
            max_image_payload=MAX_IMAGE_PAYLOAD_BYTES,
            max_images=MAX_FALLBACK_PAGES,
        )

    def _base_url(self) -> str:
        # Fixed, so an OPENAI_BASE_URL left in the environment can't send the key elsewhere.
        return self.service.base_url or OPENAI_API_URL

    def _sdk(self) -> Any:
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self._key,
                base_url=self._base_url(),
                timeout=CLIENT_TIMEOUT_SECONDS,
                max_retries=2,
            )
        return self._client

    @contextmanager
    def _quick_client(self) -> Iterator[Any]:
        """A client for Settings, where someone is waiting: short timeout and no retries."""
        if self._client is not None:
            # Shares the existing connection pool, so it is not closed here.
            yield self._client.with_options(timeout=CHECK_TIMEOUT_SECONDS, max_retries=0)
            return
        client = openai.OpenAI(
            api_key=self._key,
            base_url=self._base_url(),
            timeout=CHECK_TIMEOUT_SECONDS,
            max_retries=0,
        )
        try:
            yield client
        finally:
            client.close()

    def request(self, doc: Prepared, instructions: str, *, budget: int | None) -> Reply:
        budget = budget or self.budgets[0]
        if self._cap is not None:
            budget = min(budget, self._cap)
        if self._cut_off_at is not None and budget <= self._cut_off_at:
            return Reply(extraction=None, stop="cut_off", model=self.model)
        content = [*_content_parts(doc), {"type": "text", "text": instructions}]
        while True:
            try:
                reply = self._ask(content, budget)
                break
            except openai.BadRequestError as exc:
                # A model with a smaller answer limit than budgets_for() knows: asked once
                # more within the limit it names.
                allowed = None if self._cap is not None else _allowed_budget(exc, budget)
                if allowed is None:
                    raise _api_error(exc, self.service, self.model, self._key) from exc
                self._cap = budget = allowed
            except openai.APIError as exc:
                raise _api_error(exc, self.service, self.model, self._key) from exc
        if reply.stop == "cut_off":
            self._cut_off_at = max(budget, self._cut_off_at or 0)
        return reply

    def _ask(self, content: list[dict], budget: int) -> Reply:
        """One request; API errors are left to request()."""
        options: dict[str, Any] = {}
        if budget > MAX_COMPLETION_TOKENS:
            options["timeout"] = RETRY_TIMEOUT_SECONDS
        try:
            completion = self._sdk().chat.completions.parse(
                model=self.model,
                messages=[
                    # First and identical every time, so OpenAI's prompt cache can reuse it.
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                response_format=InvoiceExtraction,
                max_completion_tokens=budget,
                store=False,  # invoices are not kept for OpenAI's dashboard or evals
                **options,
            )
        except openai.LengthFinishReasonError as exc:
            # Still billed: the cut-off answer's usage counts towards the document's cost.
            return self._reply(getattr(exc, "completion", None), stop="cut_off")
        except openai.ContentFilterFinishReasonError as exc:
            completion = getattr(exc, "completion", None)
            return self._reply(completion, stop="refused", refusal_reason="content filter")
        except ValidationError:
            # Structured output makes this all but impossible; the usage is lost with it.
            return Reply(extraction=None, stop="done", model=self.model)

        choices = getattr(completion, "choices", None) or []
        message = choices[0].message if choices else None
        refusal = getattr(message, "refusal", None)
        if refusal:
            return self._reply(completion, stop="refused", refusal_reason=_reason(refusal))
        return self._reply(completion, stop="done", extraction=getattr(message, "parsed", None))

    def _reply(
        self,
        completion: Any,
        *,
        stop: Stop,
        extraction: InvoiceExtraction | None = None,
        refusal_reason: str | None = None,
    ) -> Reply:
        usage = getattr(completion, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) or 0  # includes cached tokens
        output_tokens = getattr(usage, "completion_tokens", None) or 0  # includes reasoning
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) or 0
        model = getattr(completion, "model", None) or self.model
        return Reply(
            extraction=extraction,
            stop=stop,
            model=model,
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            cost=cost(model, prompt_tokens, output_tokens, cached),
            refusal_reason=refusal_reason,
        )

    def check(self) -> Check:
        """Looks the model up with the key: proves both work without spending any tokens."""
        try:
            with self._quick_client() as client:
                client.models.retrieve(self.model)
        except openai.AuthenticationError:
            return Check(
                ok=False,
                detail="The key was rejected by OpenAI. Check that it was copied in full and has "
                "not been revoked, then save it again.",
            )
        except openai.PermissionDeniedError:
            return Check(
                ok=False,
                detail="The key was rejected by OpenAI. If it is a restricted key, allow it to "
                "read models and use them (or create a key with All permissions), then save it "
                "again.",
            )
        except openai.NotFoundError:
            return Check(
                ok=False,
                detail=f"{self.model} is not available to this key. Choose another model, or "
                "use a key from a project that has access to it.",
            )
        except openai.APIConnectionError:  # includes timeouts
            return Check(
                ok=False,
                detail="Could not reach the OpenAI API. Check this computer's internet "
                "connection and try again.",
            )
        except openai.APIStatusError as exc:
            return Check(
                ok=False,
                detail=f"The OpenAI API returned an error (HTTP {exc.status_code}). Try again "
                "in a few minutes.",
            )
        except openai.APIError:
            return Check(
                ok=False,
                detail="The OpenAI API gave an answer that could not be read. Try again in a "
                "few minutes.",
            )
        return Check(ok=True, detail=f"Connected. {self.model} is available.")

    def list_models(self) -> list[str]:
        """Chat models that read PDFs and images: larger before mini and nano, newer before
        older, named models before their dated snapshots."""
        with self._quick_client() as client:
            models = [
                m
                for m in client.models.list()
                if _DOCUMENT_MODEL.match(m.id) and not _NOT_FOR_DOCUMENTS.search(m.id)
            ]
        models.sort(
            key=lambda m: (
                bool(_SNAPSHOT.search(m.id)),
                _size(m.id),
                -(getattr(m, "created", 0) or 0),
                m.id,
            )
        )
        return [m.id for m in models]


def budgets_for(model: str) -> tuple[int, ...]:
    """Answer budgets for a model: the usual pair, or the model's whole limit at once when it
    is below the retry budget, since a retry could then add little or nothing."""
    cap = output_cap(model)
    if cap is None or cap >= RETRY_MAX_COMPLETION_TOKENS:
        return (MAX_COMPLETION_TOKENS, RETRY_MAX_COMPLETION_TOKENS)
    return (cap,)


def output_cap(model: str) -> int | None:
    """The most this model writes in one answer, where it is known to be below the retry
    budget; None otherwise."""
    if _CHAT_LATEST.search(model):
        return CHAT_LATEST_CAP
    if model in OUTPUT_CAPS:
        return OUTPUT_CAPS[model]
    matches = [name for name in OUTPUT_CAPS if model.startswith(f"{name}-")]
    return OUTPUT_CAPS[max(matches, key=len)] if matches else None


def cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> Decimal:
    """Estimated cost of one request in USD. input_tokens includes cached_tokens, as OpenAI
    reports them. Unknown models cost 0 rather than a wrong guess."""
    prices = _prices_for(model)
    if prices is None:
        return Decimal("0")
    input_price, cached_price, output_price = prices
    cached = min(cached_tokens, input_tokens)
    total = (
        (input_tokens - cached) * input_price + cached * cached_price + output_tokens * output_price
    ) / _MILLION
    return total.quantize(_COST_STEP, rounding=ROUND_HALF_UP)


def _prices_for(model: str) -> tuple[Decimal, Decimal, Decimal] | None:
    if model in PRICING:
        return PRICING[model]
    # Dated or suffixed ids; longest prefix wins so "gpt-5-mini-..." is not priced as "gpt-5".
    matches = [name for name in PRICING if model.startswith(f"{name}-")]
    return PRICING[max(matches, key=len)] if matches else None


def _size(model: str) -> int:
    if "nano" in model:
        return 2
    return 1 if "mini" in model else 0


def _content_parts(doc: Prepared) -> list[dict]:
    if doc.pdf is not None:
        return [
            {
                "type": "file",
                "file": {
                    "filename": doc.pdf.name,
                    "file_data": f"data:application/pdf;base64,{b64(doc.pdf)}",
                },
            }
        ]
    return [
        {
            "type": "image_url",
            # High detail: invoice figures and GSTINs are small print.
            "image_url": {"url": f"data:{image_media_type(p)};base64,{b64(p)}", "detail": "high"},
        }
        for p in doc.images
    ]


def _reason(refusal: str) -> str:
    """The model's refusal, short enough to sit in a message for people."""
    text = " ".join(refusal.split()).rstrip(".")
    return text if len(text) <= 200 else f"{text[:197].rstrip()}..."


def _api_error(
    exc: openai.APIError, service: Service, model: str, key: str | None
) -> ExtractionError:
    # Order matters: APITimeoutError is an APIConnectionError, and every HTTP error is an
    # APIStatusError.
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ExtractionError(key_rejected_message(service), retryable=False, fix_in_settings=True)
    if isinstance(exc, openai.RateLimitError):
        if "insufficient_quota" in (exc.code, exc.type):
            return ExtractionError(
                "OpenAI did not read this document because the account for this key has no "
                "credit left. An administrator can add credit at platform.openai.com, under "
                "Billing, and then try again.",
                retryable=False,
                fix_in_settings=True,
            )
        return ExtractionError(
            "OpenAI's usage limit has been reached for now. Try again in a minute.",
            retryable=True,
        )
    if isinstance(exc, openai.NotFoundError):
        return ExtractionError(
            f"The OpenAI model {model} is not available to this key. An administrator can "
            "choose another model in Settings.",
            retryable=False,
            fix_in_settings=True,
        )
    if isinstance(exc, openai.APIStatusError) and (
        exc.status_code == 413 or exc.code == "context_length_exceeded"
    ):
        return ExtractionError(too_large(service), retryable=False)
    if isinstance(exc, openai.BadRequestError):
        message = _api_message(exc, key)
        if _unsupported_input(message):
            return ExtractionError(
                f"The OpenAI model {model} can't read PDF files or scans. An administrator "
                f"can choose a model that reads PDFs, such as {service.default_model or 'gpt-5'}"
                ", in Settings.",
                retryable=False,
                fix_in_settings=True,
            )
        return ExtractionError(
            f"OpenAI could not process this document: {message}", retryable=False
        )
    if isinstance(exc, openai.APITimeoutError):
        return ExtractionError("OpenAI took too long to answer. Try again.", retryable=True)
    if isinstance(exc, openai.APIConnectionError):
        return ExtractionError(
            "Could not reach the OpenAI API. Check the internet connection and try again.",
            retryable=True,
        )
    if isinstance(exc, openai.InternalServerError):
        return ExtractionError(
            "OpenAI is temporarily unavailable. Try again in a few minutes.", retryable=True
        )
    if isinstance(exc, openai.APIStatusError):
        retryable = exc.status_code in (408, 409) or exc.status_code >= 500
        return ExtractionError(
            f"The OpenAI API returned an error: {_api_message(exc, key)}", retryable=retryable
        )
    return ExtractionError(
        f"The OpenAI API returned an error: {_scrub(exc.message, key)}", retryable=False
    )


def _allowed_budget(exc: openai.BadRequestError, budget: int) -> int | None:
    """The answer limit a refusal names, when it is about the budget and below what was
    asked: "max_tokens is too large: 32000. This model supports at most 16384 completion
    tokens"."""
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    param = error.get("param") if isinstance(error, dict) else None
    message = _api_message(exc, None)
    if not (
        param in ("max_tokens", "max_completion_tokens")
        or "max_tokens" in message
        or "max_completion_tokens" in message
    ):
        return None
    limit = _AT_MOST.search(message)
    allowed = int(limit.group(1).replace(",", "")) if limit else 0
    return allowed if 1024 <= allowed < budget else None


def _unsupported_input(message: str) -> bool:
    """Whether a 400 says the model can't take a PDF, an image or structured output (and
    not, say, that one image is broken)."""
    text = message.lower()
    if not any(word in text for word in _INPUTS):
        return False
    if "supported values" in text:  # "Invalid value: 'file'. Supported values are: ..."
        return True
    return "model" in text and any(word in text for word in _UNSUPPORTED)


def _api_message(exc: openai.APIError, key: str | None) -> str:
    # The SDK keeps the "error" object of the response body as exc.body.
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return _scrub(error["message"], key)
    status = getattr(exc, "status_code", None)
    return f"HTTP {status}" if status else "no details were given"


def _scrub(text: str, key: str | None) -> str:
    """The key never reaches a message, even if the service echoes it back."""
    return text.replace(key, "[key]") if key else text
