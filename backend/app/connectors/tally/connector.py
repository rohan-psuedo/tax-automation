import re
from datetime import date

import httpx

from app.connectors.base import (
    ConnectionStatus,
    ConnectorError,
    ExternalCompany,
    ExternalGroup,
    ExternalLedger,
    PostResult,
)
from app.connectors.tally import xml_builder, xml_parser
from app.connectors.tally.client import TallyHttpClient
from app.schemas.canonical import CanonicalTransaction, ProposedLedger

GST_START = date(2017, 7, 1)

_DATE_HINT = (
    "Check that the date falls within the company's books period. In Educational mode, "
    "TallyPrime only accepts vouchers dated the 1st, 2nd or 31st of a month."
)
# Tally's wording for some rejections, and what to do about them.
_HINTS = [
    # Educational mode refuses other days with this misleading message.
    (re.compile(r"voucher date is missing"), _DATE_HINT),
    (
        re.compile(r"^ledger '.*' does not exist"),
        "Sync ledgers from Tally, then check the ledgers on this entry.",
    ),
]


def explain(result: xml_parser.ImportResult) -> list[str]:
    """Why Tally refused an import, in words a person can act on."""
    if result.success:
        return []
    if result.unknown_request:
        reply = " ".join(result.messages) or "nothing"
        return [
            f'Tally did not understand the request (it replied "{reply}"). Check that the '
            "Tally address in Settings is TallyPrime's XML port."
        ]
    messages = []
    for message in result.messages:
        hint = next((h for p, h in _HINTS if p.search(message.casefold())), None)
        if hint:
            message = message.rstrip()
            message = f"{message}{'' if message.endswith(('.', '!', '?')) else '.'} {hint}"
        messages.append(message)
    if messages:
        return messages
    if result.errors or result.exceptions:
        return [f"Tally rejected it without giving a reason. {_DATE_HINT}"]
    if result.ignored:
        return ["Tally ignored it without giving a reason."]
    return ["Tally did not create anything and gave no reason."]


class TallyConnector:
    """AccountingConnector implementation for TallyPrime / Tally.ERP 9 over HTTP-XML."""

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = TallyHttpClient(url, timeout=timeout, transport=transport)

    def test_connection(self) -> ConnectionStatus:
        try:
            banner = self.http.ping()
        except ConnectorError as exc:
            return ConnectionStatus(ok=False, detail=str(exc))
        try:
            text = xml_parser.parse(banner.encode()).text or banner
        except ConnectorError:
            text = banner
        return ConnectionStatus(ok=True, detail=text.strip() or "Connected", version=None)

    def list_companies(self) -> list[ExternalCompany]:
        return xml_parser.parse_companies(self.http.post(xml_builder.companies_request()))

    def fetch_ledgers(self, company: str) -> list[ExternalLedger]:
        return xml_parser.parse_ledgers(self.http.post(xml_builder.ledgers_request(company)))

    def fetch_groups(self, company: str) -> list[ExternalGroup]:
        return xml_parser.parse_groups(self.http.post(xml_builder.groups_request(company)))

    def _import(self, payload: bytes) -> PostResult:
        raw = self.http.post(payload)
        result = xml_parser.parse_import_result(raw)
        return PostResult(
            success=result.success,
            created=result.created,
            altered=result.altered,
            errors=explain(result),
            external_id=result.last_voucher_id,
            request_payload=payload.decode("utf-8"),
            response_payload=xml_parser.decode(raw),
        )

    def _registrations_from(self, company: str) -> date:
        """When a new ledger's GST and address details start to apply: from the first day of
        the books (no voucher can be dated earlier), but not before GST began."""
        key = company.strip().casefold()
        books_from = next(
            (c.books_from for c in self.list_companies() if c.name.strip().casefold() == key),
            None,
        )
        return max(books_from, GST_START) if books_from else GST_START

    def create_ledger(self, company: str, ledger: ProposedLedger) -> PostResult:
        since = self._registrations_from(company)
        return self._import(xml_builder.create_ledgers_request(company, [ledger], since))

    def post_transaction(self, company: str, tx: CanonicalTransaction) -> PostResult:
        return self._import(xml_builder.voucher_request(company, tx))
