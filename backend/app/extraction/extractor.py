"""Reads an invoice with the office's AI service and returns the structured extraction.

One request per document: the original PDF (or the rendered page images, or the document's
text) plus a short instruction, answered as an InvoiceExtraction. The service is the one
chosen in Settings (see app.extraction.services); this module drives the request and holds
the Claude adapter, the others live in app.extraction.adapters.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import ValidationError

from app.extraction import services as catalog
from app.extraction.base import (
    Adapter,
    Check,
    ExtractionError,
    ExtractionNotConfigured,
    Limits,
    Prepared,
    Reply,
    b64,
    image_media_type,
    not_configured_message,
    prepare,
    too_large,
)
from app.extraction.prompt import SYSTEM_PROMPT, build_instructions
from app.extraction.services import Service
from app.schemas.extraction import InvoiceExtraction
from app.services import app_settings

__all__ = [
    "ExtractionError",
    "ExtractionNotConfigured",
    "ExtractionOutcome",
    "build_adapter",
    "cost_usd",
    "extract_invoice",
]

MAX_TOKENS = 16000
RETRY_MAX_TOKENS = 32000
CLIENT_TIMEOUT_SECONDS = 180.0
# The larger retry produces up to twice the output, so it gets twice the time.
RETRY_TIMEOUT_SECONDS = 360.0
CHECK_TIMEOUT_SECONDS = 20

# A PDF this large would exceed the request size limit once base64-encoded.
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_FALLBACK_PAGES = 20
# Requests are limited to 32 MB; this leaves room for the prompt and the output schema.
MAX_IMAGE_PAYLOAD_BYTES = 30_000_000  # base64 characters across all page images

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output).
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
_CACHE_READ_FACTOR = Decimal("0.1")
_CACHE_WRITE_FACTOR = Decimal("1.25")
_MILLION = Decimal(1_000_000)
_COST_STEP = Decimal("0.000001")


@dataclass
class ExtractionOutcome:
    extraction: InvoiceExtraction
    model: str  # model that actually served the request (fallbacks may differ)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float


def extract_invoice(
    *,
    kind: str,
    file_path: Path,
    page_images: list[Path],
    text: str | None,
    company_name: str,
    company_gstin: str | None,
    client: Any = None,
) -> ExtractionOutcome:
    """client: an SDK client for the office's service, for tests; None builds one from the
    settings."""
    settings = app_settings.current()
    service = catalog.get(settings.ai_provider) or catalog.DEFAULT
    if client is None and settings.saved_unreadable:
        # The key may be saved only in Settings; "not configured" would hand the document to
        # manual entry for good.
        raise ExtractionError(
            "The office settings could not be read from the database, so this document was "
            f"not sent to {service.short}. Try again in a minute.",
            retryable=True,
        )
    adapter = build_adapter(service, settings, client=client)
    doc = prepare(kind, file_path, page_images, text, service=service, limits=adapter.limits())
    instructions = doc.instructions(build_instructions(company_name, company_gstin))

    usage = _Usage()
    for budget in adapter.budgets:
        reply = adapter.request(doc, instructions, budget=budget)
        usage.add(reply)
        if reply.stop != "cut_off":
            break
    else:
        raise ExtractionError(
            f"{service.short}'s answer for this document was cut off before it finished. "
            "Split the document into smaller files and upload them again, or enter it "
            "manually.",
            retryable=False,
        )

    if reply.stop == "refused":
        reason = f" (reason: {reply.refusal_reason})" if reply.refusal_reason else ""
        raise ExtractionError(
            f"{service.short} declined to read this document{reason}. Enter it manually.",
            retryable=False,
        )
    if reply.extraction is None:
        raise ExtractionError(
            f"{service.short} did not return any invoice details for this document. Enter it "
            "manually.",
            retryable=False,
        )
    return ExtractionOutcome(
        extraction=reply.extraction,
        model=reply.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cost_usd=float(usage.cost),
    )


def build_adapter(
    service: Service, settings: app_settings.Effective, *, client: Any = None
) -> Adapter:
    """The adapter for a service, with its effective key, model and endpoint. Raises
    ExtractionNotConfigured when the service needs a key and none is set."""
    state = settings.service_settings(service.id)
    key = (state.api_key or "").strip() or None
    if key is None and not service.key_optional and client is None:
        raise ExtractionNotConfigured(not_configured_message(service))
    if service.custom_base_url and not state.base_url and client is None:
        raise ExtractionNotConfigured(
            "Invoice reading is not set up because no address was entered for the AI "
            "service. An administrator can add it in Settings."
        )
    if not state.model and client is None:
        raise ExtractionNotConfigured(
            f"Invoice reading is not set up because no model was chosen for {service.short}. "
            "An administrator can choose one in Settings."
        )
    match service.adapter:
        case "anthropic":
            return AnthropicAdapter(service, key, state.model, settings.claude_effort, client)
        case "gemini":
            from app.extraction.adapters.gemini import GeminiAdapter

            return GeminiAdapter(service, key, state.model, client)
        case "openai":
            from app.extraction.adapters.openai_api import OpenAIAdapter

            return OpenAIAdapter(service, key, state.model, client)
        case "compatible":
            from app.extraction.adapters.compatible import CompatibleAdapter

            base_url = state.base_url or service.base_url
            return CompatibleAdapter(service, key, state.model, base_url, client)
    raise ExtractionNotConfigured(not_configured_message(service))


def waiting_for_ai(error: str | None) -> bool:
    """Whether an extraction error only means AI reading wasn't usable (no key, no model or
    address, a rejected key), so the document can be read once the settings change."""
    error = error or ""
    return error.startswith("Invoice reading is not set up") or (
        " was rejected. An administrator can check it in Settings" in error
    )


def key_rejected_message(service: Service) -> str:
    env = service.env_keys[0].upper() if service.env_keys else "the key"
    return (
        f"The {service.key_name} was rejected. An administrator can check it in Settings (or "
        f"{env} in backend/.env)."
    )


# -- Claude -------------------------------------------------------------------------------


class AnthropicAdapter:
    budgets = (MAX_TOKENS, RETRY_MAX_TOKENS)

    def __init__(
        self, service: Service, key: str | None, model: str, effort: str, client: Any = None
    ) -> None:
        self.service = service
        self.model = model
        self.effort = effort
        self._key = key
        self._client = client

    def limits(self) -> Limits:
        # Read at call time: tests (and future tuning) change the module values.
        return Limits(
            max_pdf_bytes=MAX_PDF_BYTES,
            pdf_label="20 MB",
            max_image_payload=MAX_IMAGE_PAYLOAD_BYTES,
            max_images=MAX_FALLBACK_PAGES,
        )

    def _sdk(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self._key, timeout=CLIENT_TIMEOUT_SECONDS, max_retries=2
            )
        return self._client

    def request(self, doc: Prepared, instructions: str, *, budget: int | None) -> Reply:
        content = [*_claude_blocks(doc), {"type": "text", "text": instructions}]
        message, extraction = _request(
            self._sdk(),
            model=self.model,
            effort=self.effort,
            content=content,
            max_tokens=budget or MAX_TOKENS,
            service=self.service,
        )
        stop = {"max_tokens": "cut_off", "refusal": "refused"}.get(message.stop_reason, "done")
        usage = message.usage
        input_tokens = usage.input_tokens or 0
        output_tokens = usage.output_tokens or 0
        cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
        category = getattr(getattr(message, "stop_details", None), "category", None)
        return Reply(
            extraction=extraction,
            stop=stop,
            model=message.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            # Priced at the serving model's rates; after a fallback this is an approximation.
            cost=_cost(message.model, input_tokens, output_tokens, cache_read, cache_write),
            refusal_reason=category,
        )

    def check(self) -> Check:
        """Looks the model up with the key: proves both work without spending any tokens."""
        client = anthropic.Anthropic(
            api_key=self._key, timeout=CHECK_TIMEOUT_SECONDS, max_retries=0
        )
        try:
            client.models.retrieve(self.model)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return Check(
                ok=False,
                detail="The key was rejected by Anthropic. Check that it was copied in full and "
                "has not been revoked, then save it again.",
            )
        except anthropic.NotFoundError:
            return Check(
                ok=False,
                detail=f"{self.model} is not available to this key. Choose another model, or "
                "use a key from a workspace that has access to it.",
            )
        except anthropic.APIConnectionError:  # includes timeouts
            return Check(
                ok=False,
                detail="Could not reach the Anthropic API. Check this computer's internet "
                "connection and try again.",
            )
        except anthropic.APIStatusError as exc:
            return Check(
                ok=False,
                detail=f"The Anthropic API returned an error (HTTP {exc.status_code}). Try "
                "again in a few minutes.",
            )
        finally:
            client.close()
        return Check(ok=True, detail=f"Connected. {self.model} is available.")

    def list_models(self) -> list[str]:
        client = anthropic.Anthropic(
            api_key=self._key, timeout=CHECK_TIMEOUT_SECONDS, max_retries=0
        )
        try:
            return [m.id for m in client.models.list(limit=100)]
        finally:
            client.close()


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimated cost of one Claude request; input_tokens excludes cached tokens, as the API
    reports it. Unknown models cost 0.0 rather than a wrong guess."""
    return float(_cost(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens))


def _cost(
    model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> Decimal:
    prices = _prices_for(model)
    if prices is None:
        return Decimal("0")
    input_price, output_price = prices
    total = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_read * input_price * _CACHE_READ_FACTOR
        + cache_write * input_price * _CACHE_WRITE_FACTOR
    ) / _MILLION
    return total.quantize(_COST_STEP, rounding=ROUND_HALF_UP)


def _prices_for(model: str) -> tuple[Decimal, Decimal] | None:
    if model in PRICING:
        return PRICING[model]
    # Dated or suffixed ids; longest prefix wins so "claude-opus-5-5-..." is not priced as
    # "claude-opus-5".
    matches = [name for name in PRICING if model.startswith(f"{name}-")]
    return PRICING[max(matches, key=len)] if matches else None


def _request(
    client: Any,
    *,
    model: str,
    effort: str,
    content: list[dict],
    max_tokens: int,
    service: Service,
) -> tuple[Any, InvoiceExtraction | None]:
    """One API call: the message (stop reason, model, usage) and its extraction, if any.

    The raw response is requested because the SDK validates the answer text against the
    schema whatever the stop reason, so a refusal or a cut-off answer raises ValidationError
    and would otherwise lose its stop reason and its billed usage.
    """
    options: dict[str, Any] = {}
    if max_tokens > MAX_TOKENS:
        options["timeout"] = RETRY_TIMEOUT_SECONDS
    try:
        raw = client.beta.messages.with_raw_response.parse(
            model=model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": content}],
            output_format=InvoiceExtraction,
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **options,
        )
    except anthropic.APIError as exc:
        raise _api_error(exc, service) from exc
    try:
        message = raw.parse()
    except ValidationError:
        return BetaMessage.construct(**raw.json()), None
    return message, message.parsed_output


def _claude_blocks(doc: Prepared) -> list[dict]:
    if doc.pdf is not None:
        return [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64(doc.pdf),
                },
            }
        ]
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type(p), "data": b64(p)},
        }
        for p in doc.images
    ]


def _api_error(exc: anthropic.APIError, service: Service) -> ExtractionError:
    # Order matters: APITimeoutError is an APIConnectionError, and every HTTP error is an
    # APIStatusError.
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ExtractionError(key_rejected_message(service), retryable=False)
    if isinstance(exc, anthropic.RequestTooLargeError):
        return ExtractionError(too_large(service), retryable=False)
    if isinstance(exc, anthropic.BadRequestError | anthropic.NotFoundError):
        return ExtractionError(
            f"Claude could not process this document: {_api_message(exc)}", retryable=False
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ExtractionError(
            "Claude's usage limit has been reached for now. Try again in a minute.",
            retryable=True,
        )
    if isinstance(exc, anthropic.APITimeoutError):
        return ExtractionError("Claude took too long to answer. Try again.", retryable=True)
    if isinstance(exc, anthropic.APIConnectionError):
        return ExtractionError(
            "Could not reach the Claude API. Check the internet connection and try again.",
            retryable=True,
        )
    if isinstance(exc, anthropic.OverloadedError | anthropic.InternalServerError):
        return ExtractionError(
            "Claude is temporarily unavailable. Try again in a few minutes.", retryable=True
        )
    if isinstance(exc, anthropic.APIStatusError):
        retryable = exc.status_code in (408, 409) or exc.status_code >= 500
        return ExtractionError(
            f"The Claude API returned an error: {_api_message(exc)}", retryable=retryable
        )
    return ExtractionError(f"The Claude API returned an error: {exc}", retryable=False)


def _api_message(exc: anthropic.APIError) -> str:
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return exc.message


class _Usage:
    """Totals across attempts: a cut-off first attempt is still billed."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cost = Decimal("0")

    def add(self, reply: Reply) -> None:
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.cache_read_tokens += reply.cache_read_tokens
        self.cost += reply.cost
