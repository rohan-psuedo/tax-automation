"""Any AI service's key in Settings: recognising a pasted key, saving it per service
(encrypted), choosing the service in use, models and addresses, the environment's keys, the
test-ai and ai-models endpoints, and documents that waited for a key being read once one is
saved. Adapters are faked (extractor.build_adapter is replaced): no network calls."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import security_box
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.extraction import extractor
from app.extraction import services as catalog
from app.extraction.base import Check, ExtractionError, Limits, Reply
from app.models import AppSetting, AuditEvent
from app.services import app_settings
from tests import samples
from tests import test_vouchers_api as flows

CLAUDE_KEY = "sk-ant-api03-" + "Zq8Xv3Lm" * 11 + "4f2a"
GEMINI_KEY = "AIzaSyD" + "k3Lm9Qx2" * 4
GOOGLE_AQ_KEY = "AQ.Ab8RN6" + "Jq2Vt7Hs" * 6 + "p0Lk"
OPENAI_KEY = "sk-proj-" + "Tz4Kp8Rw" * 12 + "Qv7n"
OPENAI_LEGACY_KEY = "sk-" + "Ab3dEf6h" * 6
OPENROUTER_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
GROQ_KEY = "gsk_" + "Wb7Nq2Xc" * 7
XAI_KEY = "xai-" + "Hr5Tg8Jk" * 10
DEEPSEEK_KEY = "sk-" + "3f9a2b1c" * 4  # 32 hex digits
MISTRAL_KEY = "Mq7Rb2Lx" * 4  # no recognisable start
ENV_GEMINI_KEY = "AIzaSyE" + "nv0Key9a" * 4
ENV_OPENAI_KEY = "sk-proj-" + "Env0Key9" * 12 + "e9d1"
ALL_KEYS = [
    CLAUDE_KEY,
    GEMINI_KEY,
    GOOGLE_AQ_KEY,
    OPENAI_KEY,
    OPENAI_LEGACY_KEY,
    OPENROUTER_KEY,
    GROQ_KEY,
    XAI_KEY,
    DEEPSEEK_KEY,
    MISTRAL_KEY,
    ENV_GEMINI_KEY,
    ENV_OPENAI_KEY,
]

# Every AI variable the app reads from backend/.env or the process environment.
AI_ENV = {
    "ai_provider": None,
    "ai_model": None,
    "ai_base_url": None,
    "anthropic_api_key": None,
    "claude_model": "claude-opus-5-5",
    "claude_effort": "medium",
    "gemini_api_key": None,
    "google_api_key": None,
    "openai_api_key": None,
    "openrouter_api_key": None,
    "groq_api_key": None,
    "xai_api_key": None,
    "deepseek_api_key": None,
    "mistral_api_key": None,
    "ai_api_key": None,
}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch):
    """No AI key from this machine's backend/.env or environment reaches the tests."""
    settings = get_settings()
    for name, value in AI_ENV.items():
        monkeypatch.setattr(settings, name, value)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(settings, "tally_url", "http://127.0.0.1:9000")
    app_settings.invalidate()
    return settings


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setattr(get_settings(), name, value)
    app_settings.invalidate()


class FakeAdapter:
    """Stands in for any service's adapter; what it answers is set in the `ai` fixture."""

    budgets = (None,)

    def __init__(self, service: catalog.Service, state: app_settings.ServiceSettings, ai: dict):
        self.service = service
        self.key = state.api_key
        self.model = state.model
        self.base_url = state.base_url
        self._ai = ai

    def limits(self) -> Limits:
        return Limits(
            max_pdf_bytes=20 * 1024 * 1024,
            pdf_label="20 MB",
            max_image_payload=30_000_000,
            max_images=20,
        )

    def request(self, doc, instructions: str, *, budget: int | None) -> Reply:
        self._ai["requests"].append((self.service.id, self.model))
        if isinstance(self._ai["reply"], Exception):
            raise self._ai["reply"]
        return Reply(
            extraction=flows.extraction(),
            stop="done",
            model=self.model,
            input_tokens=1000,
            output_tokens=500,
        )

    def check(self) -> Check:
        if isinstance(self._ai["check"], Exception):
            raise self._ai["check"]
        return self._ai["check"]

    def list_models(self) -> list[str]:
        if isinstance(self._ai["models"], Exception):
            raise self._ai["models"]
        return self._ai["models"]


@pytest.fixture(autouse=True)
def ai(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replaces extractor.build_adapter. A service that isn't ready goes through the real
    build_adapter, so its "not set up" error is the real one."""
    state: dict = {
        "built": [],
        "requests": [],
        "reply": None,
        "check": Check(ok=True, detail="Connected."),
        "models": ["model-a", "model-b"],
        "build_error": None,
    }
    real = extractor.build_adapter

    def build_adapter(service, settings, *, client=None):
        current = settings.service_settings(service.id)
        if not current.ready:
            real(service, settings)
            raise AssertionError(f"build_adapter accepted {service.id}, which is not ready")
        if state["build_error"] is not None:
            raise state["build_error"]
        adapter = FakeAdapter(service, current, state)
        state["built"].append(adapter)
        return adapter

    monkeypatch.setattr(extractor, "build_adapter", build_adapter)
    return state


def _put(client: TestClient, **body) -> dict:
    resp = client.put("/api/settings", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _refused(client: TestClient, **body) -> str:
    resp = client.put("/api/settings", json=body)
    assert resp.status_code == 422, resp.text
    return resp.text


def _service(body: dict, service_id: str) -> dict:
    return next(s for s in body["ai"]["services"] if s["id"] == service_id)


def _rows() -> dict[str, object]:
    with SessionLocal() as db:
        return {row.key: row.value for row in db.scalars(select(AppSetting))}


def _events(action: str) -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)
            )
        )


def _changed() -> list[list[str]]:
    return [e.data["changed"] for e in _events("settings.updated")]


def _leaks(text: str) -> list[str]:
    """Keys (or a long piece of one) found in text."""
    return [key for key in ALL_KEYS if key in text or key[8:30] in text]


def _login_as(client: TestClient, role: str) -> None:
    email = f"{role}@ca.test"
    resp = client.post(
        "/api/users",
        json={"email": email, "full_name": role.title(), "password": "role-pass-1", "role": role},
    )
    assert resp.status_code == 201, resp.text
    client.cookies.clear()
    resp = client.post("/api/auth/login", json={"email": email, "password": "role-pass-1"})
    assert resp.status_code == 200, resp.text


# -- recognising a key -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "service"),
    [
        (CLAUDE_KEY, "anthropic"),
        ("sk-ant-admin01-" + "Ab3dEf6h" * 8, "anthropic"),
        (GEMINI_KEY, "gemini"),
        (GOOGLE_AQ_KEY, "gemini"),
        (OPENAI_KEY, "openai"),
        ("sk-svcacct-" + "Ab3dEf6h" * 8, "openai"),
        ("sk-admin-" + "Ab3dEf6h" * 8, "openai"),
        (OPENAI_LEGACY_KEY, "openai"),
        (OPENROUTER_KEY, "openrouter"),
        (GROQ_KEY, "groq"),
        (XAI_KEY, "xai"),
        (DEEPSEEK_KEY, "deepseek"),
        (f"  {GEMINI_KEY}\n", "gemini"),
    ],
)
def test_detect_recognises_each_services_key(key: str, service: str):
    assert catalog.detect(key).id == service


@pytest.mark.parametrize(
    "key",
    [
        MISTRAL_KEY,  # Mistral keys have no recognisable start
        "tgp_v1_" + "Ab3dEf6h" * 6,  # Together, through the "Other" service
        "hello",
        "",
        "   ",
        None,
        "SK-ANT-api03-abc",  # case matters
        "sk_ant_api03",
    ],
)
def test_detect_gives_none_for_unknown_keys(key: str | None):
    assert catalog.detect(key) is None


def test_the_longest_matching_start_wins():
    """Claude, OpenRouter and DeepSeek keys all start with "sk-", like OpenAI's."""
    assert catalog.detect("sk-ant-" + "x" * 40).id == "anthropic"
    assert catalog.detect("sk-or-" + "x" * 40).id == "openrouter"
    assert catalog.detect("sk-" + "x" * 40).id == "openai"
    # DeepSeek: exactly 32 lower-case hex digits after "sk-"; anything else is OpenAI's.
    assert catalog.detect("sk-" + "a1" * 16).id == "deepseek"
    assert catalog.detect("sk-" + "a1" * 16 + "f").id == "openai"
    assert catalog.detect("sk-" + "a1" * 15 + "f").id == "openai"
    assert catalog.detect("sk-" + "A1" * 16).id == "openai"
    assert catalog.detect("sk-proj-" + "a1" * 16).id == "openai"


def test_catalog_is_consistent():
    env_fields = set(Settings.model_fields)
    assert catalog.SERVICES[0] is catalog.DEFAULT and catalog.DEFAULT.id == "anthropic"
    assert len(catalog.BY_ID) == len(catalog.SERVICES)
    for service in catalog.SERVICES:
        assert service.adapter in ("anthropic", "gemini", "openai", "compatible"), service.id
        # Every key variable is a setting, so backend/.env can set it.
        assert set(service.env_keys) <= env_fields, service.id
        for prefix in service.key_prefixes:
            assert catalog.detect(prefix + "Q7w" * 15) is service, (service.id, prefix)
        if service.custom_base_url:
            assert service.base_url is None and service.models == ()
        else:
            assert service.default_model in service.models, service.id
            assert service.adapter != "compatible" or service.base_url, service.id
        for model in service.models:
            assert catalog.MODEL_PATTERN.fullmatch(model), model
    assert catalog.get("custom").in_sentence == "the AI service"
    assert catalog.get("gemini").in_sentence == "Gemini"


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.5-pro",
        "google/gemini-2.5-pro",
        "llama3.1:8b-instruct-q4_K_M",
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
        "claude-sonnet-4@20250514",
    ],
)
def test_model_names_as_services_publish_them(model: str):
    assert catalog.MODEL_PATTERN.fullmatch(model)


# -- saving a pasted key -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "service", "hint"),
    [
        (GEMINI_KEY, "gemini", "AIzaSyD…9Qx2"),
        (GOOGLE_AQ_KEY, "gemini", "AQ.Ab8R…p0Lk"),
        (OPENAI_KEY, "openai", "sk-proj…Qv7n"),
        (OPENROUTER_KEY, "openrouter", "sk-or-v…cdef"),
        (GROQ_KEY, "groq", "gsk_Wb7…q2Xc"),
        (XAI_KEY, "xai", "xai-Hr5…g8Jk"),
        (DEEPSEEK_KEY, "deepseek", "sk-3f9a…2b1c"),
        (CLAUDE_KEY, "anthropic", "sk-ant-…4f2a"),
    ],
)
def test_a_pasted_key_goes_to_its_service_and_puts_it_in_use(
    admin_client: TestClient, caplog, key: str, service: str, hint: str
):
    caplog.set_level(logging.DEBUG)

    resp = admin_client.put("/api/settings", json={"ai_api_key": f"  {key}\n"})

    assert resp.status_code == 200, resp.text
    ai = resp.json()["ai"]
    assert ai["provider"] == service
    assert (ai["configured"], ai["source"], ai["key_hint"]) == (True, "settings", hint)
    assert ai["model"] == catalog.get(service).default_model
    listed = _service(resp.json(), service)
    assert (listed["configured"], listed["source"], listed["key_hint"]) == (True, "settings", hint)
    # Encrypted at rest under the service's own row.
    rows = _rows()
    stored = rows[app_settings.key_setting(service)]
    assert isinstance(stored, str) and key not in stored and key[8:30] not in stored
    assert security_box.decrypt(stored) == key
    assert rows["ai_provider"] == service
    assert app_settings.current().service_settings(service).api_key == key
    others = [s for s in catalog.SERVICES if s.id != service]
    assert not any(app_settings.key_setting(s.id) in rows for s in others)
    # Never shown again: not in any response, log, audit event or repr.
    texts = [resp.text, admin_client.get("/api/settings").text, caplog.text]
    texts += [json.dumps(e.data) for e in _events("settings.updated")]
    texts.append(repr(app_settings.current()))
    texts.append(admin_client.get("/api/activity").text)
    assert not any(_leaks(text) for text in texts)


def test_a_key_pasted_with_its_variable_name_or_quotes_is_saved_clean(admin_client: TestClient):
    _put(admin_client, ai_api_key=f"GEMINI_API_KEY={GEMINI_KEY}")
    assert app_settings.current().service_settings("gemini").api_key == GEMINI_KEY

    ai = _put(admin_client, ai_api_key=f'"{OPENAI_KEY}"')["ai"]
    assert ai["provider"] == "openai"
    assert app_settings.current().service_settings("openai").api_key == OPENAI_KEY


def test_a_key_that_cant_be_told_goes_to_the_service_in_use(admin_client: TestClient):
    _put(admin_client, ai_provider="mistral")

    ai = _put(admin_client, ai_api_key=MISTRAL_KEY)["ai"]

    assert (ai["provider"], ai["configured"], ai["source"]) == ("mistral", True, "settings")
    assert security_box.decrypt(_rows()["mistral_api_key"]) == MISTRAL_KEY
    assert "anthropic_api_key" not in _rows()


def test_an_explicit_service_wins_over_the_keys_format(admin_client: TestClient):
    """The "Other" service can be OpenRouter or OpenAI behind a proxy: its key is any key."""
    _put(admin_client, ai_provider="custom", ai_api_key=OPENAI_KEY)

    rows = _rows()
    assert security_box.decrypt(rows["custom_api_key"]) == OPENAI_KEY
    assert "openai_api_key" not in rows and rows["ai_provider"] == "custom"


def test_each_service_keeps_its_own_key(admin_client: TestClient):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    _put(admin_client, ai_api_key=OPENAI_KEY)
    body = _put(admin_client, ai_api_key=CLAUDE_KEY)

    assert body["ai"]["provider"] == "anthropic"
    hints = {s["id"]: s["key_hint"] for s in body["ai"]["services"] if s["configured"]}
    assert hints == {
        "anthropic": "sk-ant-…4f2a",
        "gemini": "AIzaSyD…9Qx2",
        "openai": "sk-proj…Qv7n",
    }
    # Switching back needs no key again.
    ai = _put(admin_client, ai_provider="gemini")["ai"]
    assert (ai["provider"], ai["configured"], ai["key_hint"]) == ("gemini", True, "AIzaSyD…9Qx2")


def test_removing_a_key(admin_client: TestClient, monkeypatch):
    _put(admin_client, ai_api_key=GEMINI_KEY)

    ai = _put(admin_client, ai_api_key="")["ai"]  # the key of the service in use

    assert (ai["provider"], ai["configured"], ai["key_hint"]) == ("gemini", False, None)
    assert "gemini_api_key" not in _rows()
    _env(monkeypatch, gemini_api_key=ENV_GEMINI_KEY)
    ai = admin_client.get("/api/settings").json()["ai"]
    assert (ai["configured"], ai["source"], ai["key_hint"]) == (True, "env", "AIzaSyE…ey9a")


def test_saving_the_same_key_again_changes_nothing(admin_client: TestClient):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    _put(admin_client, ai_api_key=GEMINI_KEY)
    _put(admin_client, ai_provider="gemini", ai_api_key=f" {GEMINI_KEY} ")

    assert _changed() == [["ai_provider", "gemini_api_key"]]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"ai_api_key": f"AIzaSyD k3Lm9Qx2 {GEMINI_KEY[7:]}"}, "spaces or line breaks"),
        ({"ai_api_key": f"export GEMINI_API_KEY={GEMINI_KEY}"}, "spaces or line breaks"),
        ({"ai_provider": DEEPSEEK_KEY}, "not an AI service this app knows"),
        ({"ai_provider": "chatgpt"}, "Choose one of: anthropic, gemini, openai"),
        ({"ai_provider": "gemini", "ai_model": GEMINI_KEY}, "looks like an API key"),
        ({"ai_provider": "openai", "ai_model": OPENAI_KEY}, "looks like an API key"),
        ({"ai_provider": "custom", "ai_model": f"{MISTRAL_KEY} x"}, "not a model name"),
        ({"ai_provider": "custom", "ai_base_url": OPENAI_KEY}, "not a valid address"),
        ({"ai_provider": "custom", "ai_base_url": "ftp://192.168.1.30/v1"}, "not a valid address"),
        ({"ai_provider": "custom", "ai_base_url": "localhost:11434"}, "not a valid address"),
        ({"ai_provider": "gemini", "ai_base_url": "http://10.0.0.5/v1"}, "is fixed"),
        ({"ai_provider": "gemini", "ai_model": "gemini 2.5 pro"}, "not a model name"),
        ({"ai_provider": "gemini", "ai_model": "../../etc/passwd"}, "not a model name"),
        ({"ai_provider": "gemini", "ai_model": "gemini<script>"}, "not a model name"),
        ({"ai_provider": "custom", "ai_model": "http://10.0.0.5:11434/v1"}, "an address"),
        ({"ai_provider": "anthropic", "ai_model": "gpt-5"}, "not an available model"),
        ({"ai_api_key": GEMINI_KEY, "ai_model": "gemini 2.5"}, "not a model name"),
        ({"ai_api_key": "x" * 501}, "500 characters"),
    ],
    ids=[
        "key-spaces",
        "key-with-export",
        "provider-is-a-key",
        "provider-unknown",
        "model-is-gemini-key",
        "model-is-openai-key",
        "custom-model-spaces",
        "address-is-a-key",
        "address-scheme",
        "address-no-scheme",
        "address-of-fixed-service",
        "model-spaces",
        "model-path",
        "model-junk",
        "model-is-address",
        "claude-model-not-offered",
        "nothing-saved-when-one-field-is-wrong",
        "key-too-long",
    ],
)
def test_invalid_ai_values_are_refused_without_repeating_them(
    admin_client: TestClient, caplog, body: dict, expected: str
):
    caplog.set_level(logging.DEBUG)

    text = _refused(admin_client, **body)

    assert expected in text
    assert not _leaks(text) and not _leaks(caplog.text)
    assert _rows() == {} and _changed() == []


# -- choosing the service, its model and address -----------------------------------------


def test_switching_the_service_in_use(admin_client: TestClient):
    body = _put(admin_client, ai_provider="openai")

    ai = body["ai"]
    assert (ai["provider"], ai["configured"], ai["source"], ai["key_hint"]) == (
        "openai",
        False,
        None,
        None,
    )
    assert ai["model"] == "gpt-5" and ai["models"] == ["gpt-5", "gpt-5-mini"]
    assert app_settings.current().ai_provider == "openai"

    ai = _put(admin_client, ai_provider=" anthropic ")["ai"]
    assert ai["provider"] == "anthropic" and ai["models"] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    assert _changed() == [["ai_provider"], ["ai_provider"]]


def test_empty_provider_goes_back_to_the_automatic_choice(admin_client: TestClient, monkeypatch):
    _env(monkeypatch, openai_api_key=ENV_OPENAI_KEY)
    assert _put(admin_client, ai_provider="gemini")["ai"]["provider"] == "gemini"

    ai = _put(admin_client, ai_provider="")["ai"]

    assert (ai["provider"], ai["configured"], ai["source"]) == ("openai", True, "env")
    assert "ai_provider" not in _rows()


def test_models_for_claude_and_for_other_services(admin_client: TestClient):
    ai = _put(admin_client, ai_provider="anthropic", ai_model="claude-sonnet-5-5")["ai"]
    assert ai["model"] == "claude-sonnet-5-5" and _rows()["claude_model"] == "claude-sonnet-5-5"

    body = _put(admin_client, ai_provider="gemini", ai_model=" gemini-2.5-flash ")
    gemini = _service(body, "gemini")
    assert (gemini["model"], gemini["models"]) == (
        "gemini-2.5-flash",
        ["gemini-2.5-pro", "gemini-2.5-flash"],
    )

    # Any model the service publishes is accepted, and offered first from then on.
    _put(admin_client, ai_provider="gemini")
    ai = _put(admin_client, ai_model="gemini-3.0-pro-preview-0925")["ai"]
    assert (ai["provider"], ai["model"]) == ("gemini", "gemini-3.0-pro-preview-0925")
    assert ai["models"] == ["gemini-3.0-pro-preview-0925", "gemini-2.5-pro", "gemini-2.5-flash"]
    assert _rows()["gemini_model"] == "gemini-3.0-pro-preview-0925"

    body = _put(admin_client, ai_provider="openrouter", ai_model="anthropic/claude-sonnet-4.5")
    assert _service(body, "openrouter")["model"] == "anthropic/claude-sonnet-4.5"

    # "" goes back to the default model, for Claude too.
    assert _service(_put(admin_client, ai_model=""), "gemini")["model"] == "gemini-2.5-pro"
    body = _put(admin_client, ai_provider="anthropic", ai_model="")
    assert _service(body, "anthropic")["model"] == "claude-opus-5-5"
    rows = _rows()
    assert "gemini_model" not in rows and "claude_model" not in rows
    assert rows["openrouter_model"] == "anthropic/claude-sonnet-4.5"
    assert _changed() == [
        ["claude_model"],
        ["gemini_model"],
        ["ai_provider"],
        ["gemini_model"],
        ["openrouter_model"],
        ["gemini_model"],
        ["claude_model"],
    ]


def test_editing_another_service_leaves_the_service_in_use_alone(admin_client: TestClient):
    """The settings screen saves a model, an address or a key removal with ai_provider naming
    the service shown; documents read meanwhile must not go to that service."""
    _put(admin_client, ai_api_key=CLAUDE_KEY)
    _put(admin_client, ai_provider="gemini", ai_api_key=GEMINI_KEY)  # a key: now in use
    _put(admin_client, ai_provider="anthropic")  # on its own: now in use

    _put(admin_client, ai_provider="gemini", ai_model="gemini-2.5-flash")
    _put(admin_client, ai_provider="custom", ai_base_url="http://10.0.0.9:8000/v1")
    body = _put(admin_client, ai_provider="gemini", ai_api_key="")

    assert body["ai"]["provider"] == "anthropic" and body["ai"]["configured"] is True
    assert _rows()["ai_provider"] == "anthropic"
    assert _service(body, "gemini")["model"] == "gemini-2.5-flash"
    assert _service(body, "gemini")["source"] is None
    assert _service(body, "custom")["base_url"] == "http://10.0.0.9:8000/v1"
    assert _changed()[-3:] == [["gemini_model"], ["custom_base_url"], ["gemini_api_key"]]


def test_the_other_service_needs_an_address_and_a_model_but_no_key(admin_client: TestClient):
    ai = _put(admin_client, ai_provider="custom")["ai"]
    assert (ai["provider"], ai["configured"], ai["model"], ai["models"]) == (
        "custom",
        False,
        "",
        [],
    )

    body = _put(admin_client, ai_base_url=" http://192.168.1.30:11434/v1/chat/completions/ ")
    custom = _service(body, "custom")
    assert custom["base_url"] == "http://192.168.1.30:11434/v1" and not custom["configured"]

    body = _put(admin_client, ai_model="llama3.1:8b-instruct-q4_K_M")
    ai = body["ai"]
    assert (ai["configured"], ai["source"], ai["key_hint"]) == (True, None, None)
    assert ai["model"] == "llama3.1:8b-instruct-q4_K_M"
    assert ai["models"] == ["llama3.1:8b-instruct-q4_K_M"]
    state = app_settings.current().ai
    assert (state.id, state.base_url, state.api_key) == (
        "custom",
        "http://192.168.1.30:11434/v1",
        None,
    )

    ai = _put(admin_client, ai_base_url="")["ai"]  # "" removes the address
    assert ai["configured"] is False and "custom_base_url" not in _rows()
    assert _changed() == [
        ["ai_provider"],
        ["custom_base_url"],
        ["custom_model"],
        ["custom_base_url"],
    ]


def test_an_empty_address_for_a_fixed_service_is_ignored(admin_client: TestClient):
    """A settings screen may send the address field for every service."""
    body = _put(admin_client, ai_provider="groq", ai_base_url="", ai_api_key=GROQ_KEY)

    assert _service(body, "groq")["base_url"] == "https://api.groq.com/openai/v1"
    assert "custom_base_url" not in _rows()


def test_model_and_address_apply_to_the_service_in_use(admin_client: TestClient):
    _put(admin_client, ai_api_key=GEMINI_KEY)

    _put(admin_client, ai_model="gemini-2.5-flash")

    effective = app_settings.current()
    assert effective.service_settings("gemini").model == "gemini-2.5-flash"
    assert effective.claude_model == "claude-opus-5-5"


def test_a_key_and_model_for_a_detected_service(admin_client: TestClient):
    ai = _put(admin_client, ai_api_key=OPENROUTER_KEY, ai_model="openai/gpt-5")["ai"]

    assert (ai["provider"], ai["model"]) == ("openrouter", "openai/gpt-5")


# -- the environment ---------------------------------------------------------------------


def test_environment_keys_of_each_service(admin_client: TestClient, monkeypatch):
    _env(
        monkeypatch,
        gemini_api_key=ENV_GEMINI_KEY,
        openai_api_key=ENV_OPENAI_KEY,
        groq_api_key=GROQ_KEY,
        deepseek_api_key=DEEPSEEK_KEY,
    )
    monkeypatch.setenv("MISTRAL_API_KEY", f" {MISTRAL_KEY} ")  # only in the process

    resp = admin_client.get("/api/settings")

    body = resp.json()
    configured = {s["id"]: (s["source"], s["key_hint"]) for s in body["ai"]["services"]}
    assert {k for k, v in configured.items() if v[0]} == {
        "gemini",
        "openai",
        "groq",
        "deepseek",
        "mistral",
    }
    assert configured["gemini"] == ("env", "AIzaSyE…ey9a")
    assert configured["mistral"] == ("env", "Mq7Rb2L…b2Lx")
    assert body["ai"]["provider"] == "gemini"  # the first one ready, in catalog order
    assert not _leaks(resp.text)


def test_google_api_key_counts_for_gemini(monkeypatch):
    _env(monkeypatch, google_api_key=GOOGLE_AQ_KEY)
    assert app_settings.current().service_settings("gemini").api_key == GOOGLE_AQ_KEY

    _env(monkeypatch, gemini_api_key=ENV_GEMINI_KEY)  # GEMINI_API_KEY comes first
    gemini = app_settings.current().service_settings("gemini")
    assert (gemini.api_key, gemini.key_source) == (ENV_GEMINI_KEY, "env")


def test_google_api_key_in_the_process_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", GEMINI_KEY)
    app_settings.invalidate()

    effective = app_settings.current()

    assert effective.ai_provider == "gemini" and effective.ai.api_key == GEMINI_KEY


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ({"openai_api_key": ENV_OPENAI_KEY}, "openai"),
        ({"openai_api_key": ENV_OPENAI_KEY, "anthropic_api_key": CLAUDE_KEY}, "anthropic"),
        ({"openai_api_key": ENV_OPENAI_KEY, "gemini_api_key": ENV_GEMINI_KEY}, "gemini"),
        ({"xai_api_key": XAI_KEY, "openrouter_api_key": OPENROUTER_KEY}, "openrouter"),
        ({}, "anthropic"),
    ],
    ids=["only-openai", "claude-first", "catalog-order", "openrouter-before-xai", "none"],
)
def test_without_a_choice_the_first_ready_service_is_used(monkeypatch, keys: dict, expected: str):
    _env(monkeypatch, **keys)

    assert app_settings.current().ai_provider == expected


def test_ai_provider_from_the_environment(admin_client: TestClient, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=app_settings.__name__)
    _env(monkeypatch, anthropic_api_key=CLAUDE_KEY, openai_api_key=ENV_OPENAI_KEY)

    _env(monkeypatch, ai_provider="openai")
    assert app_settings.current().ai_provider == "openai"

    _env(monkeypatch, ai_provider=" OpenAI ")  # as typed in backend/.env
    assert app_settings.current().ai_provider == "openai"

    _env(monkeypatch, ai_provider="mistral")  # chosen even without a key
    ai = admin_client.get("/api/settings").json()["ai"]
    assert (ai["provider"], ai["configured"]) == ("mistral", False)

    _env(monkeypatch, ai_provider="chatgpt")
    assert app_settings.current().ai_provider == "anthropic"
    assert "AI_PROVIDER in backend/.env names no AI service" in caplog.text

    # A choice saved in Settings wins over the environment.
    _env(monkeypatch, ai_provider="openai")
    assert _put(admin_client, ai_provider="gemini")["ai"]["provider"] == "gemini"


def test_ai_model_from_the_environment_is_for_the_ai_provider_only(monkeypatch):
    _env(monkeypatch, ai_provider="gemini", ai_model=" gemini-2.5-flash ")

    effective = app_settings.current()

    assert effective.ai.model == "gemini-2.5-flash"
    assert effective.service_settings("openai").model == "gpt-5"
    assert effective.claude_model == "claude-opus-5-5"  # Claude's comes from CLAUDE_MODEL


def test_a_saved_model_wins_over_the_environment(admin_client: TestClient, monkeypatch):
    _env(monkeypatch, ai_provider="gemini", ai_model="gemini-2.5-flash")

    assert _put(admin_client, ai_model="gemini-2.5-pro")["ai"]["model"] == "gemini-2.5-pro"
    ai = _put(admin_client, ai_model="")["ai"]
    assert ai["model"] == "gemini-2.5-flash"


def test_the_other_service_from_the_environment(admin_client: TestClient, monkeypatch):
    _env(
        monkeypatch,
        ai_provider="custom",
        ai_base_url=" http://localhost:1234/v1/ ",
        ai_model="qwen2.5-7b-instruct",
    )

    ai = admin_client.get("/api/settings").json()["ai"]
    assert (ai["provider"], ai["configured"], ai["model"], ai["source"]) == (
        "custom",
        True,
        "qwen2.5-7b-instruct",
        None,
    )
    assert app_settings.current().ai.base_url == "http://localhost:1234/v1"

    _env(monkeypatch, ai_api_key="lm-studio-local-key")
    assert app_settings.current().ai.api_key == "lm-studio-local-key"

    # A saved address wins over AI_BASE_URL.
    _put(admin_client, ai_base_url="http://192.168.1.30:11434/v1")
    assert app_settings.current().ai.base_url == "http://192.168.1.30:11434/v1"


def test_an_invalid_ai_base_url_in_the_environment_is_ignored(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=app_settings.__name__)
    _env(monkeypatch, ai_provider="custom", ai_base_url="localhost:1234", ai_model="qwen")

    state = app_settings.current().ai

    assert state.base_url is None and not state.ready
    assert "AI_BASE_URL in backend/.env is not a valid address" in caplog.text


def test_a_saved_key_wins_over_the_environment_key(admin_client: TestClient, monkeypatch):
    _env(monkeypatch, gemini_api_key=ENV_GEMINI_KEY)

    ai = _put(admin_client, ai_api_key=GEMINI_KEY)["ai"]
    assert (ai["source"], ai["key_hint"]) == ("settings", "AIzaSyD…9Qx2")

    ai = _put(admin_client, ai_api_key="")["ai"]
    assert (ai["provider"], ai["source"], ai["key_hint"]) == ("gemini", "env", "AIzaSyE…ey9a")


@pytest.mark.parametrize("value", ["chatgpt", 7, ["gemini"]])
def test_a_damaged_saved_provider_is_ignored(monkeypatch, value: object):
    _env(monkeypatch, openai_api_key=ENV_OPENAI_KEY)
    with SessionLocal() as db:
        db.add(AppSetting(key="ai_provider", value=value))
        db.commit()

    assert app_settings.current().ai_provider == "openai"


def test_a_saved_key_that_cant_be_decrypted_counts_as_not_set(environment, monkeypatch):
    original = environment.secret_key
    monkeypatch.setattr(environment, "secret_key", "the-previous-installation-secret")
    token = security_box.encrypt(GEMINI_KEY)
    monkeypatch.setattr(environment, "secret_key", original)
    with SessionLocal() as db:
        db.add(AppSetting(key="gemini_api_key", value=token))
        db.commit()

    gemini = app_settings.current().service_settings("gemini")

    assert gemini.api_key is None and gemini.key_source is None


# -- what GET shows ----------------------------------------------------------------------


def test_services_list_describes_each_service(admin_client: TestClient, monkeypatch):
    _env(monkeypatch, openai_api_key=ENV_OPENAI_KEY)
    _put(admin_client, ai_api_key=GEMINI_KEY, ai_model="gemini-2.5-flash")
    _put(admin_client, ai_provider="custom")
    _put(admin_client, ai_base_url="http://10.0.0.9:8000/v1")

    resp = admin_client.get("/api/settings")

    body = resp.json()
    services = {s["id"]: s for s in body["ai"]["services"]}
    assert services["gemini"] | {"key_help": None} == {
        "id": "gemini",
        "name": "Gemini (Google)",
        "key_name": "Gemini API key",
        "key_help": None,
        "key_optional": False,
        "custom_base_url": False,
        "supports_effort": False,
        "reads_images": True,
        "configured": True,
        "source": "settings",
        "key_hint": "AIzaSyD…9Qx2",
        "model": "gemini-2.5-flash",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "base_url": None,
    }
    assert "aistudio.google.com" in services["gemini"]["key_help"]
    openai = services["openai"]
    assert (openai["configured"], openai["source"], openai["key_hint"]) == (
        True,
        "env",
        "sk-proj…e9d1",
    )
    anthropic = services["anthropic"]
    assert anthropic["supports_effort"] and not anthropic["configured"]
    assert anthropic["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert services["deepseek"]["reads_images"] is False
    assert services["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"
    custom = services["custom"]
    assert (custom["key_optional"], custom["custom_base_url"], custom["configured"]) == (
        True,
        True,
        False,  # no model yet
    )
    assert custom["base_url"] == "http://10.0.0.9:8000/v1"
    # The top-level fields describe the service in use.
    ai = body["ai"]
    assert (ai["provider"], ai["configured"], ai["model"], ai["key_hint"]) == (
        "custom",
        False,
        "",
        None,
    )
    assert ai["effort"] == "medium" and ai["efforts"] == app_settings.EFFORTS
    assert not _leaks(resp.text)

    ai = _put(admin_client, ai_provider="gemini")["ai"]
    assert (ai["configured"], ai["source"], ai["key_hint"], ai["model"]) == (
        True,
        "settings",
        "AIzaSyD…9Qx2",
        "gemini-2.5-flash",
    )
    assert ai["models"] == ["gemini-2.5-pro", "gemini-2.5-flash"]


# -- test-ai -----------------------------------------------------------------------------


def test_ai_check_uses_the_adapter_of_the_service_in_use(admin_client: TestClient, ai, caplog):
    caplog.set_level(logging.DEBUG)
    _put(admin_client, ai_api_key=GEMINI_KEY, ai_model="gemini-2.5-flash")
    ai["check"] = Check(ok=True, detail="Connected. gemini-2.5-flash is available.")

    resp = admin_client.post("/api/settings/test-ai")

    assert resp.json() == {"ok": True, "detail": "Connected. gemini-2.5-flash is available."}
    [adapter] = ai["built"]
    assert (adapter.service.id, adapter.key, adapter.model) == (
        "gemini",
        GEMINI_KEY,
        "gemini-2.5-flash",
    )
    assert not _leaks(resp.text) and not _leaks(caplog.text)


def test_ai_check_reports_the_adapters_verdict(admin_client: TestClient, ai):
    _put(admin_client, ai_api_key=OPENAI_KEY)
    ai["check"] = Check(ok=False, detail="The key was rejected by OpenAI.")

    body = admin_client.post("/api/settings/test-ai").json()

    assert body == {"ok": False, "detail": "The key was rejected by OpenAI."}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"ai_provider": "gemini"}, "No Gemini API key is set. Add a key above"),
        ({"ai_provider": "openrouter"}, "No OpenRouter API key is set. Add a key above"),
        ({"ai_provider": "custom"}, "No address is set for the AI service. Enter it above"),
        (
            {"ai_provider": "custom", "then": {"ai_base_url": "http://10.0.0.9:8000/v1"}},
            "No model is chosen for the AI service. Choose one above",
        ),
        ({}, "No Anthropic API key is set. Add a key above"),
    ],
    ids=["gemini", "openrouter", "custom-address", "custom-model", "nothing-chosen"],
)
def test_ai_check_says_what_is_missing(admin_client: TestClient, ai, body: dict, expected: str):
    first = {k: v for k, v in body.items() if k != "then"}
    for change in (first, body.get("then")):
        if change:
            _put(admin_client, **change)

    result = admin_client.post("/api/settings/test-ai").json()

    assert result == {"ok": False, "detail": f"{expected}, save it, and test again."}
    assert ai["built"] == []


@pytest.mark.parametrize("where", ["check", "build_error"])
def test_ai_check_survives_an_adapter_bug(admin_client: TestClient, ai, caplog, where: str):
    caplog.set_level(logging.DEBUG)
    _put(admin_client, ai_api_key=GEMINI_KEY)
    ai[where] = RuntimeError(f"unexpected answer for key {GEMINI_KEY}")

    resp = admin_client.post("/api/settings/test-ai")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "could not be checked because of an error in this app" in resp.json()["detail"]
    assert "Gemini" in resp.json()["detail"] and "RuntimeError" in caplog.text
    assert not _leaks(resp.text) and not _leaks(caplog.text)


# -- ai-models ---------------------------------------------------------------------------


def test_listing_models_of_a_service(admin_client: TestClient, ai):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    _put(admin_client, ai_provider="anthropic")  # listing doesn't need the service in use
    ai["models"] = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]

    resp = admin_client.get("/api/settings/ai-models", params={"service": "gemini"})

    assert resp.json() == {
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "detail": None,
    }
    [adapter] = ai["built"]
    assert (adapter.service.id, adapter.key) == ("gemini", GEMINI_KEY)


def test_listing_models_of_the_other_service_before_one_is_chosen(admin_client: TestClient, ai):
    """The list is how an office picks the first model of its local server."""
    _put(admin_client, ai_provider="custom", ai_base_url="http://192.168.1.30:11434/v1")
    ai["models"] = ["llama3.1:8b", "qwen2.5:14b"]

    body = admin_client.get("/api/settings/ai-models", params={"service": "custom"}).json()

    assert body == {"models": ["llama3.1:8b", "qwen2.5:14b"], "detail": None}
    [adapter] = ai["built"]
    assert adapter.base_url == "http://192.168.1.30:11434/v1" and adapter.key is None


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        ("gemini", "Save a key for this service to see its models."),
        ("custom", "Save the service's address to see the models it offers."),
    ],
)
def test_listing_models_without_what_it_needs(
    admin_client: TestClient, ai, service: str, expected: str
):
    body = admin_client.get("/api/settings/ai-models", params={"service": service}).json()

    assert body == {"models": [], "detail": expected}
    assert ai["built"] == []


@pytest.mark.parametrize("where", ["models", "build_error"])
def test_a_failed_listing_gives_a_detail_and_no_exception_text(
    admin_client: TestClient, ai, caplog, where: str
):
    caplog.set_level(logging.DEBUG)
    _put(admin_client, ai_api_key=GEMINI_KEY)
    ai[where] = PermissionError(f"403 Forbidden for key {GEMINI_KEY}: project disabled")

    resp = admin_client.get("/api/settings/ai-models", params={"service": "gemini"})

    assert resp.status_code == 200
    assert resp.json() == {
        "models": [],
        "detail": "Gemini did not return its list of models. Type the name.",
    }
    assert "Forbidden" not in resp.text and "project disabled" not in caplog.text
    assert "PermissionError" in caplog.text
    assert not _leaks(resp.text) and not _leaks(caplog.text)


def test_listing_models_of_an_unknown_service(admin_client: TestClient):
    assert admin_client.get("/api/settings/ai-models", params={"service": "gpt"}).status_code == 404
    assert admin_client.get("/api/settings/ai-models").status_code == 422
    resp = admin_client.get("/api/settings/ai-models", params={"service": "x" * 41})
    assert resp.status_code == 422


@pytest.mark.parametrize("role", ["reviewer", "preparer"])
def test_ai_models_is_for_administrators_only(admin_client: TestClient, ai, role: str):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    _login_as(admin_client, role)

    resp = admin_client.get("/api/settings/ai-models", params={"service": "gemini"})

    assert resp.status_code == 403 and ai["built"] == []


def test_ai_models_needs_a_login(client: TestClient):
    assert client.get("/api/settings/ai-models", params={"service": "gemini"}).status_code == 401


# -- documents waiting for a key ---------------------------------------------------------


def _skipped_voucher(client: TestClient, company_id: int, name: str = "inv.pdf") -> dict:
    """Uploads a PDF while AI reading isn't set up; the worker hands it to manual entry."""
    voucher = flows._voucher(client, flows._upload_invoice(client, company_id, name)["id"])
    assert (voucher["status"], voucher["source"]) == ("needs_review", "manual")
    assert voucher["extraction_error"].startswith("Invoice reading is not set up")
    return voucher


def _voucher(client: TestClient, voucher: dict) -> dict:
    """The voucher as it is now."""
    return flows._voucher(client, voucher["document_id"])


def _reextract_events() -> list[AuditEvent]:
    return _events("voucher.reextract")


def test_a_document_that_waited_for_a_key_is_read_once_one_is_saved(
    admin_client: TestClient, company_id: int, ai
):
    voucher = _skipped_voucher(admin_client, company_id)
    assert _events("voucher.extraction_skipped")

    body = _put(admin_client, ai_api_key=GEMINI_KEY)

    assert body["requeued"] == 1
    queued = _voucher(admin_client, voucher)
    assert (queued["status"], queued["extraction_error"]) == ("pending", None)
    [event] = _reextract_events()
    assert event.data == {"reason": "ai_settings_changed"}
    assert event.entity_id == str(voucher["id"]) and event.company_id == company_id
    assert event.actor_id is not None
    feed = admin_client.get(f"/api/companies/{company_id}/activity").json()["items"]
    item = next(i for i in feed if i["action"] == "voucher.reextract")
    assert item["summary"].startswith("Queued the invoice")
    assert item["summary"].endswith("to be read now that AI reading is set up.")

    flows._process_all()

    read = _voucher(admin_client, voucher)
    assert (read["source"], read["model"], read["extraction_error"]) == (
        "ai",
        "gemini-2.5-pro",
        None,
    )
    assert read["invoice_number"] == "SE/2026/0042"
    assert ai["requests"] == [("gemini", "gemini-2.5-pro")]
    # Saving again finds nothing left waiting.
    assert _put(admin_client, ai_model="gemini-2.5-flash")["requeued"] == 0


def test_documents_a_person_has_edited_are_left_to_them(admin_client: TestClient, company_id: int):
    edited = _skipped_voucher(admin_client, company_id, "edited.pdf")
    waiting = _skipped_voucher(admin_client, company_id, "waiting.pdf")
    resp = admin_client.put(
        f"/api/vouchers/{edited['id']}",
        json={
            "invoice": edited["invoice"] | {"invoice_number": "MAN/001"},
            "choices": edited["choices"],
        },
    )
    assert resp.status_code == 200, resp.text

    assert _put(admin_client, ai_api_key=OPENAI_KEY)["requeued"] == 1

    assert _voucher(admin_client, waiting)["status"] == "pending"
    kept = _voucher(admin_client, edited)
    assert kept["status"] != "pending" and kept["invoice_number"] == "MAN/001"
    assert [e.entity_id for e in _reextract_events()] == [str(waiting["id"])]


def test_documents_that_failed_for_another_reason_are_not_queued(
    admin_client: TestClient, company_id: int, ai
):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    ai["reply"] = ExtractionError(
        "Gemini could not process this document: the file is damaged.", retryable=False
    )
    voucher = flows._voucher(admin_client, flows._upload_invoice(admin_client, company_id)["id"])
    assert voucher["extraction_error"].startswith("Gemini could not process")

    assert _put(admin_client, ai_api_key=OPENAI_KEY)["requeued"] == 0
    assert _put(admin_client, ai_provider="gemini", ai_model="gemini-2.5-flash")["requeued"] == 0

    assert _voucher(admin_client, voucher)["status"] == "needs_review"
    assert _reextract_events() == []


def test_a_document_whose_key_was_rejected_is_read_with_the_new_key(
    admin_client: TestClient, company_id: int, ai
):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    ai["reply"] = ExtractionError(
        extractor.key_rejected_message(catalog.get("gemini")), retryable=False
    )
    voucher = flows._voucher(admin_client, flows._upload_invoice(admin_client, company_id)["id"])
    assert "Gemini API key was rejected" in voucher["extraction_error"]
    ai["reply"] = None

    assert _put(admin_client, ai_api_key=GOOGLE_AQ_KEY)["requeued"] == 1
    flows._process_all()

    assert _voucher(admin_client, voucher)["source"] == "ai"
    assert ai["built"][-1].key == GOOGLE_AQ_KEY


def test_scans_a_text_only_service_could_not_read_wait_for_another_service(
    admin_client: TestClient, company_id: int
):
    _put(admin_client, ai_api_key=DEEPSEEK_KEY)
    resp = admin_client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", ("bill.png", samples.png(), "image/png"))],
    )
    assert resp.status_code == 200, resp.text
    flows._process_all()
    voucher = flows._voucher(admin_client, resp.json()["documents"][0]["id"])
    assert "DeepSeek can only read text" in voucher["extraction_error"]

    # Still DeepSeek: reading again would fail the same way.
    assert _put(admin_client, ai_model="deepseek-reasoner")["requeued"] == 0

    assert _put(admin_client, ai_api_key=GEMINI_KEY)["requeued"] == 1
    assert _voucher(admin_client, voucher)["status"] == "pending"


def test_nothing_is_queued_while_ai_reading_is_still_not_ready(
    admin_client: TestClient, company_id: int, monkeypatch
):
    voucher = _skipped_voucher(admin_client, company_id)

    assert _put(admin_client, ai_provider="openai")["requeued"] == 0
    assert _put(admin_client, ai_provider="custom")["requeued"] == 0
    assert _put(admin_client, ai_base_url="http://10.0.0.9:8000/v1")["requeued"] == 0  # no model
    assert _put(admin_client, ai_api_key="")["requeued"] == 0

    assert _voucher(admin_client, voucher)["status"] == "needs_review"
    assert _reextract_events() == []

    # Ready through the environment: only a change to the AI settings queues it.
    _env(monkeypatch, openai_api_key=ENV_OPENAI_KEY)
    assert _put(admin_client, tally_url="http://192.168.1.20:9000")["requeued"] == 0
    assert _put(admin_client, ai_provider="openai")["requeued"] == 1


# -- the audit trail ---------------------------------------------------------------------


def test_changes_are_audited_by_name_and_read_well(admin_client: TestClient, monkeypatch):
    _put(admin_client, ai_api_key=GEMINI_KEY)
    _put(admin_client, ai_model="gemini-2.5-flash")
    _put(admin_client, ai_api_key=OPENROUTER_KEY, ai_model="openai/gpt-5")
    _put(admin_client, ai_provider="gemini", ai_api_key="")

    assert _changed() == [
        ["ai_provider", "gemini_api_key"],
        ["gemini_model"],
        ["ai_provider", "openrouter_api_key", "openrouter_model"],
        ["gemini_api_key"],
    ]
    resp = admin_client.get("/api/activity", params={"action_prefix": "settings."})
    summaries = [i["summary"] for i in reversed(resp.json()["items"])]
    assert summaries == [
        "Admin changed the AI service and Gemini API key in the office settings.",
        "Admin changed the Gemini model in the office settings.",
        "Admin changed the AI service, OpenRouter API key and OpenRouter model in the office "
        "settings.",
        "Admin changed the Gemini API key in the office settings.",
    ]
    assert [i["data"] for i in resp.json()["items"]][0] == {"changed": ["gemini_api_key"]}
    assert not _leaks(resp.text)
    with SessionLocal() as db:
        everything = json.dumps([e.data for e in db.scalars(select(AuditEvent))])
    assert not _leaks(everything)
