"""Parses Tally XML responses.

Tally's output is not always well-formed XML: it can be UTF-16 encoded and can contain
control-character references such as ``&#4;`` (used e.g. in front of "Primary").
Everything is decoded and sanitised before parsing.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

from defusedxml.ElementTree import fromstring

from app.connectors.base import ConnectorError, ExternalCompany, ExternalGroup, ExternalLedger

# Character references XML 1.0 does not allow (everything below 0x20 except tab/LF/CR).
_BAD_CHAR_REF = re.compile(
    r"&#(?:x0*(?:[0-8bBcCeEfF]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));"
)
_BAD_RAW_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) > 1 and raw[1:2] == b"\x00":  # UTF-16LE without BOM
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig", errors="replace")


def sanitize(text: str) -> str:
    return _BAD_RAW_CHARS.sub("", _BAD_CHAR_REF.sub("", text))


def parse(raw: bytes) -> ET.Element:
    text = sanitize(decode(raw)).strip()
    if not text:
        raise ConnectorError("Empty response from Tally")
    try:
        return fromstring(text)
    except ET.ParseError as exc:
        raise ConnectorError(f"Unparseable response from Tally: {exc}") from exc


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _child_text(el: ET.Element, tag: str) -> str | None:
    return _text(el.find(tag))


def _descendant_text(el: ET.Element, tag: str) -> str | None:
    for found in el.iter(tag):
        if (value := _text(found)) is not None:
            return value
    return None


def _object_name(el: ET.Element) -> str | None:
    return (el.get("NAME") or "").strip() or _child_text(el, "NAME")


def parse_date(value: str | None) -> date | None:
    """A Tally date: 20250401 in XML exports, 1-Apr-2025 in some reports."""
    for fmt in ("%Y%m%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _in_force(el: ET.Element, list_tag: str, on: date) -> ET.Element | None:
    """The entry of a dated list (such as LEDGSTREGDETAILS.LIST) that applies on a date: the
    latest one dated on or before it. Undated entries count as the earliest, and empty
    placeholders (Tally sends one when the list is empty) are skipped. When every entry
    starts later, the earliest one is used."""
    entries = []
    for i, entry in enumerate(el.findall(list_tag)):
        if any(_text(child) for child in entry.iter() if child is not entry):
            since = parse_date(_child_text(entry, "APPLICABLEFROM")) or date.min
            entries.append((since, i, entry))
    if not entries:
        return None
    current = [e for e in entries if e[0] <= on]
    return max(current)[2] if current else min(entries)[2]


def _entry_text(entry: ET.Element | None, tag: str) -> str | None:
    return _child_text(entry, tag) if entry is not None else None


def line_errors(root: ET.Element) -> list[str]:
    return [t for e in root.iter("LINEERROR") if (t := _text(e))]


def raise_for_export_error(root: ET.Element) -> None:
    errors = line_errors(root)
    status = _descendant_text(root, "STATUS")
    if errors or status == "0":
        raise ConnectorError("; ".join(errors) or "Tally returned an error status")


def parse_companies(raw: bytes) -> list[ExternalCompany]:
    root = parse(raw)
    raise_for_export_error(root)
    companies = []
    for el in root.iter("COMPANY"):
        name = _object_name(el)
        if not name:
            continue
        companies.append(
            ExternalCompany(
                name=name,
                external_id=_child_text(el, "GUID"),
                state=_child_text(el, "STATENAME"),
                gstin=_descendant_text(el, "GSTREGISTRATIONNUMBER"),
                books_from=parse_date(_child_text(el, "BOOKSFROM")),
            )
        )
    return companies


def parse_ledgers(raw: bytes, on: date | None = None) -> list[ExternalLedger]:
    """Ledgers with the GSTIN and state in force on a date (today by default). TallyPrime
    3.0+ keeps these in dated lists; the flat fields are used when a ledger has none."""
    root = parse(raw)
    raise_for_export_error(root)
    on = on or date.today()
    ledgers = []
    for el in root.iter("LEDGER"):
        name = _object_name(el)
        if not name:
            continue
        # Alternate names (aliases) live in LANGUAGENAME.LIST/NAME.LIST/NAME.
        names = [t for nl in el.iter("NAME.LIST") for n in nl.findall("NAME") if (t := _text(n))]
        aliases = list(dict.fromkeys(n for n in names if n != name))
        reg = _in_force(el, "LEDGSTREGDETAILS.LIST", on)
        mailing = _in_force(el, "LEDMAILINGDETAILS.LIST", on)
        gstin = _entry_text(reg, "GSTIN") or _child_text(el, "PARTYGSTIN")
        state = (
            _entry_text(mailing, "STATE")
            or _entry_text(reg, "STATE")
            or _child_text(el, "LEDSTATENAME")
            or _descendant_text(el, "STATENAME")
        )
        ledgers.append(
            ExternalLedger(
                name=name,
                parent=_child_text(el, "PARENT"),
                gstin=gstin.upper() if gstin else None,
                state=state,
                aliases=aliases,
                external_id=_child_text(el, "GUID"),
            )
        )
    return ledgers


def parse_groups(raw: bytes) -> list[ExternalGroup]:
    root = parse(raw)
    raise_for_export_error(root)
    return [
        ExternalGroup(name=name, parent=_child_text(el, "PARENT"))
        for el in root.iter("GROUP")
        if (name := _object_name(el))
    ]


@dataclass
class ImportResult:
    created: int = 0
    altered: int = 0
    errors: int = 0
    exceptions: int = 0
    ignored: int = 0
    last_voucher_id: str | None = None
    messages: list[str] = field(default_factory=list)
    # Tally did not recognise the request at all ("Unknown Request, cannot be processed").
    unknown_request: bool = False

    @property
    def success(self) -> bool:
        return self.errors == 0 and self.exceptions == 0 and (self.created + self.altered) > 0


_COUNTERS = ("CREATED", "ALTERED", "DELETED", "COMBINED", "IGNORED", "ERRORS", "EXCEPTIONS")


def _int(root: ET.Element, tag: str) -> int:
    value = _descendant_text(root, tag)
    try:
        return int(value) if value else 0
    except ValueError:
        return 0


def parse_import_result(raw: bytes) -> ImportResult:
    """Tally's counters. A rejection shows up as ERRORS or EXCEPTIONS, with or without a
    LINEERROR (only the last one when several lines fail). A request Tally can't read gets
    a bare <RESPONSE>Unknown Request, cannot be processed</RESPONSE> with no counters."""
    root = parse(raw)
    result = ImportResult(
        created=_int(root, "CREATED"),
        altered=_int(root, "ALTERED"),
        errors=_int(root, "ERRORS"),
        exceptions=_int(root, "EXCEPTIONS"),
        ignored=_int(root, "IGNORED"),
        last_voucher_id=_descendant_text(root, "LASTVCHID"),
        messages=line_errors(root),
    )
    if result.messages and result.errors == 0:
        result.errors = len(result.messages)
    if not result.messages and all(root.find(f".//{tag}") is None for tag in _COUNTERS):
        result.unknown_request = True
        result.errors = max(result.errors, 1)
        if reply := " ".join("".join(root.itertext()).split()):
            result.messages.append(reply)
    return result
