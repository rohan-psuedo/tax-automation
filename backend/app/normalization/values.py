"""Parses amounts and dates as printed on Indian invoices.

Both parsers refuse to guess: anything ambiguous or malformed comes back as None so a
reviewer is asked, instead of a plausible but wrong number reaching the books.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_CURRENCY = re.compile(r"₹|\brs\.?|\binr\.?|\brupees\b|\bonly\b", re.IGNORECASE)
_WHITESPACE = re.compile(r"[\s   ]+")
_DASHES = str.maketrans({"−": "-", "–": "-", "—": "-"})
_SPACE_GROUPED = re.compile(r"^\d{1,3}(?: \d{2,3})+(?:\.\d*)?$")
_NUMBER = re.compile(r"^[\d,]*\.?\d*$")
# Indian lakh/crore grouping (12,34,567) or Western thousands grouping (1,234,567). A first
# group starting with 0 ("0,500") is a decimal comma or a misread, never grouping.
_INDIAN_GROUPING = re.compile(r"^[1-9]\d?(?:,\d{2})*,\d{3}$")
_WESTERN_GROUPING = re.compile(r"^[1-9]\d{0,2}(?:,\d{3})+$")
_SUFFIXES = ("/-", "/=")
# What an amount column shows when nothing is charged, e.g. IGST on an intra-state invoice.
_NOTHING_CHARGED = re.compile(r"^(?:-|nil\.?)$", re.IGNORECASE)
_TALLY_SIGNS = ("(-)", "(+)")


def _drop_suffix(text: str) -> str:
    text = text.strip()
    return text[:-2].strip() if text.endswith(_SUFFIXES) else text


def _split_sign(text: str) -> tuple[str, bool] | None:
    """Removes brackets or a leading/trailing sign; None when more than one is present."""
    markers = 0
    negative = False
    if text.startswith(_TALLY_SIGNS):  # TallyPrime prints "(-)0.40"
        text, markers, negative = text[3:].strip(), 1, text[1] == "-"
    if text.startswith("(") and text.endswith(")"):
        markers, negative = markers + 1, True
        text = _drop_suffix(text[1:-1])  # "(Rs. 500/-)"
    if text.startswith(("+", "-")):
        markers, negative = markers + 1, negative or text[0] == "-"
        text = text[1:].strip()
    if text.endswith(("+", "-")):
        markers, negative = markers + 1, negative or text[-1] == "-"
        text = text[:-1].strip()
    if markers > 1:
        return None  # "-(100)" or "-100-" is a misread, not a value
    return text, negative


def _grouping_ok(integer_part: str) -> bool:
    if "," not in integer_part:
        return True
    return bool(_INDIAN_GROUPING.match(integer_part) or _WESTERN_GROUPING.match(integer_part))


def parse_amount(raw: str | None) -> Decimal | None:
    """A printed amount as a Decimal (not rounded), or None if it is empty or unclear.

    A lone "-" or "Nil" is 0, and Tally's "(-)0.40" is negative.
    """
    if raw is None:
        return None
    text = _drop_suffix(_CURRENCY.sub(" ", raw.translate(_DASHES)))
    text = " ".join(_WHITESPACE.split(text))
    if not text:
        return None
    if _NOTHING_CHARGED.match(text):
        return Decimal("0")

    unsigned = _split_sign(text)
    if unsigned is None:
        return None
    text, negative = unsigned
    if _SPACE_GROUPED.match(text):
        text = text.replace(" ", ",")
    if text.count(".") > 1 or not _NUMBER.match(text):
        return None
    integer_part, _, fraction = text.partition(".")
    if not (integer_part or fraction) or not _grouping_ok(integer_part):
        return None
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return -value if negative and value else value


_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTHS = {n[:3]: i for i, n in enumerate(_MONTH_NAMES, 1)}
_MONTHS |= {n: i for i, n in enumerate(_MONTH_NAMES, 1)} | {"sept": 9}

_ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b")
_TIME_SUFFIX = re.compile(r"[ t]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:am|pm|z)?$")
_YEAR_FIRST = re.compile(r"^(\d{4})([-/.])(\d{1,2})\2(\d{1,2})$")
_DAY_FIRST = re.compile(r"^(\d{1,2})([-/. ])(\d{1,2})\2(\d{4}|\d{2})$")
_DAY_MONTH_NAME = re.compile(r"^(\d{1,2})[-/. ]+([a-z]+)\.?[-/. ]+(\d{4}|\d{2})$")
_MONTH_NAME_DAY = re.compile(r"^([a-z]+)\.?[-/. ]+(\d{1,2})[-/. ]+(\d{4})$")


def _year(text: str) -> int:
    return 2000 + int(text) if len(text) == 2 else int(text)


def _make_date(year: int, month: int | None, day: int) -> date | None:
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(raw: str | None) -> date | None:
    """A printed date, read day-first as Indian documents print it; None if invalid."""
    if raw is None:
        return None
    text = " ".join(raw.lower().replace(",", " ").split())
    text = _TIME_SUFFIX.sub("", _ORDINAL.sub("", text)).strip()
    if not text:
        return None

    if m := _YEAR_FIRST.match(text):
        return _make_date(int(m[1]), int(m[3]), int(m[4]))
    if m := _DAY_FIRST.match(text):
        return _make_date(_year(m[4]), int(m[3]), int(m[1]))
    if m := _DAY_MONTH_NAME.match(text):
        return _make_date(_year(m[3]), _MONTHS.get(m[2]), int(m[1]))
    if m := _MONTH_NAME_DAY.match(text):
        return _make_date(int(m[3]), _MONTHS.get(m[1]), int(m[2]))
    return None
