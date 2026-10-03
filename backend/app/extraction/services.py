"""The AI services an office can use to read documents, and how to tell them apart by key.

Pure data: no SDK is imported here, so settings and the API can use it cheaply. Each service
is served by one adapter (see app.extraction.adapters): Anthropic and Gemini have their own,
OpenAI its own, and every other service speaks the OpenAI chat-completions protocol at its
own base URL ("compatible").
"""

import re
from dataclasses import dataclass
from typing import Literal

Adapter = Literal["anthropic", "gemini", "openai", "compatible"]


@dataclass(frozen=True)
class Service:
    id: str
    name: str  # shown in Settings
    short: str  # used in messages: "Claude declined to read this document"
    adapter: Adapter
    key_name: str  # "Anthropic API key"
    key_help: str  # where to get a key
    default_model: str
    models: tuple[str, ...]  # suggested on the settings screen
    env_keys: tuple[str, ...] = ()  # Settings attributes / environment variables, in order
    key_prefixes: tuple[str, ...] = ()
    key_pattern: str | None = None  # a stricter pattern, tried before the prefixes
    base_url: str | None = None  # fixed endpoint of an OpenAI-compatible service
    custom_base_url: bool = False  # the office enters the endpoint
    key_optional: bool = False  # a local server needs no key
    supports_effort: bool = False
    reads_pdf: bool = True  # takes a PDF as is; otherwise it gets page images and text
    reads_images: bool = True  # False: text only (scans can't be read)

    @property
    def in_sentence(self) -> str:
        """short for use mid-sentence: "not sent to the AI service", not "to The AI service"."""
        return f"t{self.short[1:]}" if self.short.startswith("The ") else self.short


SERVICES: tuple[Service, ...] = (
    Service(
        id="anthropic",
        name="Claude (Anthropic)",
        short="Claude",
        adapter="anthropic",
        key_name="Anthropic API key",
        key_help="Create a key in your Anthropic Console account, under API keys. It starts "
        "with sk-ant-.",
        default_model="claude-opus-5-5",
        models=("claude-opus-5-5", "claude-sonnet-5-5"),
        env_keys=("anthropic_api_key",),
        key_prefixes=("sk-ant-",),
        supports_effort=True,
    ),
    Service(
        id="gemini",
        name="Gemini (Google)",
        short="Gemini",
        adapter="gemini",
        key_name="Gemini API key",
        key_help="Create a key in Google AI Studio (aistudio.google.com), under Get API key.",
        default_model="gemini-2.5-pro",
        models=("gemini-2.5-pro", "gemini-2.5-flash"),
        env_keys=("gemini_api_key", "google_api_key"),
        key_prefixes=("AIza", "AQ."),
    ),
    Service(
        id="openai",
        name="OpenAI (GPT)",
        short="OpenAI",
        adapter="openai",
        key_name="OpenAI API key",
        key_help="Create a key at platform.openai.com, under API keys. It starts with sk-.",
        default_model="gpt-5",
        models=("gpt-5", "gpt-5-mini"),
        env_keys=("openai_api_key",),
        key_prefixes=("sk-proj-", "sk-svcacct-", "sk-admin-", "sk-"),
    ),
    Service(
        id="openrouter",
        name="OpenRouter (many models, one key)",
        short="OpenRouter",
        adapter="compatible",
        key_name="OpenRouter API key",
        key_help="Create a key at openrouter.ai, under Keys. It starts with sk-or-.",
        default_model="google/gemini-2.5-pro",
        models=("google/gemini-2.5-pro", "openai/gpt-5", "anthropic/claude-sonnet-4.5"),
        env_keys=("openrouter_api_key",),
        key_prefixes=("sk-or-",),
        base_url="https://openrouter.ai/api/v1",
        reads_pdf=False,
    ),
    Service(
        id="groq",
        name="Groq",
        short="Groq",
        adapter="compatible",
        key_name="Groq API key",
        key_help="Create a key at console.groq.com, under API Keys. It starts with gsk_.",
        default_model="meta-llama/llama-4-maverick-17b-128e-instruct",
        models=(
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "meta-llama/llama-4-scout-17b-16e-instruct",
        ),
        env_keys=("groq_api_key",),
        key_prefixes=("gsk_",),
        base_url="https://api.groq.com/openai/v1",
        reads_pdf=False,
    ),
    Service(
        id="xai",
        name="Grok (xAI)",
        short="Grok",
        adapter="compatible",
        key_name="xAI API key",
        key_help="Create a key at console.x.ai, under API Keys. It starts with xai-.",
        default_model="grok-4",
        models=("grok-4",),
        env_keys=("xai_api_key",),
        key_prefixes=("xai-",),
        base_url="https://api.x.ai/v1",
        reads_pdf=False,
    ),
    Service(
        id="deepseek",
        name="DeepSeek (text only)",
        short="DeepSeek",
        adapter="compatible",
        key_name="DeepSeek API key",
        key_help="Create a key at platform.deepseek.com, under API keys. DeepSeek reads only "
        "the text of a document, so scanned bills and photos can't be read with it.",
        default_model="deepseek-chat",
        models=("deepseek-chat",),
        env_keys=("deepseek_api_key",),
        # Same "sk-" start as OpenAI keys, but 32 hex digits after it.
        key_pattern=r"sk-[0-9a-f]{32}",
        base_url="https://api.deepseek.com/v1",
        reads_pdf=False,
        reads_images=False,
    ),
    Service(
        id="mistral",
        name="Mistral",
        short="Mistral",
        adapter="compatible",
        key_name="Mistral API key",
        key_help="Create a key at console.mistral.ai, under API Keys.",
        default_model="mistral-medium-latest",
        models=("mistral-medium-latest", "mistral-small-latest"),
        env_keys=("mistral_api_key",),
        base_url="https://api.mistral.ai/v1",
        reads_pdf=False,
    ),
    Service(
        id="custom",
        name="Other (OpenAI-compatible)",
        short="The AI service",
        adapter="compatible",
        key_name="API key",
        key_help="Any service with an OpenAI-compatible API, such as Together, Fireworks, or a "
        "model on this network through Ollama or LM Studio. Enter its address and the model "
        "name too. A local server may not need a key.",
        default_model="",
        models=(),
        env_keys=("ai_api_key",),
        custom_base_url=True,
        key_optional=True,
        reads_pdf=False,
    ),
)

BY_ID: dict[str, Service] = {s.id: s for s in SERVICES}
DEFAULT = BY_ID["anthropic"]
# Model names as services publish them: no spaces; "/" and ":" appear in OpenRouter and
# Ollama names, "_" in Ollama and LM Studio ones ("llama3.1:8b-instruct-q4_K_M").
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+_-]{0,199}")


def get(service_id: str | None) -> Service | None:
    return BY_ID.get(service_id or "")


def detect(key: str | None) -> Service | None:
    """The service a key belongs to, judged by its format; None when it can't be told."""
    key = (key or "").strip()
    if not key:
        return None
    for service in SERVICES:
        if service.key_pattern and re.fullmatch(service.key_pattern, key):
            return service
    prefixed = [(p, s) for s in SERVICES for p in s.key_prefixes if key.startswith(p)]
    if not prefixed:
        return None
    return max(prefixed, key=lambda item: len(item[0]))[1]  # "sk-ant-" beats "sk-"
