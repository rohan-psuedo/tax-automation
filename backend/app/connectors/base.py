"""Connector contract. The AI and accounting layers depend only on this module,
never on a specific accounting system."""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.schemas.canonical import CanonicalTransaction, ProposedLedger


class ConnectorError(Exception):
    """The accounting system could not be reached or returned an unusable response."""


@dataclass
class ConnectionStatus:
    ok: bool
    detail: str
    version: str | None = None


@dataclass
class ExternalCompany:
    name: str
    external_id: str | None = None
    state: str | None = None
    gstin: str | None = None
    books_from: date | None = None  # first day of the books


@dataclass
class ExternalLedger:
    name: str
    parent: str | None = None
    gstin: str | None = None
    state: str | None = None
    aliases: list[str] = field(default_factory=list)
    external_id: str | None = None


@dataclass
class ExternalGroup:
    name: str
    parent: str | None = None


@dataclass
class PostResult:
    success: bool
    created: int = 0
    altered: int = 0
    errors: list[str] = field(default_factory=list)
    external_id: str | None = None
    request_payload: str = ""
    response_payload: str = ""


class AccountingConnector(Protocol):
    def test_connection(self) -> ConnectionStatus: ...

    def list_companies(self) -> list[ExternalCompany]: ...

    def fetch_ledgers(self, company: str) -> list[ExternalLedger]: ...

    def fetch_groups(self, company: str) -> list[ExternalGroup]: ...

    def create_ledger(self, company: str, ledger: ProposedLedger) -> PostResult: ...

    def post_transaction(self, company: str, tx: CanonicalTransaction) -> PostResult: ...
