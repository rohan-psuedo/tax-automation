"""Office settings (administrators only).

CONTRACT:

GET  /api/settings            -> SettingsOut
PUT  /api/settings            SettingsUpdate -> SettingsOut
    Only fields sent change. anthropic_api_key: non-empty saves it (encrypted), "" removes
    the saved key (the environment key, if any, applies again). claude_model must be one of
    AiSettingsOut.models, claude_effort one of AiSettingsOut.efforts, else 422.
    ai_provider / ai_api_key / ai_model / ai_base_url do the same for any AI service (see
    SettingsUpdate). When the change leaves AI reading ready, documents that were waiting
    for it (never read, nobody typed them in) are queued to be read; SettingsOut.requeued
    says how many.
    Audited as "settings.updated" with the names of the changed rows ("gemini_api_key",
    "ai_provider", ...); the key itself is never logged, returned or written to any log (a
    422 never repeats the submitted values).
POST /api/settings/test-ai    -> CheckResult
    Checks the service in use: its key and model, without spending tokens where it can.
    Always 200: what is missing, the adapter's verdict, or (an adapter bug) a generic error.
GET  /api/settings/ai-models?service=<id> -> AiModelsOut
    The models that service's key can use, as the service lists them; 404 for an unknown
    service. models is [] with a detail when the key or address is missing or listing
    failed (the failure's text is never returned: it can repeat request details).
POST /api/settings/test-tally -> CheckResult
    Tests the effective Tally URL (connector.test_connection()).
"""

import logging
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.db import get_db
from app.deps import require_role
from app.extraction import extractor
from app.extraction import services as catalog
from app.extraction.base import ExtractionError
from app.models import User
from app.schemas.api import (
    AiModelsOut,
    AiServiceOut,
    AiSettingsOut,
    CheckResult,
    SettingsOut,
    SettingsUpdate,
)
from app.services import app_settings, vouchers
from app.services.connectors import ConnectorFactory, get_connector_factory


class _NoEchoRoute(APIRoute):
    """Validation errors without the submitted values: FastAPI's 422 repeats each field's
    input, which for an over-long paste would include the API key."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
                raise RequestValidationError(errors) from None

        return handle


router = APIRouter(prefix="/api/settings", tags=["settings"], route_class=_NoEchoRoute)

log = logging.getLogger(__name__)

# Sending any of these may make AI reading usable, so waiting documents are queued then.
_AI_FIELDS = {
    "anthropic_api_key",
    "claude_model",
    "ai_provider",
    "ai_api_key",
    "ai_model",
    "ai_base_url",
}


def _service_out(svc: catalog.Service, effective: app_settings.Effective) -> AiServiceOut:
    state = effective.service_settings(svc.id)
    return AiServiceOut(
        id=svc.id,
        name=svc.name,
        key_name=svc.key_name,
        key_help=svc.key_help,
        key_optional=svc.key_optional,
        custom_base_url=svc.custom_base_url,
        supports_effort=svc.supports_effort,
        reads_images=svc.reads_images,
        configured=state.ready,
        source=state.key_source,
        key_hint=app_settings.key_hint(state.api_key),
        model=state.model,
        models=app_settings.models_for(svc.id, state.model),
        base_url=state.base_url,
    )


def _out(effective: app_settings.Effective, requeued: int = 0) -> SettingsOut:
    active = effective.ai
    return SettingsOut(
        tally_url=effective.tally_url,
        tally_url_source=effective.tally_url_source,
        ai=AiSettingsOut(
            provider=effective.ai_provider,
            configured=active.ready,
            source=active.key_source,
            key_hint=app_settings.key_hint(active.api_key),
            model=active.model,
            effort=effective.claude_effort,
            models=app_settings.models_for(active.id, active.model),
            efforts=list(app_settings.EFFORTS),
            services=[_service_out(svc, effective) for svc in catalog.SERVICES],
        ),
        requeued=requeued,
    )


@router.get("", response_model=SettingsOut)
def get_office_settings(_: User = Depends(require_role())) -> SettingsOut:
    return _out(app_settings.current())


@router.put("", response_model=SettingsOut)
def update_office_settings(
    body: SettingsUpdate, db: Session = Depends(get_db), admin: User = Depends(require_role())
) -> SettingsOut:
    try:
        effective = app_settings.update(db, body, admin)
    except app_settings.SettingsError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    requeued = 0
    if body.model_fields_set & _AI_FIELDS and effective.ai.ready:
        requeued = vouchers.requeue_waiting_for_ai(db, admin)
    return _out(effective, requeued)


@router.post("/test-ai", response_model=CheckResult)
def check_ai(_: User = Depends(require_role())) -> CheckResult:
    effective = app_settings.current()
    service = catalog.get(effective.ai_provider) or catalog.DEFAULT
    try:
        adapter = extractor.build_adapter(service, effective)
    except ExtractionError:
        state = effective.ai
        if service.custom_base_url and not state.base_url:
            missing = "No address is set for the AI service. Enter it above"
        elif not state.model:
            missing = f"No model is chosen for {service.in_sentence}. Choose one above"
        else:
            missing = f"No {service.key_name} is set. Add a key above"
        return CheckResult(ok=False, detail=f"{missing}, save it, and test again.")
    except Exception as exc:  # noqa: BLE001 - the settings screen must get an answer
        return _unchecked(service, "building the client", exc)
    try:
        result = adapter.check()
    except Exception as exc:  # noqa: BLE001 - check() reports failures; this is a bug
        return _unchecked(service, "checking", exc)
    return CheckResult(ok=result.ok, detail=result.detail)


def _unchecked(service: catalog.Service, step: str, exc: Exception) -> CheckResult:
    # The exception text can echo request details (even the key); only its type is logged.
    log.warning("AI check for %s failed while %s (%s)", service.id, step, type(exc).__name__)
    return CheckResult(
        ok=False,
        detail=f"The connection to {service.in_sentence} could not be checked because of an "
        "error in this app. Try again; if it keeps failing, send the log file "
        "(backend\\data\\logs\\app.log) along when you ask for help.",
    )


@router.get("/ai-models", response_model=AiModelsOut)
def ai_models(
    service: str = Query(max_length=40), _: User = Depends(require_role())
) -> AiModelsOut:
    svc = catalog.get(service)
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such AI service")
    effective = app_settings.current()
    state = effective.service_settings(svc.id)
    if svc.custom_base_url and not state.base_url:
        return AiModelsOut(
            models=[], detail="Save the service's address to see the models it offers."
        )
    try:
        adapter = extractor.build_adapter(svc, _with_some_model(effective, state))
    except ExtractionError:
        return AiModelsOut(models=[], detail="Save a key for this service to see its models.")
    except Exception as exc:  # noqa: BLE001 - any failure only means no list to offer
        return _no_list(svc, exc)
    try:
        return AiModelsOut(models=adapter.list_models())
    except Exception as exc:  # noqa: BLE001
        return _no_list(svc, exc)


def _no_list(svc: catalog.Service, exc: Exception) -> AiModelsOut:
    # The exception text can echo request details; only its type is logged.
    log.info("Listing models for %s failed (%s)", svc.id, exc.__class__.__name__)
    return AiModelsOut(
        models=[], detail=f"{svc.short} did not return its list of models. Type the name."
    )


def _with_some_model(
    effective: app_settings.Effective, state: app_settings.ServiceSettings
) -> app_settings.Effective:
    """Listing models needs none, but an adapter is only built with one: the list is what an
    office with the "Other" service looks at to choose its first model."""
    if state.model:
        return effective
    others = {**effective.other_services, state.id: replace(state, model="model-to-choose")}
    return replace(effective, other_services=others)


@router.post("/test-tally", response_model=CheckResult)
def check_tally(
    _: User = Depends(require_role()),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> CheckResult:
    url = app_settings.current().tally_url
    try:
        result = factory(url).test_connection()
    except ConnectorError as exc:
        return CheckResult(ok=False, detail=str(exc))
    except httpx.InvalidURL:  # only an address from backend/.env can be malformed
        return CheckResult(
            ok=False,
            detail=f"The Tally address {url} is not valid. Save an address like "
            "http://192.168.1.20:9000 above and test again.",
        )
    return CheckResult(ok=result.ok, detail=result.detail)
