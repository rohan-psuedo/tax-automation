"""In-memory stand-in for TallyPrime's HTTP-XML server, for development and tests.

It understands the same requests our TallyConnector sends (collection exports for
companies/ledgers/groups, and "Import Data" for masters and vouchers) and enforces the
rules that matter most: the company must exist, parent groups and voucher ledgers must
exist, and voucher amounts must net to zero.

It also copies TallyPrime 7.x's quirks, so the connector is tested against them:
- it answers in the request's encoding, and a UTF-16 body needs charset=utf-16;
- party GST and address details live in dated lists (TallyPrime 3.0+), returned only
  when the FETCH names them;
- ACTION="Create" on a ledger name in use alters that ledger;
- a voucher sent again with the same REMOTEID replaces the first one;
- rejected vouchers count as EXCEPTIONS (ERRORS stays 0), with only the last LINEERROR;
- an import for a company that isn't open lands in the active one;
- in Educational mode, vouchers dated other than the 1st, 2nd or 31st are refused with
  "Voucher date is missing".

Run standalone:  uv run python -m app.devtools.mock_tally --port 9000 [--educational]
"""

import argparse
import codecs
import copy
import threading
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from defusedxml.ElementTree import fromstring

DEMO_COMPANY = "Demo Traders Pvt Ltd"
_PRIMARY = "&#4; Primary"  # how Tally names the root of the group tree
_UNKNOWN_REQUEST = "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"

_STANDARD_GROUPS: dict[str, str | None] = {
    "Capital Account": None,
    "Current Assets": None,
    "Current Liabilities": None,
    "Direct Expenses": None,
    "Direct Incomes": None,
    "Fixed Assets": None,
    "Indirect Expenses": None,
    "Indirect Incomes": None,
    "Purchase Accounts": None,
    "Sales Accounts": None,
    "Bank Accounts": "Current Assets",
    "Cash-in-Hand": "Current Assets",
    "Sundry Debtors": "Current Assets",
    "Duties & Taxes": "Current Liabilities",
    "Sundry Creditors": "Current Liabilities",
}

# A ledger's "gstin" and "state" are the flat fields of Tally.ERP 9 and TallyPrime before
# 3.0; "gst_details" and "mailing" are the dated lists of TallyPrime 3.0+.
_DEMO_LEDGERS: list[dict] = [
    {"name": "Cash", "parent": "Cash-in-Hand"},
    {"name": "HDFC Bank", "parent": "Bank Accounts"},
    {"name": "Purchase", "parent": "Purchase Accounts"},
    {"name": "Sales", "parent": "Sales Accounts"},
    {"name": "Input CGST", "parent": "Duties & Taxes"},
    {"name": "Input SGST", "parent": "Duties & Taxes"},
    {"name": "Input IGST", "parent": "Duties & Taxes"},
    {"name": "Output CGST", "parent": "Duties & Taxes"},
    {"name": "Output SGST", "parent": "Duties & Taxes"},
    {"name": "Output IGST", "parent": "Duties & Taxes"},
    {"name": "Round Off", "parent": "Indirect Expenses"},
    {"name": "Office Expenses", "parent": "Indirect Expenses"},
    {
        # Made in TallyPrime 3.0+: the details are only in the dated lists.
        "name": "Sharma Electronics",
        "parent": "Sundry Creditors",
        "gst_details": [
            {"since": "20240401", "type": "Regular", "state": "Karnataka", "gstin": None},
            {
                "since": "20240701",
                "type": "Regular",
                "state": "Karnataka",
                "gstin": "29ABCDE1234F1ZW",
            },
        ],
        "mailing": [{"since": "20240401", "state": "Karnataka", "country": "India"}],
        "aliases": ["Sharma Elec"],
    },
    {
        # Carried over from Tally.ERP 9: only the flat fields.
        "name": "Gupta Retail",
        "parent": "Sundry Debtors",
        "gstin": "27PQRSX6789K1ZS",
        "state": "Maharashtra",
    },
]


class MockTally:
    def __init__(self, educational: bool = False) -> None:
        self.lock = threading.Lock()
        self.educational = educational
        self.companies: dict[str, dict] = {}  # the open companies; the first is the active one
        self.add_company(DEMO_COMPANY, state="Karnataka", gstin="29AAACD1234A1ZD")

    def add_company(
        self, name: str, state: str, gstin: str | None = None, books_from: str = "20240401"
    ) -> None:
        self.companies[name] = {
            "state": state,
            "gstin": gstin,
            "books_from": books_from,
            "guid": f"guid-company-{len(self.companies) + 1}",
            "groups": dict(_STANDARD_GROUPS),
            "ledgers": {d["name"]: copy.deepcopy(d) for d in _DEMO_LEDGERS},
            "vouchers": [],
        }

    # -- request dispatch -------------------------------------------------------------

    def handle(self, payload: bytes, content_type: str = "text/xml; charset=utf-8") -> bytes:
        utf16 = "utf-16" in content_type.lower()
        try:
            if utf16:
                bom = payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                text = payload.decode("utf-16" if bom else "utf-16-le")
            else:
                text = payload.decode("utf-8")
            root = fromstring(text)
        except Exception:  # noqa: BLE001 - Tally gives one answer to anything it can't read
            return _encode(_UNKNOWN_REQUEST, utf16)
        request = (root.findtext("HEADER/TALLYREQUEST") or "").strip()
        with self.lock:
            if request == "Export":
                return _encode(self._export(root), utf16)
            if request == "Import Data":
                return _encode(self._import(root), utf16)
        return _encode(_UNKNOWN_REQUEST, utf16)

    def _company(self, root: ET.Element) -> tuple[str | None, dict | None]:
        name = root.findtext(".//SVCURRENTCOMPANY")
        if name is None:
            return None, None
        return name, self.companies.get(name)

    def _export(self, root: ET.Element) -> str:
        obj_type = (root.findtext(".//COLLECTION/TYPE") or "").strip()
        fetch = {f.strip().upper() for f in (root.findtext(".//COLLECTION/FETCH") or "").split(",")}
        if obj_type == "Company":
            body = "".join(
                f'<COMPANY NAME="{_esc(n)}"><NAME>{_esc(n)}</NAME><GUID>{c["guid"]}</GUID>'
                f"<STATENAME>{_esc(c['state'])}</STATENAME>"
                f'<BOOKSFROM TYPE="Date">{c["books_from"]}</BOOKSFROM>'
                f"<GSTREGISTRATIONNUMBER>{c['gstin'] or ''}</GSTREGISTRATIONNUMBER></COMPANY>"
                for n, c in self.companies.items()
            )
            return _collection_envelope(body)

        name, company = self._company(root)
        if company is None:
            return _error_envelope(f"Could not set 'SVCurrentCompany' to '{name}'")
        if obj_type == "Ledger":
            return _collection_envelope(
                "".join(
                    _ledger_xml(i, led, fetch) for i, led in enumerate(company["ledgers"].values())
                )
            )
        if obj_type == "Group":
            body = "".join(
                f'<GROUP NAME="{_esc(g)}"><PARENT>{_esc(p) if p else _PRIMARY}</PARENT></GROUP>'
                for g, p in company["groups"].items()
            )
            return _collection_envelope(body)
        return _error_envelope(f"Unsupported collection type '{obj_type}'")

    def _import(self, root: ET.Element) -> str:
        _, company = self._company(root)
        if company is None:
            # Tally does not reliably switch to the company asked for: the import lands in
            # whichever company is active.
            company = next(iter(self.companies.values()), None)
            if company is None:
                return _error_envelope("No company is open")
        report = (root.findtext(".//REPORTNAME") or "").strip()
        counts = {"created": 0, "altered": 0, "errors": 0, "exceptions": 0}
        line_errors: list[str] = []
        last_vch_id = 0
        if report == "All Masters":
            for el in root.iter("LEDGER"):
                outcome, err = self._create_ledger(company, el)
                if err:
                    counts["errors"] += 1
                    line_errors.append(err)
                else:
                    counts[outcome] += 1
        elif report == "Vouchers":
            for el in root.iter("VOUCHER"):
                outcome, err = self._create_voucher(company, el)
                if err:
                    counts["exceptions"] += 1
                    line_errors[:] = [err]  # Tally reports only the last rejection
                else:
                    counts[outcome] += 1
                    last_vch_id = len(company["vouchers"])
        else:
            return _UNKNOWN_REQUEST
        return _import_response(counts, line_errors, last_vch_id)

    def _create_ledger(self, company: dict, el: ET.Element) -> tuple[str, str | None]:
        name = (el.get("NAME") or "").strip()
        parent = (el.findtext("PARENT") or "").strip()
        if not name:
            return "", "Ledger name is missing"
        if parent not in company["groups"]:
            return "", f"Group '{parent}' does not exist"
        sent = {
            "parent": parent,
            "gstin": _field(el, "PARTYGSTIN"),
            "state": _field(el, "LEDSTATENAME"),
            "gst_details": [
                {
                    "since": _field(r, "APPLICABLEFROM"),
                    "type": _field(r, "GSTREGISTRATIONTYPE"),
                    "state": _field(r, "STATE"),
                    "gstin": _field(r, "GSTIN"),
                }
                for r in el.findall("LEDGSTREGDETAILS.LIST")
            ],
            "mailing": [
                {
                    "since": _field(m, "APPLICABLEFROM"),
                    "state": _field(m, "STATE"),
                    "country": _field(m, "COUNTRY"),
                    "pincode": _field(m, "PINCODE"),
                    "address": [a.text or "" for a in m.iter("ADDRESS")],
                }
                for m in el.findall("LEDMAILINGDETAILS.LIST")
            ],
        }
        key = name.casefold()
        existing = next((d for n, d in company["ledgers"].items() if n.casefold() == key), None)
        if existing is not None:
            # Like Tally: the fields sent replace that ledger's, the others stay.
            existing.update({k: v for k, v in sent.items() if v})
            return "altered", None
        if any(
            key == a.casefold() for d in company["ledgers"].values() for a in d.get("aliases", [])
        ):
            return "", f"Ledger '{name}' already exists"
        company["ledgers"][name] = {"name": name, **sent}
        return "created", None

    def _create_voucher(self, company: dict, el: ET.Element) -> tuple[str, str | None]:
        date = (el.findtext("DATE") or "").strip()
        vch_type = el.get("VCHTYPE") or ""
        if len(date) != 8 or not date.isdigit():
            return "", f"Invalid voucher date '{date}'"
        if self.educational and int(date[6:]) not in {1, 2, 31}:
            return "", f"Voucher date is missing for: '{vch_type}' voucher"
        total = Decimal("0")
        lines = el.findall("ALLLEDGERENTRIES.LIST")
        if len(lines) < 2:
            return "", "Voucher needs at least two ledger entries"
        for line in lines:
            ledger = (line.findtext("LEDGERNAME") or "").strip()
            if ledger not in company["ledgers"]:
                return "", f"Ledger '{ledger}' does not exist!"
            try:
                amount = Decimal((line.findtext("AMOUNT") or "").strip())
            except InvalidOperation:
                return "", f"Invalid amount for ledger '{ledger}'"
            deemed_positive = (line.findtext("ISDEEMEDPOSITIVE") or "").strip() == "Yes"
            if deemed_positive != (amount < 0):
                return "", f"ISDEEMEDPOSITIVE does not match the sign of the amount for '{ledger}'"
            total += amount
        if total != 0:
            return "", f"Voucher totals do not match (difference {total})"
        record = {
            "remote_id": el.get("REMOTEID"),
            "type": vch_type,
            "date": date,
            "reference": el.findtext("REFERENCE"),
            "xml": ET.tostring(el, encoding="unicode"),
        }
        vouchers = company["vouchers"]
        for i, existing in enumerate(vouchers):
            if record["remote_id"] and existing["remote_id"] == record["remote_id"]:
                vouchers[i] = record
                return "altered", None
        vouchers.append(record)
        return "created", None


def _field(el: ET.Element, tag: str) -> str | None:
    return (el.findtext(tag) or "").strip() or None


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _tag(tag: str, value: str | None) -> str:
    return f"<{tag}>{_esc(value)}</{tag}>" if value else ""


def _dated_list(tag: str, entries: list[dict], fields: dict[str, str]) -> str:
    if not entries:
        return f"<{tag}>     </{tag}>"  # Tally's placeholder for an empty list
    parts = []
    for e in entries:
        since = (
            f'<APPLICABLEFROM TYPE="Date">{e["since"]}</APPLICABLEFROM>' if e.get("since") else ""
        )
        address = "".join(f"<ADDRESS>{_esc(a)}</ADDRESS>" for a in e.get("address") or [])
        if address:
            address = f'<ADDRESS.LIST TYPE="String">{address}</ADDRESS.LIST>'
        inner = "".join(_tag(xml_tag, e.get(key)) for key, xml_tag in fields.items())
        parts.append(f"<{tag}>{since}{address}{inner}</{tag}>")
    return "".join(parts)


def _ledger_xml(i: int, led: dict, fetch: set[str]) -> str:
    names = "".join(f"<NAME>{_esc(n)}</NAME>" for n in [led["name"], *led.get("aliases", [])])
    parts = [
        f'<LEDGER NAME="{_esc(led["name"])}">',
        f"<GUID>guid-ledger-{i}</GUID>",
        f"<PARENT>&#4; {_esc(led['parent'])}</PARENT>",
        f"<PARTYGSTIN>{led.get('gstin') or ''}</PARTYGSTIN>",
        f"<LEDSTATENAME>{_esc(led.get('state') or '')}</LEDSTATENAME>",
    ]
    if "LEDGSTREGDETAILS.LIST" in fetch:
        reg_fields = {"type": "GSTREGISTRATIONTYPE", "state": "STATE", "gstin": "GSTIN"}
        parts.append(_dated_list("LEDGSTREGDETAILS.LIST", led.get("gst_details") or [], reg_fields))
    if "LEDMAILINGDETAILS.LIST" in fetch:
        mail_fields = {"state": "STATE", "country": "COUNTRY", "pincode": "PINCODE"}
        parts.append(_dated_list("LEDMAILINGDETAILS.LIST", led.get("mailing") or [], mail_fields))
    parts.append(
        f'<LANGUAGENAME.LIST><NAME.LIST TYPE="String">{names}</NAME.LIST></LANGUAGENAME.LIST>'
    )
    parts.append("</LEDGER>")
    return "".join(parts)


def _encode(text: str, utf16: bool) -> bytes:
    if utf16:
        return codecs.BOM_UTF16_LE + text.encode("utf-16-le")
    # Answering a UTF-8 request, Tally loses what its 8-bit character set can't hold.
    return text.encode("latin-1", errors="replace").decode("latin-1").encode("utf-8")


def _collection_envelope(body: str) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>"
        f"<BODY><DESC></DESC><DATA><COLLECTION>{body}</COLLECTION></DATA></BODY></ENVELOPE>"
    )


def _error_envelope(message: str) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER>"
        f"<BODY><DATA><LINEERROR>{_esc(message)}</LINEERROR></DATA></BODY></ENVELOPE>"
    )


def _import_response(counts: dict[str, int], line_errors: list[str], last_vch_id: int) -> str:
    errors = "".join(f"<LINEERROR>{_esc(e)}</LINEERROR>" for e in line_errors)
    return (
        f"<RESPONSE>{errors}<CREATED>{counts['created']}</CREATED>"
        f"<ALTERED>{counts['altered']}</ALTERED><DELETED>0</DELETED>"
        f"<LASTVCHID>{last_vch_id}</LASTVCHID><LASTMID>0</LASTMID><COMBINED>0</COMBINED>"
        f"<IGNORED>0</IGNORED><ERRORS>{counts['errors']}</ERRORS><CANCELLED>0</CANCELLED>"
        f"<EXCEPTIONS>{counts['exceptions']}</EXCEPTIONS></RESPONSE>"
    )


def _content_type(request_content_type: str) -> str:
    charset = "utf-16" if "utf-16" in request_content_type.lower() else "utf-8"
    return f"text/xml; charset={charset}"


def httpx_transport(mock: MockTally):
    """An httpx transport that routes requests to the mock, for tests."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"<RESPONSE>TallyPrime Server is Running</RESPONSE>")
        content_type = request.headers.get("content-type", "")
        return httpx.Response(
            200,
            content=mock.handle(request.content, content_type),
            headers={"Content-Type": _content_type(content_type)},
        )

    return httpx.MockTransport(handler)


def serve(port: int, educational: bool = False) -> None:
    mock = MockTally(educational=educational)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            body = b"<RESPONSE>TallyPrime Server is Running (mock)</RESPONSE>"
            self._send(body, "text/xml; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            content_type = self.headers.get("Content-Type", "")
            self._send(
                mock.handle(self.rfile.read(length), content_type), _content_type(content_type)
            )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    mode = " in Educational mode" if educational else ""
    print(f"Mock Tally listening on http://127.0.0.1:{port}{mode} (company: {DEMO_COMPANY})")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--educational",
        action="store_true",
        help="refuse vouchers not dated the 1st, 2nd or 31st, like TallyPrime's Educational mode",
    )
    args = parser.parse_args()
    serve(args.port, educational=args.educational)
