"""Effective office settings: a saved AppSetting row wins over the environment.

CONTRACT:

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5"]          # offered on the settings screen
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

@dataclass(frozen=True)
class Effective:
    tally_url: str
    tally_url_source: Literal["settings", "env"]
    anthropic_api_key: str | None
    api_key_source: Literal["settings", "env"] | None
    claude_model: str
    claude_effort: str
    saved_unreadable: bool = False  # the database could not be read and no earlier read was
                                    # at hand, so only the environment applied

def current() -> Effective
    Process-wide, cached for up to 10 seconds, read through app.db.SessionLocal; safe to
    call from request handlers and the worker thread. A saved key that can't be decrypted
    counts as not set (and is logged once as a warning, without the key); so does any other
    saved value that isn't valid, even one that isn't JSON at all. When the database can't
    be read, the last values read stand in (else the environment, with saved_unreadable),
    and nothing is cached, so the next call reads again.
def invalidate() -> None                        # drop the cache (called after an update)
def update(db, changes: SettingsUpdate, actor: User) -> Effective   # validates, saves, audits
    Raises SettingsError (a ValueError) with a message for the user; nothing is saved then.
def key_hint(key: str | None) -> str | None     # "sk-ant-…4f2a"

Integration done by module B:
- app.extraction.extractor uses current() for the API key, model and effort.
- app.services.connectors.connector_url(company) falls back to current().tally_url.

AI services (app.extraction.services): Claude keeps its original settings (anthropic_api_key,
claude_model, claude_effort). Every other service has "<id>_api_key" (encrypted) and
"<id>_model"; the "Other (OpenAI-compatible)" service also "custom_base_url". "ai_provider"
names the service in use; without one, AI_PROVIDER from the environment applies, else the
first service that is ready (Claude first, then in catalog order). The environment gives
each service's key (its env_keys, e.g. GEMINI_API_KEY or GOOGLE_API_KEY), AI_MODEL for the
AI_PROVIDER service (Claude uses CLAUDE_MODEL) and AI_BASE_URL for the custom service.
Effective.service_settings(id) gives any service's key, model and address.

update() takes ai_provider / ai_api_key / ai_model / ai_base_url for any service: a key sent
without ai_provider is saved under the service its format points to (catalog.detect), or the
service in use when it can't be told. Saving a key puts its service in use, and so does
ai_provider sent on its own; sent with a model, an address or a key removal, ai_provider only
names the service those belong to (the service in use stays). "" resets:
ai_provider "" goes back to the automatic choice, ai_model "" to the service's default model,
ai_base_url "" removes the custom address, ai_api_key "" removes the saved key. Rejected
values are never repeated in the message, since a key pasted into the wrong field would be.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy import update as update_rows
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import audit, security_box
from app.config import get_settings
from app.db import SessionLocal
from app.extraction import services as catalog
from app.models import AppSetting, User
from app.schemas.api import SettingsUpdate

log = logging.getLogger(__name__)

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CACHE_SECONDS = 10.0

TALLY_URL = "tally_url"
API_KEY = "anthropic_api_key"
MODEL = "claude_model"
EFFORT = "claude_effort"
PROVIDER = "ai_provider"
BASE_URL = "custom_base_url"
_BASE_URL_PATTERN = re.compile(r"https?://[^\s/?#]+(/[^\s?#]*)?")
# A line copied from backend/.env: "OPENAI_API_KEY=sk-...".
_ENV_LINE = re.compile(r"[A-Z][A-Z0-9_]*_(?:KEY|TOKEN)\s*=\s*(?P<value>[^=\s]\S*)")


def key_setting(service_id: str) -> str:
    return f"{service_id}_api_key"  # "anthropic_api_key" for Claude, as before


def model_setting(service_id: str) -> str:
    return MODEL if service_id == catalog.DEFAULT.id else f"{service_id}_model"


_KEY_SETTINGS = {key_setting(svc.id) for svc in catalog.SERVICES}

# Stricter than the schema's pattern, which also lets through typos such as a letter O in
# the port; those would only fail later, on every Tally request.
_TALLY_URL = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(?P<port>\d{1,5}))?/?")
_UNREADABLE = object()  # a stored value that isn't JSON (e.g. typed into a database browser)

Source = Literal["settings", "env"]


class SettingsError(ValueError):
    """A value update() won't save. The message is for the user."""


@dataclass(frozen=True)
class ServiceSettings:
    """The effective key, model and address of one AI service."""

    id: str
    api_key: str | None = field(repr=False)  # never in logs or tracebacks
    key_source: Source | None
    model: str
    base_url: str | None = None

    @property
    def ready(self) -> bool:
        """Has what reading needs: a key (unless optional), a model, an address."""
        service = catalog.get(self.id)
        if service is None or not self.model:
            return False
        if service.custom_base_url and not self.base_url:
            return False
        return bool(self.api_key) or service.key_optional


@dataclass(frozen=True)
class Effective:
    tally_url: str
    tally_url_source: Source
    anthropic_api_key: str | None = field(repr=False)  # never in logs or tracebacks
    api_key_source: Source | None
    claude_model: str
    claude_effort: str
    saved_unreadable: bool = False
    ai_provider: str = catalog.DEFAULT.id
    # Every service but Claude, whose values are the fields above.
    other_services: dict[str, ServiceSettings] = field(default_factory=dict, repr=False)

    def service_settings(self, service_id: str) -> ServiceSettings:
        if service_id == catalog.DEFAULT.id:
            return ServiceSettings(
                service_id, self.anthropic_api_key, self.api_key_source, self.claude_model
            )
        if found := self.other_services.get(service_id):
            return found
        service = catalog.get(service_id)
        return ServiceSettings(
            service_id,
            None,
            None,
            service.default_model if service else "",
            service.base_url if service else None,
        )

    @property
    def ai(self) -> ServiceSettings:
        """The service in use."""
        return self.service_settings(self.ai_provider)


_lock = threading.Lock()
_cache: tuple[float, Effective] | None = None  # the last successful read, and when
_warned: set[str] = set()


def current() -> Effective:
    global _cache
    with _lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        saved = _saved_values()
        if saved is None:
            # Not cached, so the next call reads again. The environment may name another
            # Tally or key than the office saved, so it applies only with nothing better.
            return _cache[1] if _cache is not None else _effective({}, saved_unreadable=True)
        _cache = (now, _effective(saved))
        return _cache[1]


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None
        _warned.clear()


def offered_models() -> list[str]:
    """MODELS, plus the environment's model when it is not one of them, so an office that
    configured another model in backend/.env can keep it."""
    env_model = get_settings().claude_model
    return MODELS if env_model in MODELS else [*MODELS, env_model]


def models_for(service_id: str, current_model: str | None = None) -> list[str]:
    """Models suggested for a service, with the one in use first added when it isn't one."""
    if service_id == catalog.DEFAULT.id:
        return offered_models()
    service = catalog.get(service_id)
    models = list(service.models) if service else []
    if current_model and current_model not in models:
        models.insert(0, current_model)
    return models


def key_hint(key: str | None) -> str | None:
    if key is None or len(key) < 12:
        return None
    return f"{key[:7]}…{key[-4:]}"


def update(db: Session, changes: SettingsUpdate, actor: User) -> Effective:
    """Saves the fields that were sent; a field sent as null is left unchanged. Raises
    SettingsError with a message for the user when a value isn't allowed."""
    wanted = {
        name: getattr(changes, name)
        for name in changes.model_fields_set
        if getattr(changes, name) is not None
    }
    if API_KEY in wanted:
        wanted[API_KEY] = _clean_key(wanted[API_KEY], catalog.DEFAULT)
    _check_url(wanted.get(TALLY_URL))
    _check_choice(wanted.get(MODEL), offered_models(), "model")
    _check_choice(wanted.get(EFFORT), EFFORTS, "effort level")
    wanted.update(_ai_rows(wanted))

    changed = [name for name, value in wanted.items() if _save(db, name, value, actor)]
    if changed:
        audit.record(
            db,
            action="settings.updated",
            entity_type="settings",
            entity_id="office",
            actor_id=actor.id,
            data={"changed": sorted(changed)},
        )
    db.commit()
    invalidate()
    return current()


def _ai_rows(wanted: dict[str, Any]) -> dict[str, Any]:
    """Turns ai_provider/ai_api_key/ai_model/ai_base_url into the rows they are saved as,
    after checking them. Removes those four from wanted."""
    provider = wanted.pop("ai_provider", None)
    key = wanted.pop("ai_api_key", None)
    model = wanted.pop("ai_model", None)
    base_url = wanted.pop("ai_base_url", None)
    if provider is not None:
        provider = provider.strip()
        if provider and catalog.get(provider) is None:
            names = ", ".join(svc.id for svc in catalog.SERVICES)
            raise SettingsError(
                f"That is not an AI service this app knows. Choose one of: {names}."
            )
    if key is not None:
        key = _clean_key(key, catalog.get(provider) or catalog.detect(key))
    target = catalog.get(provider)
    if target is None and key:
        target = catalog.detect(key)  # a pasted key goes where it belongs
    if target is None:
        target = catalog.get(current().ai_provider) or catalog.DEFAULT

    rows: dict[str, Any] = {}
    if key is not None:
        rows[key_setting(target.id)] = key
    if model is not None:
        rows[model_setting(target.id)] = _clean_model(model, target)
    if base_url is not None:
        base_url = _clean_base_url(base_url)
        if base_url and not target.custom_base_url:
            raise SettingsError(f"The address of {target.name} is fixed and can't be changed.")
        if target.custom_base_url:
            rows[BASE_URL] = base_url
    # Saving a key puts its service in use, and so does ai_provider sent on its own ("" goes
    # back to the automatic choice). With a model, an address or a key removal, ai_provider
    # only names the service they belong to: editing a service that isn't in use must not
    # hand the documents being read meanwhile to it.
    if key:
        rows[PROVIDER] = target.id
    elif provider is not None and key is None and model is None and base_url is None:
        rows[PROVIDER] = provider
    return rows


def _clean_model(model: str, service: catalog.Service) -> str:
    """The model name to save; "" goes back to the service's default."""
    model = model.strip()
    if not model:
        return ""
    if service.id == catalog.DEFAULT.id:
        _check_choice(model, offered_models(), "model")
        return model
    # Not repeated in the message, and never saved: shown on the settings screen, a key
    # pasted into the model field would be on view to every administrator.
    if catalog.detect(model) is not None:
        raise SettingsError(
            "That looks like an API key, not a model name. Paste the key in the key field, "
            "and type the model name here."
        )
    if "://" in model:
        raise SettingsError(
            "That looks like an address, not a model name. Enter the address in the address "
            "field, and the model name here."
        )
    if not catalog.MODEL_PATTERN.fullmatch(model):
        example = service.default_model or "llama3.1:8b"
        raise SettingsError(
            f"That is not a model name. Copy the name exactly as {service.in_sentence} lists "
            f"it, without spaces, for example {example}."
        )
    return model


def _clean_base_url(base_url: str) -> str:
    """The service address to save. Pasting the full chat endpoint is a common slip, so
    "/chat/completions" is dropped: the address is the API's base, which ends before it."""
    base_url = base_url.strip().rstrip("/").removesuffix("/chat/completions").rstrip("/")
    if base_url and not _BASE_URL_PATTERN.fullmatch(base_url):
        raise SettingsError(
            "That is not a valid address. Enter the service's API address, starting with "
            "http:// or https://, for example http://192.168.1.30:11434/v1 for Ollama."
        )
    return base_url


# -- reading ---------------------------------------------------------------------------


def _effective(saved: dict[str, Any], *, saved_unreadable: bool = False) -> Effective:
    env = get_settings()
    url = _saved_url(saved.get(TALLY_URL))
    key = _saved_key(saved.get(API_KEY))
    env_key = _env_key(env.anthropic_api_key)
    model = _saved_choice(saved, MODEL, offered_models())
    effort = _saved_choice(saved, EFFORT, EFFORTS)
    others = {
        svc.id: _service_settings(svc, saved, env)
        for svc in catalog.SERVICES
        if svc.id != catalog.DEFAULT.id
    }
    claude = ServiceSettings(
        catalog.DEFAULT.id,
        key or env_key,
        "settings" if key else "env" if env_key else None,
        model or env.claude_model,
    )
    return Effective(
        tally_url=url or env.tally_url,
        tally_url_source="settings" if url else "env",
        anthropic_api_key=claude.api_key,
        api_key_source=claude.key_source,
        claude_model=claude.model,
        claude_effort=effort or env.claude_effort,
        saved_unreadable=saved_unreadable,
        ai_provider=_provider(saved, env, [claude, *others.values()]),
        other_services=others,
    )


def _service_settings(service: catalog.Service, saved: dict[str, Any], env: Any) -> ServiceSettings:
    name = key_setting(service.id)
    key = _saved_key(saved.get(name), name)
    env_key = next(
        (
            value
            for attr in service.env_keys
            if (value := _env_key(getattr(env, attr, None), attr.upper()))
        ),
        None,
    )
    env_model = env.ai_model if _env_provider(env) == service.id else None
    model = _saved_model(saved, service) or (env_model or "").strip() or service.default_model
    base_url = service.base_url
    if service.custom_base_url:
        base_url = _saved_base_url(saved.get(BASE_URL)) or _env_base_url(env)
    return ServiceSettings(
        service.id,
        key or env_key,
        "settings" if key else "env" if env_key else None,
        model,
        base_url,
    )


def _provider(saved: dict[str, Any], env: Any, services: list[ServiceSettings]) -> str:
    """The service chosen in Settings, else in the environment, else the first one ready."""
    chosen = saved.get(PROVIDER)
    if chosen is not None and not (isinstance(chosen, str) and catalog.get(chosen)):
        _warn_invalid(PROVIDER)
        chosen = None
    for candidate in (chosen, _env_provider(env)):
        if catalog.get(candidate):
            return candidate
    return next((svc.id for svc in services if svc.ready), catalog.DEFAULT.id)


def _env_provider(env: Any) -> str | None:
    """AI_PROVIDER as typed in backend/.env ("Gemini", " openai"); None when it names no
    service, which is logged once instead of silently picking another service."""
    value = (env.ai_provider or "").strip().lower()
    if not value:
        return None
    if catalog.get(value) is None:
        names = ", ".join(svc.id for svc in catalog.SERVICES)
        _warn_once(
            "AI_PROVIDER", f"AI_PROVIDER in backend/.env names no AI service; use one of {names}."
        )
        return None
    return value


def _env_base_url(env: Any) -> str | None:
    value = (env.ai_base_url or "").strip().rstrip("/") or None
    if value is not None and not _BASE_URL_PATTERN.fullmatch(value):
        _warn_once(
            "AI_BASE_URL",
            "AI_BASE_URL in backend/.env is not a valid address (it must start with http:// "
            "or https://); it is ignored.",
        )
        return None
    return value


def _saved_model(saved: dict[str, Any], service: catalog.Service) -> str | None:
    value = saved.get(model_setting(service.id))
    if value is None or value == "":
        return None
    if not (isinstance(value, str) and catalog.MODEL_PATTERN.fullmatch(value)):
        _warn_invalid(model_setting(service.id))
        return None
    return value


def _saved_base_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not (isinstance(value, str) and _BASE_URL_PATTERN.fullmatch(value)):
        _warn_invalid(BASE_URL)
        return None
    return value


def _saved_values() -> dict[str, Any] | None:
    """The saved values that could be parsed, or None when the database can't be read."""
    try:
        with SessionLocal() as db:
            keys = db.scalars(select(AppSetting.key)).all()
            values = {key: _stored(db, key) for key in keys}
    except SQLAlchemyError as exc:
        _warn_once(
            "_db",
            f"Saved settings could not be read ({exc.__class__.__name__}); the last settings "
            "read (or else the environment settings) apply until the next try.",
        )
        return None
    _warned.discard("_db")  # so a later failure is logged too
    for key in [key for key, value in values.items() if value is _UNREADABLE]:
        _warn_invalid(key)
        del values[key]
    return values


def _stored(db: Session, key: str) -> Any:
    """One saved value, read on its own so a damaged row can't hide the others."""
    try:
        return db.scalar(select(AppSetting.value).where(AppSetting.key == key))
    except ValueError:  # the JSON column's decoder rejected the stored text
        return _UNREADABLE


def _env_key(configured: str | None, variable: str = "ANTHROPIC_API_KEY") -> str | None:
    return (configured or os.environ.get(variable) or "").strip() or None


def _saved_url(value: Any) -> str | None:
    if value is None:
        return None
    if not (isinstance(value, str) and _valid_url(value)):
        _warn_invalid(TALLY_URL)
        return None
    return value


def _saved_key(token: Any, name: str = API_KEY) -> str | None:
    if token is None:
        return None
    try:
        key = security_box.decrypt(token).strip()
    except security_box.SecretUnreadable:
        service = catalog.get(name.removesuffix("_api_key")) or catalog.DEFAULT
        _warn_once(
            name,
            f"The saved {service.key_name} can't be decrypted (the installation's secret key "
            "may have changed); the environment key applies. Save the key again in Settings.",
        )
        return None
    return key or None


def _saved_choice(saved: dict[str, Any], name: str, allowed: list[str]) -> str | None:
    value = saved.get(name)
    if value is None:
        return None
    if value not in allowed:
        _warn_invalid(name)
        return None
    return value


def _warn_invalid(name: str) -> None:
    _warn_once(name, f"The saved setting {name} is not valid; the environment value applies.")


def _warn_once(topic: str, message: str) -> None:
    if topic not in _warned:
        _warned.add(topic)
        log.warning(message)


# -- writing ---------------------------------------------------------------------------


def _valid_url(url: str) -> bool:
    match = _TALLY_URL.fullmatch(url)
    return match is not None and 0 < int(match["port"] or 80) <= 65535


def _check_url(url: str | None) -> None:
    if url is not None and not _valid_url(url):
        raise SettingsError(
            f'"{url}" is not a valid Tally address. Enter the name or IP address of the '
            "computer running TallyPrime and its port, for example http://192.168.1.20:9000."
        )


def _clean_key(key: str, service: catalog.Service | None) -> str:
    """The key as pasted, without what often comes along with it: surrounding quotes, or the
    variable name of a backend/.env line ("GEMINI_API_KEY=...")."""
    key = key.strip()
    if named := _ENV_LINE.fullmatch(key):
        key = named["value"].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    if any(char.isspace() for char in key):
        where = " from the Anthropic Console" if service is catalog.DEFAULT else ""
        raise SettingsError(
            f"The API key contains spaces or line breaks. Copy the key again{where} and "
            "paste it as a single line."
        )
    return key


def _check_choice(value: str | None, allowed: list[str], what: str) -> None:
    if value is not None and value not in allowed:
        choices = ", ".join(allowed)
        raise SettingsError(f"That is not an available {what}. Choose one of: {choices}.")


def _save(db: Session, name: str, value: str, actor: User) -> bool:
    """Stores one field; True when the saved value changed. An empty key removes the row.
    Rows are written with statements rather than loaded, so a damaged value is replaced
    instead of failing the save."""
    exists = db.scalar(select(AppSetting.key).where(AppSetting.key == name)) is not None
    old = _stored(db, name) if exists else None
    if name in _KEY_SETTINGS:
        if not value:
            if exists:
                db.execute(delete(AppSetting).where(AppSetting.key == name))
            return exists
        if exists and _same_key(old, value):
            return False
        value = security_box.encrypt(value)
    elif value == "" and name != TALLY_URL:
        # An empty model or address goes back to the service's default.
        if exists:
            db.execute(delete(AppSetting).where(AppSetting.key == name))
        return exists
    elif exists and old == value:
        return False
    if exists:
        db.execute(
            update_rows(AppSetting)
            .where(AppSetting.key == name)
            .values(value=value, updated_by=actor.id)
        )
    else:
        db.add(AppSetting(key=name, value=value, updated_by=actor.id))
    return True


def _same_key(token: Any, key: str) -> bool:
    try:
        return security_box.decrypt(token) == key
    except security_box.SecretUnreadable:
        return False
