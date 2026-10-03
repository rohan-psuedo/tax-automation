from fastapi import APIRouter, Depends, HTTPException, status

from app.connectors.base import ConnectorError
from app.deps import get_current_user
from app.models import User
from app.schemas.api import ConnectionStatusOut, ExternalCompanyOut
from app.services.connectors import ConnectorFactory, connector_url, get_connector_factory

# Only the configured Tally address is ever contacted. These endpoints deliberately take no
# address from the caller: that would let any logged-in user make the server connect to
# arbitrary hosts. A new address is saved and tested on the (admin-only) Settings screen.
router = APIRouter(prefix="/api/connectors/tally", tags=["connectors"])


@router.get("/status", response_model=ConnectionStatusOut)
def tally_status(
    _: User = Depends(get_current_user),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ConnectionStatusOut:
    target = connector_url(None)
    status_ = factory(target).test_connection()
    return ConnectionStatusOut(ok=status_.ok, detail=status_.detail, url=target)


@router.get("/companies", response_model=list[ExternalCompanyOut])
def tally_companies(
    _: User = Depends(get_current_user),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> list[ExternalCompanyOut]:
    try:
        companies = factory(connector_url(None)).list_companies()
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return [ExternalCompanyOut(name=c.name, state=c.state, gstin=c.gstin) for c in companies]
