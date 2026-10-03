"""Turns an InvoiceExtraction (strings as Claude read them) into a typed NormalizedInvoice.

Every field the review screen highlights gets a confidence: the model's own when the value
was read cleanly or not printed, 0.0 when something was printed but could not be read.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.normalization.gst import (
    STATE_CODES,
    clean_gstin,
    is_valid_gstin,
    state_code_from_gstin,
    state_code_from_name,
)
from app.normalization.values import parse_amount, parse_date
from app.schemas.extraction import (
    ExtractedLineItem,
    ExtractedParty,
    ExtractedValue,
    InvoiceExtraction,
)
from app.schemas.invoice import InvoiceLine, NormalizedInvoice, Party

CONFIDENCE: dict[str, float] = {"high": 0.95, "medium": 0.7, "low": 0.4}
DERIVED_CONFIDENCE = 0.7
UNREADABLE = 0.0

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
# No invoice amount or quantity comes near this; a larger value is a misread such as
# concatenated digits, and from 27 digits on it cannot even be rounded to paise.
_IMPLAUSIBLE = Decimal("1e15")

# What invoices print in the GSTIN box for an unregistered party, after clean_gstin.
_NO_GSTIN = frozenset(
    {
        "NA",
        "N/A",
        "NIL",
        "NONE",
        "URP",
        "URD",
        "UNREGD",
        "UNREGISTERED",
        "UNREGISTEREDDEALER",
        "UNREGISTEREDPERSON",
        "NOTREGISTERED",
        "NOTAPPLICABLE",
        "NOTAVAILABLE",
        "CONSUMER",
    }
)

_ZERO_DEFAULT_TOTALS = {
    "cgst": "CGST amount",
    "sgst": "SGST amount",
    "igst": "IGST amount",
    "cess": "cess amount",
    "round_off": "round-off amount",
}


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _plausible_number(raw: str) -> Decimal | None:
    value = parse_amount(raw)
    return None if value is None or abs(value) >= _IMPLAUSIBLE else value


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _text_or_none(text: str | None) -> str | None:
    return None if _blank(text) else text.strip()


class _Reader:
    """Parses extracted values while recording field confidence and reviewer notes."""

    def __init__(self, notes: list[str]) -> None:
        self.confidence: dict[str, float] = {}
        self.notes = notes

    def _unreadable(self, key: str, label: str, raw: str, expected: str) -> None:
        self.confidence[key] = UNREADABLE
        self.notes.append(
            f'The {label} could not be read as {expected} ("{raw.strip()}"); '
            "enter it from the document."
        )

    def text(self, key: str, field: ExtractedValue) -> str | None:
        self.confidence[key] = CONFIDENCE[field.confidence]
        return _text_or_none(field.value)

    def gstin(self, key: str, field: ExtractedValue) -> str | None:
        self.confidence[key] = CONFIDENCE[field.confidence]
        gstin = clean_gstin(field.value)
        return None if gstin in _NO_GSTIN else gstin

    def amount(self, key: str, label: str, field: ExtractedValue) -> Decimal | None:
        self.confidence[key] = CONFIDENCE[field.confidence]
        if _blank(field.value):
            return None
        value = _plausible_number(field.value)
        if value is None:
            self._unreadable(key, label, field.value, "an amount")
        return _money(value)

    def date(self, key: str, label: str, field: ExtractedValue) -> date | None:
        self.confidence[key] = CONFIDENCE[field.confidence]
        if _blank(field.value):
            return None
        value = parse_date(field.value)
        if value is None:
            self._unreadable(key, label, field.value, "a date")
        return value

    def number(self, key: str, label: str, raw: str | None) -> Decimal | None:
        """A line-item number; only failures get a confidence entry, lines have no model one."""
        if _blank(raw):
            return None
        value = _plausible_number(raw.replace("%", ""))
        if value is None:
            self._unreadable(key, label, raw, "a number")
        return value


def _party(reader: _Reader, role: str, extracted: ExtractedParty) -> Party:
    name = reader.text(f"{role}.name", extracted.name)
    gstin = reader.gstin(f"{role}.gstin", extracted.gstin)
    valid = is_valid_gstin(gstin)
    code = state_code_from_gstin(gstin) if valid else state_code_from_name(extracted.state)
    return Party(
        name=name,
        gstin=gstin,
        gstin_valid=valid,
        state_code=code,
        state=STATE_CODES[code] if code else None,
        address=_text_or_none(extracted.address),
    )


def _is_empty_line(item: ExtractedLineItem) -> bool:
    return all(_blank(v) for v in (item.description, item.quantity, item.rate, item.taxable_value))


def _line(reader: _Reader, index: int, item: ExtractedLineItem) -> InvoiceLine:
    def number(field: str, label: str) -> Decimal | None:
        raw = getattr(item, field)
        return reader.number(f"lines.{index}.{field}", f"{label} on line {index + 1}", raw)

    return InvoiceLine(
        description=item.description.strip(),
        hsn_sac=_text_or_none(item.hsn_sac),
        quantity=number("quantity", "quantity"),
        unit=_text_or_none(item.unit),
        rate=number("rate", "rate"),
        taxable_value=_money(number("taxable_value", "taxable value")),
        gst_rate=number("gst_rate", "GST rate"),
    )


def _has_numbers(item: ExtractedLineItem) -> bool:
    return not all(_blank(v) for v in (item.quantity, item.rate, item.taxable_value))


def _taxable_from_lines(items: list[ExtractedLineItem], lines: list[InvoiceLine]) -> Decimal | None:
    """Sum of the line taxable values, or None if a line with printed numbers has no readable
    taxable value: a sum without it would be a wrong total, not a partial one."""
    if any(
        _has_numbers(item) and line.taxable_value is None
        for item, line in zip(items, lines, strict=True)
    ):
        return None
    values = [line.taxable_value for line in lines if line.taxable_value is not None]
    return sum(values, _ZERO) if values else None


def normalize(extraction: InvoiceExtraction) -> NormalizedInvoice:
    reader = _Reader(notes=list(extraction.notes))
    totals = extraction.totals

    invoice_number = reader.text("invoice_number", extraction.invoice_number)
    invoice_date = reader.date("invoice_date", "invoice date", extraction.invoice_date)
    seller = _party(reader, "seller", extraction.seller)
    buyer = _party(reader, "buyer", extraction.buyer)

    place_of_supply = _text_or_none(extraction.place_of_supply)
    place_of_supply_code = state_code_from_name(place_of_supply)
    if place_of_supply and place_of_supply_code is None:
        reader.notes.append(
            f'The place of supply "{place_of_supply}" is not a recognised state; '
            "select the state on the review screen."
        )

    items = [item for item in extraction.line_items if not _is_empty_line(item)]
    lines = [_line(reader, i, item) for i, item in enumerate(items)]

    taxable_value = reader.amount("taxable_value", "taxable value", totals.taxable_value)
    if _blank(totals.taxable_value.value):
        from_lines = _taxable_from_lines(items, lines)
        if from_lines is not None:
            taxable_value = from_lines
            reader.confidence["taxable_value"] = DERIVED_CONFIDENCE
            reader.notes.append(
                "The total taxable value is not printed, so the sum of the line items "
                f"({from_lines}) was used; check it against the document."
            )

    zero_default = {}
    for key, label in _ZERO_DEFAULT_TOTALS.items():
        value = reader.amount(key, label, getattr(totals, key))
        zero_default[key] = _ZERO if value is None else value
    grand_total = reader.amount("grand_total", "grand total", totals.grand_total)

    return NormalizedInvoice(
        is_invoice=extraction.is_invoice,
        document_type=extraction.document_type,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        seller=seller,
        buyer=buyer,
        place_of_supply_code=place_of_supply_code,
        reverse_charge=bool(extraction.reverse_charge),
        lines=lines,
        taxable_value=taxable_value,
        grand_total=grand_total,
        confidence=reader.confidence,
        notes=reader.notes,
        **zero_default,
    )


def _refresh_party(party: Party) -> Party:
    gstin = clean_gstin(party.gstin)
    gstin = None if gstin in _NO_GSTIN else gstin
    valid = is_valid_gstin(gstin)
    if valid:
        code = state_code_from_gstin(gstin)
    else:
        code = state_code_from_name(party.state) or party.state_code
    return party.model_copy(
        update={
            "name": _text_or_none(party.name),
            "gstin": gstin,
            "gstin_valid": valid,
            "state_code": code if code in STATE_CODES else None,
            "state": STATE_CODES.get(code) if code else party.state,
        }
    )


def refresh_derived(invoice: NormalizedInvoice) -> NormalizedInvoice:
    """Recomputes the fields a person never types (GSTIN validity, state codes) and tidies
    the ones they do, after an edit on the review screen. Client-sent derived values are
    not trusted: a GSTIN typed in must be checked here, not marked valid by the browser."""
    money = {
        key: _money(getattr(invoice, key))
        for key in ("taxable_value", "grand_total", "cgst", "sgst", "igst", "cess", "round_off")
    }
    return invoice.model_copy(
        update={
            "invoice_number": _text_or_none(invoice.invoice_number),
            "seller": _refresh_party(invoice.seller),
            "buyer": _refresh_party(invoice.buyer),
            **{
                k: (v if v is not None or k in ("taxable_value", "grand_total") else _ZERO)
                for k, v in money.items()
            },
        }
    )
