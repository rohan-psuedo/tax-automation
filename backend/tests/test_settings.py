"""Office settings: the Settings API, effective settings (saved row over environment), the
encrypted API key, and the AI / Tally connection checks. No network calls are made."""

import json
import logging
import types
from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import OperationalError

from app import security_box
from app.config import get_settings
from app.connectors.base import ConnectionStatus, ConnectorError
from app.db import SessionLocal, engine
from app.extraction import extractor
from app.main import app
from app.models import AppSetting, AuditEvent, Company, User
from app.services import app_settings
from app.services.connectors import connector_url, get_connector_factory
from tests.test_extraction import FakeClient, _response

ENV_URL = "http://127.0.0.1:9000"
KEY = "sk-ant-api03-" + "Zq8Xv3Lm" * 11 + "4f2a"
ENV_KEY = "sk-ant-api03-" + "Env0Key9" * 11 + "e9d1"
ADMIN = {"email": "admin@ca.test", "password": "s3cret-pass"}  # as in conftest


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch):
    """The environment's settings, fixed so the tests don't depend on this machine's .env."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tally_url", ENV_URL)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "claude_model", "claude-opus-5-5")
    monkeypatch.setattr(settings, "claude_effort", "medium")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app_settings.invalidate()
    return settings


def _set_env_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(get_settings(), "anthropic_api_key", key)
    app_settings.invalidate()


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


def _put(client: TestClient, **body) -> dict:
    resp = client.put("/api/settings", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows() -> dict[str, AppSetting]:
    with SessionLocal() as db:
        return {row.key: row for row in db.scalars(select(AppSetting))}


def _save_row(key: str, value: object) -> None:
    with SessionLocal() as db:
        db.merge(AppSetting(key=key, value=value))
        db.commit()


def _save_raw(key: str, stored: str) -> None:
    """Stores text as typed into a database browser, without the JSON quoting the app adds."""
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO app_settings (key, value, updated_at) "
                "VALUES (:key, :value, CURRENT_TIMESTAMP)"
            ),
            {"key": key, "value": stored},
        )


def _fail_next_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next read of the saved settings fails, as with a locked SQLite database."""
    real = app_settings.SessionLocal
    failed: list[bool] = []

    def session_factory():
        if not failed:
            failed.append(True)
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return real()

    monkeypatch.setattr(app_settings, "SessionLocal", session_factory)


def _extract(tmp_path: Path) -> extractor.ExtractionOutcome:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    return extractor.extract_invoice(
        kind="pdf",
        file_path=pdf,
        page_images=[],
        text=None,
        company_name="Demo Traders",
        company_gstin=None,
    )


def _settings_events() -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "settings.updated")
                .order_by(AuditEvent.id)
            )
        )


def _admin_id() -> int:
    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.email == ADMIN["email"]))


# -- access ------------------------------------------------------------------------------

ENDPOINTS = [
    ("get", "/api/settings", None),
    ("put", "/api/settings", {"claude_effort": "high"}),
    ("post", "/api/settings/test-ai", None),
    ("post", "/api/settings/test-tally", None),
]


@pytest.mark.parametrize("role", ["reviewer", "preparer"])
def test_settings_are_for_administrators_only(admin_client: TestClient, role: str):
    _login_as(admin_client, role)

    for method, path, body in ENDPOINTS:
        resp = admin_client.request(method, path, json=body)
        assert resp.status_code == 403, (method, path, resp.text)
    assert _rows() == {}


def test_settings_need_a_login(client: TestClient):
    for method, path, body in ENDPOINTS:
        assert client.request(method, path, json=body).status_code == 401


# -- reading -----------------------------------------------------------------------------


def test_defaults_come_from_the_environment(admin_client: TestClient):
    body = admin_client.get("/api/settings").json()
    services = body["ai"].pop("services")

    assert body == {
        "tally_url": ENV_URL,
        "tally_url_source": "env",
        "ai": {
            "provider": "anthropic",
            "configured": False,
            "source": None,
            "key_hint": None,
            "model": "claude-opus-5-5",
            "effort": "medium",
            "models": ["claude-opus-5-5", "claude-sonnet-5-5"],
            "efforts": ["low", "medium", "high", "xhigh", "max"],
        },
        "requeued": 0,
    }
    assert [s["id"] for s in services] == [
        "anthropic",
        "gemini",
        "openai",
        "openrouter",
        "groq",
        "xai",
        "deepseek",
        "mistral",
        "custom",
    ]
    assert not any(s["configured"] for s in services)


def test_environment_key_is_reported_by_its_hint_only(admin_client: TestClient, monkeypatch):
    _set_env_key(monkeypatch, ENV_KEY)

    resp = admin_client.get("/api/settings")

    ai = resp.json()["ai"]
    assert ai["configured"] is True and ai["source"] == "env"
    assert ai["key_hint"] == "sk-ant-…e9d1"
    assert ENV_KEY not in resp.text


def test_key_in_the_process_environment_counts_too(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", f" {ENV_KEY} ")
    app_settings.invalidate()

    effective = app_settings.current()

    assert effective.anthropic_api_key == ENV_KEY and effective.api_key_source == "env"


def test_model_configured_in_the_environment_stays_selectable(
    admin_client: TestClient, environment, monkeypatch
):
    monkeypatch.setattr(environment, "claude_model", "claude-opus-5")
    app_settings.invalidate()

    ai = admin_client.get("/api/settings").json()["ai"]
    assert ai["model"] == "claude-opus-5"
    assert ai["models"] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5"]

    assert (
        _put(admin_client, claude_model="claude-sonnet-5-5")["ai"]["model"] == "claude-sonnet-5-5"
    )
    assert _put(admin_client, claude_model="claude-opus-5")["ai"]["model"] == "claude-opus-5"


# -- updating ----------------------------------------------------------------------------


def test_update_tally_url(admin_client: TestClient):
    body = _put(admin_client, tally_url="http://192.168.1.20:9000")

    assert body["tally_url"] == "http://192.168.1.20:9000"
    assert body["tally_url_source"] == "settings"
    assert admin_client.get("/api/settings").json()["tally_url"] == "http://192.168.1.20:9000"
    row = _rows()["tally_url"]
    assert row.value == "http://192.168.1.20:9000" and row.updated_by == _admin_id()
    events = _settings_events()
    assert len(events) == 1
    assert events[0].entity_type == "settings" and events[0].actor_id == _admin_id()
    assert events[0].data == {"changed": ["tally_url"]}


def test_update_model_and_effort(admin_client: TestClient):
    assert _put(admin_client, claude_model="claude-sonnet-5-5")["ai"]["model"] == (
        "claude-sonnet-5-5"
    )
    ai = _put(admin_client, claude_effort="xhigh")["ai"]

    assert ai["model"] == "claude-sonnet-5-5" and ai["effort"] == "xhigh"
    effective = app_settings.current()
    assert (effective.claude_model, effective.claude_effort) == ("claude-sonnet-5-5", "xhigh")
    assert [e.data for e in _settings_events()] == [
        {"changed": ["claude_model"]},
        {"changed": ["claude_effort"]},
    ]


def test_only_the_fields_sent_change(admin_client: TestClient):
    _put(admin_client, tally_url="http://tally.office:9000", claude_effort="high")

    body = _put(admin_client, claude_model="claude-sonnet-5-5", tally_url=None)

    assert body["tally_url"] == "http://tally.office:9000"
    assert body["ai"]["effort"] == "high" and body["ai"]["model"] == "claude-sonnet-5-5"
    assert [e.data for e in _settings_events()] == [
        {"changed": ["claude_effort", "tally_url"]},
        {"changed": ["claude_model"]},
    ]


def test_saving_the_same_values_records_nothing(admin_client: TestClient):
    _put(admin_client, tally_url="http://tally.office:9000", anthropic_api_key=KEY)

    _put(admin_client, tally_url="http://tally.office:9000", anthropic_api_key=KEY)
    _put(admin_client)

    assert len(_settings_events()) == 1


def test_api_key_is_encrypted_at_rest_and_never_shown(admin_client: TestClient, caplog):
    caplog.set_level(logging.DEBUG)

    resp = admin_client.put("/api/settings", json={"anthropic_api_key": f"  {KEY}\n"})

    assert resp.status_code == 200, resp.text
    ai = resp.json()["ai"]
    assert ai["configured"] is True and ai["source"] == "settings"
    assert ai["key_hint"] == "sk-ant-…4f2a"
    stored = _rows()["anthropic_api_key"].value
    assert isinstance(stored, str) and KEY not in stored and KEY[13:40] not in stored
    assert security_box.decrypt(stored) == KEY
    assert app_settings.current().anthropic_api_key == KEY

    texts = [resp.text, admin_client.get("/api/settings").text, caplog.text]
    texts += [json.dumps(e.data) for e in _settings_events()]
    texts.append(repr(app_settings.current()))
    assert all(KEY not in text and KEY[13:40] not in text for text in texts)
    assert _settings_events()[0].data == {"changed": ["anthropic_api_key"]}


def test_saved_key_wins_and_removing_it_restores_the_environment_key(
    admin_client: TestClient, monkeypatch
):
    _set_env_key(monkeypatch, ENV_KEY)

    saved = _put(admin_client, anthropic_api_key=KEY)["ai"]
    assert (saved["source"], saved["key_hint"]) == ("settings", "sk-ant-…4f2a")
    assert app_settings.current().anthropic_api_key == KEY

    removed = _put(admin_client, anthropic_api_key="")["ai"]
    assert (removed["source"], removed["key_hint"]) == ("env", "sk-ant-…e9d1")
    assert "anthropic_api_key" not in _rows()
    assert app_settings.current().anthropic_api_key == ENV_KEY

    _put(admin_client, anthropic_api_key="")  # nothing left to remove
    assert [e.data for e in _settings_events()] == [{"changed": ["anthropic_api_key"]}] * 2


def test_removing_the_only_key_leaves_ai_unconfigured(admin_client: TestClient):
    _put(admin_client, anthropic_api_key=KEY)

    ai = _put(admin_client, anthropic_api_key="  ")["ai"]

    assert ai["configured"] is False and ai["source"] is None and ai["key_hint"] is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"claude_model": "gpt-4o"}, "Choose one of: claude-opus-5-5, claude-sonnet-5-5."),
        ({"claude_effort": "extreme"}, "Choose one of: low, medium, high, xhigh, max."),
        ({"tally_url": "ftp://192.168.1.20:9000"}, None),
        ({"tally_url": "tally on the server"}, None),
        ({"tally_url": "http://192.168.1.20:9000/xml"}, None),
        ({"tally_url": ""}, None),
        ({"anthropic_api_key": "sk-ant-api03 Zq8Xv3Lm 4f2a"}, "spaces or line breaks"),
        ({"anthropic_api_key": f"ANTHROPIC_API_KEY={KEY}" + "#" * 400}, None),
        ({"tally_url": "http://192.168.1.20:900O"}, "is not a valid Tally address"),
        ({"tally_url": "http://:9000"}, "is not a valid Tally address"),
        ({"tally_url": "http://192.168.1.20:99999"}, "is not a valid Tally address"),
        ({"tally_url": "http://192.168.1.20:0"}, "is not a valid Tally address"),
        ({"tally_url": "http://tally:"}, "is not a valid Tally address"),
        ({"tally_url": "http://admin@192.168.1.20:9000"}, "is not a valid Tally address"),
        ({"claude_effort": "high", "claude_model": "claude-3"}, "not an available model"),
    ],
    ids=[
        "model",
        "effort",
        "url-scheme",
        "url-text",
        "url-path",
        "url-empty",
        "key-spaces",
        "key-too-long",
        "url-port-letter",
        "url-no-host",
        "url-port-too-high",
        "url-port-zero",
        "url-empty-port",
        "url-user",
        "nothing-saved-when-one-field-is-wrong",
    ],
)
def test_invalid_values_are_rejected(admin_client: TestClient, body: dict, expected: str | None):
    resp = admin_client.put("/api/settings", json=body)

    assert resp.status_code == 422, resp.text
    if expected:
        assert expected in resp.json()["detail"]
    assert "Zq8Xv3Lm" not in resp.text
    assert _rows() == {} and _settings_events() == []


def test_too_long_key_is_rejected_without_echoing_it(admin_client: TestClient):
    pasted = f"ANTHROPIC_API_KEY={KEY}\n" + "#" * 400

    resp = admin_client.put("/api/settings", json={"anthropic_api_key": pasted})

    assert resp.status_code == 422
    error = resp.json()["detail"][0]
    assert error["loc"] == ["body", "anthropic_api_key"] and "500 characters" in error["msg"]
    assert "input" not in error and KEY[13:40] not in resp.text


# -- effective settings ------------------------------------------------------------------


def test_unreadable_saved_key_falls_back_to_the_environment(
    admin_client: TestClient, environment, monkeypatch, caplog
):
    original_secret = environment.secret_key
    monkeypatch.setattr(environment, "secret_key", "the-previous-installation-secret")
    _save_row("anthropic_api_key", security_box.encrypt(KEY))
    monkeypatch.setattr(environment, "secret_key", original_secret)
    _set_env_key(monkeypatch, ENV_KEY)
    monkeypatch.setattr(app_settings, "CACHE_SECONDS", 0)
    caplog.set_level(logging.WARNING, logger=app_settings.__name__)

    for _ in range(3):
        effective = app_settings.current()

    assert effective.anthropic_api_key == ENV_KEY and effective.api_key_source == "env"
    warnings = [r for r in caplog.records if r.name == app_settings.__name__]
    assert len(warnings) == 1 and "can't be decrypted" in warnings[0].getMessage()
    assert KEY not in caplog.text and ENV_KEY not in caplog.text
    ai = admin_client.get("/api/settings").json()["ai"]
    assert ai["source"] == "env" and ai["key_hint"] == "sk-ant-…e9d1"


@pytest.mark.parametrize("token", ["not-a-fernet-token", 12345, {"key": KEY}, "gAAAAAé"])
def test_corrupted_saved_key_counts_as_not_set(token: object):
    _save_row("anthropic_api_key", token)

    effective = app_settings.current()

    assert effective.anthropic_api_key is None and effective.api_key_source is None


def test_corrupted_saved_values_fall_back_to_the_environment():
    _save_row("tally_url", 9000)
    _save_row("claude_model", "gpt-4o")
    _save_row("claude_effort", ["high"])

    effective = app_settings.current()

    assert effective.tally_url == ENV_URL and effective.tally_url_source == "env"
    assert effective.claude_model == "claude-opus-5-5"
    assert effective.claude_effort == "medium"


def test_unparseable_saved_values_fall_back_to_the_environment(
    admin_client: TestClient, tmp_path: Path, caplog
):
    caplog.set_level(logging.WARNING, logger=app_settings.__name__)
    _save_raw("tally_url", "http://192.168.1.5:9000")  # typed without JSON quotes
    _save_raw("claude_effort", "high")
    _save_raw("claude_model", "12.5")  # SQLite stores this one as a number
    _save_raw("anthropic_api_key", "gAAAAA-not-json")

    effective = app_settings.current()

    assert (effective.tally_url, effective.tally_url_source) == (ENV_URL, "env")
    assert effective.claude_effort == "medium" and effective.claude_model == "claude-opus-5-5"
    assert effective.anthropic_api_key is None and not effective.saved_unreadable
    assert connector_url(None) == ENV_URL
    assert admin_client.get("/api/settings").json()["tally_url"] == ENV_URL
    with pytest.raises(extractor.ExtractionNotConfigured):
        _extract(tmp_path)
    assert "The saved setting tally_url is not valid" in caplog.text


@pytest.mark.parametrize("new_key", [KEY, ""], ids=["replace-key", "remove-key"])
def test_unparseable_saved_values_can_be_replaced_in_settings(
    admin_client: TestClient, new_key: str
):
    _save_raw("tally_url", "http://192.168.1.5:9000")
    _save_raw("anthropic_api_key", "gAAAAA-not-json")

    assert _put(admin_client, claude_effort="high")["ai"]["effort"] == "high"
    body = _put(admin_client, tally_url="http://192.168.1.5:9000", anthropic_api_key=new_key)

    assert (body["tally_url"], body["tally_url_source"]) == ("http://192.168.1.5:9000", "settings")
    assert body["ai"]["configured"] is bool(new_key)
    rows = _rows()
    assert rows["tally_url"].value == "http://192.168.1.5:9000"
    assert rows["tally_url"].updated_by == _admin_id()
    if new_key:
        assert security_box.decrypt(rows["anthropic_api_key"].value) == KEY
    else:
        assert "anthropic_api_key" not in rows
    assert [e.data for e in _settings_events()] == [
        {"changed": ["claude_effort"]},
        {"changed": ["anthropic_api_key", "tally_url"]},
    ]


def test_saved_tally_url_that_is_not_a_valid_address_is_ignored():
    _save_row("tally_url", "http://192.168.1.20:900O")

    assert app_settings.current().tally_url == ENV_URL


def test_a_failed_read_keeps_the_last_saved_values(admin_client: TestClient, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=app_settings.__name__)
    _put(admin_client, tally_url="http://192.168.1.20:9000", anthropic_api_key=KEY)
    monkeypatch.setattr(app_settings, "CACHE_SECONDS", 0)  # every call reads again
    _fail_next_read(monkeypatch)

    effective = app_settings.current()

    assert effective.tally_url == "http://192.168.1.20:9000"
    assert effective.anthropic_api_key == KEY and not effective.saved_unreadable
    assert "Saved settings could not be read (OperationalError)" in caplog.text
    assert KEY not in caplog.text
    assert app_settings.current().tally_url == "http://192.168.1.20:9000"


def test_a_failed_read_is_not_cached(monkeypatch):
    _save_row("tally_url", "http://192.168.1.20:9000")
    _fail_next_read(monkeypatch)

    first = app_settings.current()
    assert (first.tally_url, first.saved_unreadable) == (ENV_URL, True)

    second = app_settings.current()
    assert (second.tally_url, second.saved_unreadable) == ("http://192.168.1.20:9000", False)
    assert connector_url(None) == "http://192.168.1.20:9000"


def test_settings_are_cached_until_invalidated(monkeypatch):
    assert app_settings.current().tally_url == ENV_URL

    _save_row("tally_url", "http://10.0.0.5:9000")
    assert app_settings.current().tally_url == ENV_URL  # cached

    app_settings.invalidate()
    assert app_settings.current().tally_url == "http://10.0.0.5:9000"

    _save_row("tally_url", "http://10.0.0.6:9000")
    monkeypatch.setattr(app_settings, "CACHE_SECONDS", 0)  # as if 10 seconds had passed
    assert app_settings.current().tally_url == "http://10.0.0.6:9000"


def test_update_takes_effect_immediately(admin_client: TestClient):
    assert app_settings.current().claude_effort == "medium"  # cached now

    _put(admin_client, claude_effort="low")

    assert app_settings.current().claude_effort == "low"


def test_key_hint():
    assert app_settings.key_hint(KEY) == "sk-ant-…4f2a"
    assert app_settings.key_hint("sk-ant-abcd1") == "sk-ant-…bcd1"
    assert app_settings.key_hint("sk-ant-abc1") is None
    assert app_settings.key_hint("") is None
    assert app_settings.key_hint(None) is None


def test_security_box_round_trip(environment, monkeypatch):
    token = security_box.encrypt(KEY)

    assert KEY not in token and security_box.encrypt(KEY) != token
    assert security_box.decrypt(token) == KEY
    for bad in ["", "garbage", token[:-4], "gAAAAAé", None, 42]:
        with pytest.raises(security_box.SecretUnreadable):
            security_box.decrypt(bad)
    monkeypatch.setattr(environment, "secret_key", "another-installation-secret")
    with pytest.raises(security_box.SecretUnreadable):
        security_box.decrypt(token)


# -- integration -------------------------------------------------------------------------


def test_connector_url_falls_back_to_the_office_tally_url(admin_client: TestClient):
    own = Company(name="Own", external_company_name="Own", connector_url="http://10.1.1.1:9000")
    plain = Company(name="Plain", external_company_name="Plain", connector_url=None)
    assert connector_url(None) == ENV_URL and connector_url(plain) == ENV_URL

    _put(admin_client, tally_url="http://192.168.1.20:9000")

    assert connector_url(None) == "http://192.168.1.20:9000"
    assert connector_url(plain) == "http://192.168.1.20:9000"
    assert connector_url(own) == "http://10.1.1.1:9000"
    status = admin_client.get("/api/connectors/tally/status").json()
    assert status["url"] == "http://192.168.1.20:9000" and status["ok"] is True


def test_extraction_uses_the_saved_key_model_and_effort(
    admin_client: TestClient, tmp_path: Path, monkeypatch
):
    _set_env_key(monkeypatch, ENV_KEY)
    _put(admin_client, anthropic_api_key=KEY, claude_model="claude-sonnet-5-5")
    _put(admin_client, claude_effort="high")
    built: list[dict] = []
    fake = FakeClient(_response(model="claude-sonnet-5-5"))

    def fake_anthropic(**kwargs):
        built.append(kwargs)
        return fake

    monkeypatch.setattr(extractor.anthropic, "Anthropic", fake_anthropic)

    outcome = _extract(tmp_path)

    assert outcome.model == "claude-sonnet-5-5"
    assert [b["api_key"] for b in built] == [KEY]
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert fake.calls[0]["output_config"] == {"effort": "high"}


def test_extraction_without_any_key_is_not_configured(tmp_path: Path):
    with pytest.raises(extractor.ExtractionNotConfigured):
        _extract(tmp_path)


def test_extraction_is_retried_when_the_saved_settings_cannot_be_read(tmp_path: Path, monkeypatch):
    """A key saved only in Settings must not look "not configured" because of one failed
    read: that would switch the document to manual entry for good."""
    _save_row("anthropic_api_key", security_box.encrypt(KEY))
    _fail_next_read(monkeypatch)

    with pytest.raises(extractor.ExtractionError) as failed:
        _extract(tmp_path)

    assert failed.value.retryable
    assert not isinstance(failed.value, extractor.ExtractionNotConfigured)
    assert app_settings.current().anthropic_api_key == KEY


# -- test-ai -----------------------------------------------------------------------------

_REQUEST = httpx2.Request("GET", "https://api.anthropic.com/v1/models/claude-opus-5-5")


def _status_error(cls: type, status: int) -> Exception:
    # The body repeats the key, as some proxies do; it must never reach the response.
    body = {"type": "error", "error": {"type": "error", "message": f"bad key {KEY}"}}
    return cls(
        f"Error code: {status}", response=httpx2.Response(status, request=_REQUEST), body=body
    )


@pytest.fixture
def fake_sdk(monkeypatch):
    """Replaces anthropic.Anthropic for the AI check; state["error"] is raised by retrieve."""
    state: dict = {"error": None, "built": [], "retrieved": [], "closed": 0}

    class FakeAnthropic:
        def __init__(self, **kwargs) -> None:
            state["built"].append(kwargs)
            self.models = types.SimpleNamespace(retrieve=self._retrieve)

        def _retrieve(self, model_id: str):
            state["retrieved"].append(model_id)
            if state["error"] is not None:
                raise state["error"]
            return types.SimpleNamespace(id=model_id, type="model")

        def close(self) -> None:
            state["closed"] += 1

    monkeypatch.setattr(extractor.anthropic, "Anthropic", FakeAnthropic)
    return state


def test_ai_check_without_a_key(admin_client: TestClient, fake_sdk):
    body = admin_client.post("/api/settings/test-ai").json()

    assert body["ok"] is False and "No Anthropic API key is set" in body["detail"]
    assert fake_sdk["built"] == []


def test_ai_check_success(admin_client: TestClient, fake_sdk, caplog):
    caplog.set_level(logging.DEBUG)
    _put(admin_client, anthropic_api_key=KEY, claude_model="claude-sonnet-5-5")

    resp = admin_client.post("/api/settings/test-ai")

    assert resp.json() == {"ok": True, "detail": "Connected. claude-sonnet-5-5 is available."}
    assert fake_sdk["built"] == [{"api_key": KEY, "timeout": 20, "max_retries": 0}]
    assert fake_sdk["retrieved"] == ["claude-sonnet-5-5"] and fake_sdk["closed"] == 1
    assert KEY not in resp.text and KEY not in caplog.text


def test_ai_check_uses_the_environment_key(admin_client: TestClient, fake_sdk, monkeypatch):
    _set_env_key(monkeypatch, ENV_KEY)

    assert admin_client.post("/api/settings/test-ai").json()["ok"] is True
    assert fake_sdk["built"][0]["api_key"] == ENV_KEY


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "The key was rejected"),
        (_status_error(anthropic.PermissionDeniedError, 403), "The key was rejected"),
        (
            _status_error(anthropic.NotFoundError, 404),
            "claude-opus-5-5 is not available to this key",
        ),
        (anthropic.APIConnectionError(request=_REQUEST), "Could not reach the Anthropic API"),
        (anthropic.APITimeoutError(request=_REQUEST), "Could not reach the Anthropic API"),
        (_status_error(anthropic.RateLimitError, 429), "returned an error (HTTP 429)"),
        (_status_error(anthropic.InternalServerError, 500), "returned an error (HTTP 500)"),
    ],
    ids=["auth", "permission", "not-found", "connection", "timeout", "rate-limit", "server"],
)
def test_ai_check_failures(admin_client: TestClient, fake_sdk, error: Exception, expected: str):
    _put(admin_client, anthropic_api_key=KEY)
    fake_sdk["error"] = error

    resp = admin_client.post("/api/settings/test-ai")

    assert resp.status_code == 200
    assert resp.json()["ok"] is False and expected in resp.json()["detail"]
    assert KEY not in resp.text and fake_sdk["closed"] == 1


@pytest.mark.parametrize(
    ("status", "ok", "expected"),
    [
        (200, True, "Connected. claude-opus-5-5 is available."),
        (401, False, "The key was rejected"),
        (404, False, "is not available to this key"),
    ],
)
def test_ai_check_through_the_real_sdk(
    admin_client: TestClient, monkeypatch, status: int, ok: bool, expected: str
):
    """The real client over a local transport: proves the call is a free model lookup."""
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"message": "no"}})
        body = {
            "id": "claude-opus-5-5",
            "type": "model",
            "display_name": "Claude Opus 5.5",
            "created_at": "2026-06-01T00:00:00Z",
        }
        return httpx2.Response(200, json=body)

    real = anthropic.Anthropic
    built: list[dict] = []

    def local_anthropic(**kwargs):
        built.append(kwargs)
        return real(**kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    monkeypatch.setattr(extractor.anthropic, "Anthropic", local_anthropic)
    _put(admin_client, anthropic_api_key=KEY)

    resp = admin_client.post("/api/settings/test-ai")

    assert resp.json()["ok"] is ok and expected in resp.json()["detail"]
    assert len(sent) == 1
    assert sent[0].method == "GET" and sent[0].url.path == "/v1/models/claude-opus-5-5"
    assert sent[0].headers["x-api-key"] == KEY
    assert built == [{"api_key": KEY, "timeout": 20, "max_retries": 0}]


# -- test-tally --------------------------------------------------------------------------


def _fake_connectors(result: ConnectionStatus | Exception) -> list[str]:
    urls: list[str] = []

    class FakeConnector:
        def test_connection(self) -> ConnectionStatus:
            if isinstance(result, Exception):
                raise result
            return result

    def factory(url: str) -> FakeConnector:
        urls.append(url)
        return FakeConnector()

    app.dependency_overrides[get_connector_factory] = lambda: factory
    return urls


def test_tally_check_against_the_mock_tally(admin_client: TestClient):
    body = admin_client.post("/api/settings/test-tally").json()

    assert body == {"ok": True, "detail": "TallyPrime Server is Running"}


def test_tally_check_uses_the_saved_url(admin_client: TestClient):
    _put(admin_client, tally_url="http://192.168.1.20:9000")
    urls = _fake_connectors(ConnectionStatus(ok=True, detail="TallyPrime Server is Running"))

    body = admin_client.post("/api/settings/test-tally").json()

    assert body == {"ok": True, "detail": "TallyPrime Server is Running"}
    assert urls == ["http://192.168.1.20:9000"]


def test_tally_check_reports_an_unreachable_tally(admin_client: TestClient):
    detail = f"Cannot reach Tally at {ENV_URL}. Make sure TallyPrime is running."
    urls = _fake_connectors(ConnectionStatus(ok=False, detail=detail))

    assert admin_client.post("/api/settings/test-tally").json() == {"ok": False, "detail": detail}
    assert urls == [ENV_URL]


def test_tally_check_reports_an_invalid_address(admin_client: TestClient, environment, monkeypatch):
    monkeypatch.setattr(environment, "tally_url", "http://127.0.0.1:90OO")  # letter O
    app_settings.invalidate()

    resp = admin_client.post("/api/settings/test-tally")

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": False,
        "detail": "The Tally address http://127.0.0.1:90OO is not valid. Save an address like "
        "http://192.168.1.20:9000 above and test again.",
    }


def test_tally_check_reports_a_connector_error(admin_client: TestClient):
    _fake_connectors(ConnectorError("Tally returned HTTP 500"))

    body = admin_client.post("/api/settings/test-tally").json()

    assert body == {"ok": False, "detail": "Tally returned HTTP 500"}
