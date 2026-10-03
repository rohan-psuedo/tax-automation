"""Gemini (Google AI Studio key) through the google-genai SDK.

One generateContent call per attempt: the PDF or page images inline, then the instructions,
with the answer constrained to JSON matching InvoiceExtraction.

Choices worth knowing:
- The schema goes as response_json_schema, self-contained: pydantic writes a field's
  description next to its "$ref", which Gemini does not allow (a "$ref" may have only "$"
  keywords beside it), so the $defs are inlined; "title" and "default" are left out
  ("default" is not a supported keyword). additionalProperties is supported there and kept.
- Temperature and thinking are left to the model: Google advises against lowering the
  temperature of Gemini 3 models (they can loop), the schema already constrains the
  answer, and thinking settings differ by model generation (2.5 Pro can't turn it off).
- When Gemini is busy (500, 502, 503, 504) or a per-minute limit is reached (429),
  request() asks again after a pause, waiting as long as Gemini asks to, up to ATTEMPTS in
  all. The SDK's own retries stay off because they would also repeat a timed-out request,
  which already took the whole deadline. Only a quick failure is repeated, so one request
  stays well under the worker's 10 minutes; what still fails is retried by the worker later.
  The Settings check and model list are never repeated: someone is waiting for them.
- A 429 that no wait can fix (the key's plan has no quota for the model, or its daily quota
  is used up) is not retried: it is fixed in Settings (another model, or billing).
- The address is fixed, so a GOOGLE_GEMINI_BASE_URL left in the environment can't send the
  key and the invoices elsewhere.
"""

import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
import httpx2
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from app.extraction.base import (
    Check,
    ExtractionError,
    ExtractionNotConfigured,
    Limits,
    Prepared,
    Reply,
    image_media_type,
    not_configured_message,
    read_bytes,
    too_large,
)
from app.extraction.prompt import SYSTEM_PROMPT
from app.extraction.services import Service
from app.schemas.extraction import InvoiceExtraction

log = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/"

# max_output_tokens: thinking counts toward it on Gemini 2.5 and later, so the first
# attempt leaves room for both; a cut-off answer is asked again with the 2.5 models' maximum.
BUDGETS = (32768, 65536)
# HttpOptions.timeout is in milliseconds.
TIMEOUT_MS = 180_000
RETRY_TIMEOUT_MS = 360_000  # the larger retry can produce twice the output
CHECK_TIMEOUT_MS = 20_000

# Asking again when Gemini is busy: attempts in all, the first pause (doubled each time, or
# the pause Gemini asks for, up to MAX_PAUSE), and no new attempt once REPEAT_WITHIN would
# be passed. Worst case: REPEAT_WITHIN plus one full timeout.
ATTEMPTS = 3
FIRST_PAUSE = 2.0  # seconds
MAX_PAUSE = 60.0  # a per-minute limit is over within a minute
REPEAT_WITHIN = 90.0  # seconds since the first attempt
_BUSY = {429, 500, 502, 503, 504}

# Inline data is limited to about 20 MB per request, and base64 adds a third: 14 MiB of PDF
# is about 19.6 million characters, leaving room for the prompt and schema.
MAX_PDF_BYTES = 14 * 1024 * 1024
PDF_LABEL = "14 MB"
MAX_IMAGE_PAYLOAD = 18_000_000  # base64 characters across all page images
MAX_IMAGES = 20

# USD per million tokens: (input, output incl. thinking, cached input), from
# ai.google.dev/gemini-api/docs/pricing (paid tier, text/image/PDF input).
PRICING: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "gemini-3.1-pro-preview": (Decimal("2"), Decimal("12"), Decimal("0.20")),
    "gemini-3-pro-preview": (Decimal("2"), Decimal("12"), Decimal("0.20")),
    "gemini-3-flash-preview": (Decimal("0.50"), Decimal("3"), Decimal("0.05")),
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("10"), Decimal("0.125")),
    "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50"), Decimal("0.03")),
    "gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40"), Decimal("0.01")),
}
# Pro models charge more for the whole request once the prompt exceeds 200k tokens.
LONG_CONTEXT_TOKENS = 200_000
LONG_CONTEXT_PRICING: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "gemini-3.1-pro-preview": (Decimal("4"), Decimal("18"), Decimal("0.40")),
    "gemini-3-pro-preview": (Decimal("4"), Decimal("18"), Decimal("0.40")),
    "gemini-2.5-pro": (Decimal("2.50"), Decimal("15"), Decimal("0.25")),
}
_MILLION = Decimal(1_000_000)
_COST_STEP = Decimal("0.000001")

_CUT_OFF = {"MAX_TOKENS", "CONTINUATION"}
# Why Gemini stopped or blocked, in a few words for the "declined" message.
_REFUSALS = {
    "SAFETY": "safety filter",
    "PROHIBITED_CONTENT": "prohibited content",
    "BLOCKLIST": "blocked terms",
    "SPII": "personal information",
    "RECITATION": "resembles published material",
    "LANGUAGE": "unsupported language",
    "IMAGE_SAFETY": "image safety filter",
    "IMAGE_PROHIBITED_CONTENT": "prohibited image content",
    "IMAGE_RECITATION": "resembles published material",
    "IMAGE_OTHER": "image blocked",
}
_BLOCKS = {
    **_REFUSALS,
    "OTHER": "blocked by Google",
    "MODEL_ARMOR": "blocked by a Model Armor policy",
    "JAILBREAK": "instructions hidden in the document",
}

# Default client: httpx. httpx2 too, since the SDK accepts either as its HTTP client.
_TIMEOUTS = (httpx.TimeoutException, httpx2.TimeoutException)
_TRANSPORT = (httpx.TransportError, httpx2.TransportError)

_FENCE = re.compile(r"^```[A-Za-z]*\s*(.*?)\s*```$", re.DOTALL)
# Gemini models that don't answer with text, or are made for another job.
_NOT_FOR_READING = ("tts", "image", "live", "audio", "embedding", "computer-use", "robotics")
# "Quota exceeded for metric: ...free_tier_requests, limit: 0, model: gemini-2.5-pro"
_ZERO_LIMIT = re.compile(r"\blimit:\s*0(?![\d.])")
_SECONDS = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")  # RetryInfo.retryDelay, e.g. "39s"

# Replaced in tests, so no test waits.
_sleep = time.sleep
_clock = time.monotonic


def _self_contained(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with every $ref replaced by its definition (keeping the field's own
    description) and without "title" or "default"."""
    defs = schema.get("$defs", {})

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$defs", "title", "default"):
                continue
            if key == "properties":
                out[key] = {name: resolve(sub) for name, sub in value.items()}
            elif key in ("items", "additionalProperties") and isinstance(value, dict):
                out[key] = resolve(value)
            elif key in ("anyOf", "oneOf", "prefixItems"):
                out[key] = [resolve(sub) for sub in value]
            else:
                out[key] = value
        return out

    return resolve(schema)


RESPONSE_SCHEMA = _self_contained(InvoiceExtraction.model_json_schema())


class GeminiAdapter:
    budgets = BUDGETS

    def __init__(self, service: Service, key: str | None, model: str, client: Any = None) -> None:
        self.service = service
        self.model = model
        self._key = key
        self._client = client

    def limits(self) -> Limits:
        # Read at call time, so tests (and future tuning) can change the module values.
        return Limits(
            max_pdf_bytes=MAX_PDF_BYTES,
            pdf_label=PDF_LABEL,
            max_image_payload=MAX_IMAGE_PAYLOAD,
            max_images=MAX_IMAGES,
        )

    def request(self, doc: Prepared, instructions: str, *, budget: int | None) -> Reply:
        parts = [*_document_parts(doc), types.Part.from_text(text=instructions)]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
            max_output_tokens=budget,
            # No tools are offered; this also skips the SDK's function-calling loop.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=_timeout(
                TIMEOUT_MS if budget is not None and budget <= BUDGETS[0] else RETRY_TIMEOUT_MS
            ),
        )
        started, attempt = _clock(), 1
        while True:
            try:
                response = self._sdk().models.generate_content(
                    model=self.model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config,
                )
                break
            except (errors.APIError, errors.UnknownApiResponseError, *_TRANSPORT) as exc:
                pause = _pause(exc, attempt)
                if (
                    pause is None
                    or attempt >= ATTEMPTS
                    or _clock() - started + pause > REPEAT_WITHIN
                ):
                    raise self._error(exc) from exc
                log.info(
                    "Gemini is busy (%s); asking again in %.0f seconds",
                    getattr(exc, "code", None) or type(exc).__name__,
                    pause,
                )
                _sleep(pause)
                attempt += 1
        return self._reply(response)

    def check(self) -> Check:
        """Looks the model up with the key: proves both work without spending any tokens."""
        owned = self._client is None
        client = self._new_client(CHECK_TIMEOUT_MS) if owned else self._client
        try:
            found = client.models.get(model=self.model)
        except errors.APIError as exc:
            return Check(ok=False, detail=self._check_failure(exc))
        except _TRANSPORT:  # includes timeouts
            return Check(
                ok=False,
                detail="Could not reach the Gemini API. Check this computer's internet "
                "connection and try again.",
            )
        except errors.UnknownApiResponseError:
            return Check(
                ok=False,
                detail="The Gemini API sent an answer that could not be read. Try again in a "
                "few minutes.",
            )
        finally:
            if owned:
                client.close()
        actions = found.supported_actions or []
        if actions and "generateContent" not in actions:
            return Check(
                ok=False, detail=f"{self.model} can't read documents. Choose another model."
            )
        return Check(ok=True, detail=f"Connected. {self.model} is available.")

    def list_models(self) -> list[str]:
        owned = self._client is None
        client = self._new_client(CHECK_TIMEOUT_MS) if owned else self._client
        try:
            names = [
                _bare(m.name)
                for m in client.models.list(config={"page_size": 1000})
                if "generateContent" in (m.supported_actions or [])
            ]
        finally:
            if owned:
                client.close()
        usable = {
            n
            for n in names
            if n.startswith("gemini-") and not any(word in n for word in _NOT_FOR_READING)
        }
        return sorted(usable, key=_rank)

    # -- internals ------------------------------------------------------------------------

    def _sdk(self) -> Any:
        if self._client is None:
            self._client = self._new_client(TIMEOUT_MS)
        return self._client

    def _new_client(self, timeout_ms: int) -> genai.Client:
        if not self._key:
            raise ExtractionNotConfigured(not_configured_message(self.service))
        return genai.Client(
            api_key=self._key,
            # Explicit, so GOOGLE_GENAI_USE_VERTEXAI or GOOGLE_GEMINI_BASE_URL in the
            # environment can't send the key and the documents elsewhere.
            vertexai=False,
            http_options=types.HttpOptions(
                base_url=GEMINI_API_URL,
                timeout=timeout_ms,
                # request() asks again itself; see the module docstring.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def _reply(self, response: types.GenerateContentResponse) -> Reply:
        model = _bare(response.model_version) or self.model
        usage = response.usage_metadata or types.GenerateContentResponseUsageMetadata()
        reply = Reply(
            extraction=None,
            stop="done",
            model=model,
            # Includes the cached tokens, as Gemini reports it (Claude's excludes them).
            input_tokens=usage.prompt_token_count or 0,
            # Thinking is billed as output.
            output_tokens=(usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            cache_read_tokens=usage.cached_content_token_count or 0,
        )
        reply.cost = _cost(model, reply.input_tokens, reply.output_tokens, reply.cache_read_tokens)

        candidate = response.candidates[0] if response.candidates else None
        if candidate is None:  # the prompt itself was blocked, or nothing came back
            feedback = response.prompt_feedback
            block = _name(feedback.block_reason) if feedback else None
            if block and block != "BLOCKED_REASON_UNSPECIFIED":
                reply.stop = "refused"
                reply.refusal_reason = _BLOCKS.get(block, block.lower().replace("_", " "))
            return reply
        finish = _name(candidate.finish_reason)
        if finish in _CUT_OFF:
            reply.stop = "cut_off"
        elif finish in _REFUSALS:
            reply.stop = "refused"
            reply.refusal_reason = _REFUSALS[finish]
        else:
            reply.extraction = _parse(_answer_text(candidate))
        return reply

    def _error(self, exc: Exception) -> ExtractionError:
        if isinstance(exc, _TIMEOUTS):
            return ExtractionError("Gemini took too long to answer. Try again.", retryable=True)
        if isinstance(exc, _TRANSPORT):
            return ExtractionError(
                "Could not reach the Gemini API. Check the internet connection and try again.",
                retryable=True,
            )
        if not isinstance(exc, errors.APIError):  # UnknownApiResponseError: not JSON
            return ExtractionError(
                "The Gemini API sent an answer that could not be read. Try again in a few minutes.",
                retryable=True,
            )
        code = exc.code or 0
        message = self._message(exc)
        if _key_rejected(exc):
            # Imported here: the extractor imports this module.
            from app.extraction.extractor import key_rejected_message

            return ExtractionError(
                key_rejected_message(self.service), retryable=False, fix_in_settings=True
            )
        if code == 413 or (code == 400 and "payload size" in message.lower()):
            return ExtractionError(too_large(self.service), retryable=False)
        if code == 404:
            return ExtractionError(
                f"Gemini could not read this document because the model {self.model} is not "
                "available to this API key. An administrator can choose another model in "
                "Settings.",
                retryable=False,
                fix_in_settings=True,
            )
        if code == 429 and (used_up := _quota_used_up(exc)):
            return ExtractionError(
                f"Gemini did not read this document because {_no_quota(used_up, self.model)}. "
                f"An administrator can choose {_instead(self.model)} in Settings, or enable "
                "billing for the key in Google AI Studio.",
                retryable=False,
                fix_in_settings=True,
            )
        if code == 429:
            return ExtractionError(
                "Gemini's usage limit for this key has been reached for now. Try again in a "
                "minute.",
                retryable=True,
            )
        if code == 400:
            return ExtractionError(
                f"Gemini could not process this document: {message}", retryable=False
            )
        if code == 504:
            return ExtractionError("Gemini took too long to answer. Try again.", retryable=True)
        if code >= 500:
            return ExtractionError(
                "Gemini is temporarily unavailable. Try again in a few minutes.", retryable=True
            )
        return ExtractionError(
            f"The Gemini API returned an error: {message}", retryable=code in (408, 409)
        )

    def _check_failure(self, exc: errors.APIError) -> str:
        code = exc.code or 0
        if _key_rejected(exc):
            return (
                "The key was rejected by Google. Check that it was copied in full and has not "
                "been revoked, then save it again."
            )
        if code == 404:
            return (
                f"{self.model} is not available to this key. Choose another model, or check "
                "the model name."
            )
        if code == 429 and (used_up := _quota_used_up(exc)):
            no_quota = _no_quota(used_up, self.model)
            return (
                f"{no_quota[0].upper()}{no_quota[1:]}. Choose {_instead(self.model)}, or "
                "enable billing for the key in Google AI Studio."
            )
        if code == 429:
            return "This key's Gemini usage limit has been reached for now. Try again later."
        if 400 <= code < 500 and exc.message:
            return f"The Gemini API returned an error (HTTP {code}): {self._message(exc)}"
        return f"The Gemini API returned an error (HTTP {code}). Try again in a few minutes."

    def _message(self, exc: errors.APIError) -> str:
        message = exc.message or exc.status or f"HTTP {exc.code}"
        if self._key:
            message = message.replace(self._key, "[API key]")  # never shown or logged
        return message


def _timeout(ms: int) -> types.HttpOptions:
    """One request's timeout, with the server-side deadline to match. The SDK fills in
    X-Server-Timeout only when it is missing, and a call without its own options writes it
    into the client's shared headers, so the first deadline would also cut the longer retry
    short."""
    return types.HttpOptions(timeout=ms, headers={"X-Server-Timeout": str(-(-ms // 1000))})


def _document_parts(doc: Prepared) -> list[types.Part]:
    if doc.pdf is not None:
        return [types.Part.from_bytes(data=read_bytes(doc.pdf), mime_type="application/pdf")]
    return [
        types.Part.from_bytes(data=read_bytes(p), mime_type=image_media_type(p)) for p in doc.images
    ]


def _answer_text(candidate: types.Candidate) -> str:
    parts = candidate.content.parts if candidate.content else None
    return "".join(p.text for p in parts or [] if isinstance(p.text, str) and not p.thought)


def _parse(text: str) -> InvoiceExtraction | None:
    """The answer as an InvoiceExtraction; None when it isn't one. JSON mode should never
    wrap the answer in a code fence, but one costs nothing to strip."""
    text = text.strip()
    if fenced := _FENCE.match(text):
        text = fenced.group(1)
    if not text:
        return None
    try:
        return InvoiceExtraction.model_validate_json(text)
    except ValidationError:
        return None


def _pause(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before asking again after this failure; None when asking again
    soon can't help (the request was refused, timed out, or no wait fixes a quota)."""
    if isinstance(exc, errors.APIError):
        if exc.code not in _BUSY or (exc.code == 429 and _quota_used_up(exc)):
            return None
        asked = _retry_delay(exc)
        if asked is not None:
            return min(asked, MAX_PAUSE)
    elif isinstance(exc, _TIMEOUTS) or not isinstance(exc, _TRANSPORT):
        return None  # a timeout already took the whole deadline; not JSON: not busy
    return min(FIRST_PAUSE * 2 ** (attempt - 1), MAX_PAUSE)


def _details(exc: errors.APIError) -> list[dict]:
    """The google.rpc details of an error (ErrorInfo, QuotaFailure, RetryInfo...)."""
    error = exc.details.get("error", exc.details) if isinstance(exc.details, dict) else {}
    details = error.get("details") if isinstance(error, dict) else None
    return [d for d in details or [] if isinstance(d, dict)]


def _key_rejected(exc: errors.APIError) -> bool:
    """401 and 403, or a 400 about the key: Gemini answers an invalid or expired key with
    400 INVALID_ARGUMENT and reason API_KEY_INVALID."""
    if exc.code in (401, 403):
        return True
    if exc.code != 400:
        return False
    reasons = [str(d.get("reason") or "") for d in _details(exc)]
    return (
        any(r.startswith("API_KEY_") for r in reasons) or "api key" in (exc.message or "").lower()
    )


def _quota_used_up(exc: errors.APIError) -> str | None:
    """For a 429 that waiting a minute won't fix: "none" when the key's plan has no quota at
    all for the model (a free key and a Pro model: "limit: 0"), "day" when its daily quota is
    used up. None for a per-minute limit, which passes by itself."""
    violations = [
        v
        for d in _details(exc)
        if str(d.get("@type", "")).endswith("QuotaFailure")
        for v in d.get("violations") or []
        if isinstance(v, dict)
    ]
    if _ZERO_LIMIT.search(exc.message or "") or any(
        str(v.get("quotaValue", "")).strip() == "0" for v in violations
    ):
        return "none"
    names = " ".join(f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}" for v in violations)
    text = f"{names} {exc.message or ''}".lower()
    if "perday" in text or "per day" in text or "per_day" in text:
        return "day"
    return None


def _retry_delay(exc: errors.APIError) -> float | None:
    """The wait Gemini asks for (RetryInfo), in seconds."""
    for detail in _details(exc):
        if str(detail.get("@type", "")).endswith("RetryInfo"):
            delay = _SECONDS.match(str(detail.get("retryDelay", "")))
            if delay:
                return float(delay.group(1))
    return None


def _no_quota(used_up: str, model: str) -> str:
    if used_up == "day":
        return f"this key's plan has no quota left for {model} today"
    return f"this key's plan has no quota at all for {model}"


def _instead(model: str) -> str:
    """What to choose instead: Flash models have far larger free quotas than Pro."""
    return "another model" if "flash" in model.lower() else "a Flash model"


def _name(value: Any) -> str | None:
    """An SDK enum (or an unknown value it passed through) as its upper-case name."""
    if value is None:
        return None
    return str(getattr(value, "value", value)).upper()


def _bare(name: str | None) -> str:
    return (name or "").removeprefix("models/")


def _rank(name: str) -> tuple:
    """Pro, then Flash, then Flash-Lite; within each, newer versions first and stable
    releases before previews."""
    version = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
    if "flash-lite" in name:
        tier = 2
    elif "flash" in name:
        tier = 1
    elif "pro" in name:
        tier = 0
    else:
        tier = 3
    preview = "preview" in name or "exp" in name
    return (tier, -float(version.group(1)) if version else 0.0, preview, len(name), name)


def _cost(model: str, prompt: int, output: int, cached: int) -> Decimal:
    """prompt includes the cached tokens, which are billed at the cache rate. Unknown models
    cost 0 rather than a wrong guess."""
    table = LONG_CONTEXT_PRICING if prompt > LONG_CONTEXT_TOKENS else PRICING
    prices = _prices_for(model, table) or _prices_for(model, PRICING)
    if prices is None:
        return Decimal("0")
    input_price, output_price, cache_price = prices
    cached = min(cached, prompt)
    total = (
        (prompt - cached) * input_price + cached * cache_price + output * output_price
    ) / _MILLION
    return total.quantize(_COST_STEP, rounding=ROUND_HALF_UP)


def _prices_for(
    model: str, table: dict[str, tuple[Decimal, Decimal, Decimal]]
) -> tuple[Decimal, Decimal, Decimal] | None:
    if model in table:
        return table[model]
    # Dated or suffixed ids ("gemini-2.5-flash-preview-09-2025"); the longest prefix wins so
    # "gemini-2.5-flash-lite-..." is not priced as "gemini-2.5-flash".
    matches = [name for name in table if model.startswith(f"{name}-")]
    return table[max(matches, key=len)] if matches else None
