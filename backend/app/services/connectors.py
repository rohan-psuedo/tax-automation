from collections.abc import Callable

from app.config import get_settings
from app.connectors.base import AccountingConnector
from app.connectors.tally import TallyConnector
from app.models import Company
from app.services import app_settings

ConnectorFactory = Callable[[str], AccountingConnector]


def default_connector_factory(url: str) -> AccountingConnector:
    return TallyConnector(url, timeout=get_settings().tally_timeout_seconds)


def get_connector_factory() -> ConnectorFactory:
    """FastAPI dependency; tests override it to route to the mock Tally."""
    return default_connector_factory


def connector_url(company: Company | None) -> str:
    """The company's own Tally address, else the office's (Settings, then the environment)."""
    return (company.connector_url if company else None) or app_settings.current().tally_url
