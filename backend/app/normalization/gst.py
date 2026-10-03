"""GST reference data and GSTIN checks: state codes, state-name lookup, valid rates."""

import re
from decimal import Decimal

STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh (Before Division)",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre Jurisdiction",
}

GST_RATES: frozenset[Decimal] = frozenset(
    Decimal(r)
    for r in ("0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40")
)

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# [0-9], not \d: \d also matches Devanagari and fullwidth digits, which are not in _ALPHABET.
# State code + 13 characters. Not a PAN shape: TDS deductors (government offices) are
# registered on a TAN, and UINs (embassies, UN bodies) and non-resident registrations have
# digits where a PAN has letters. The checksum is what catches a misread character.
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[0-9A-Z]{13}$")
# Spaces, dots, every dash and minus sign, and the invisible characters PDFs and OCR leave in.
_GSTIN_SEPARATORS = re.compile(r"[\s.\-‐-―−​-‍⁠﻿]+")

# Legacy codes still printed on old billing templates. No GSTIN carries them: undivided
# Andhra Pradesh (28) became 37 before GST began, and Daman and Diu (25) merged into 26 in
# 2020. Names and codes are resolved to the current code so they match the GSTIN.
_SUCCESSOR = {"25": "26", "28": "37"}

# Normalised spellings that differ from the canonical names.
_NAME_ALIASES: dict[str, str] = {
    "jammu kashmir": "01",
    "uttaranchal": "05",
    "nct of delhi": "07",
    "national capital territory of delhi": "07",
    "new delhi": "07",
    "orissa": "21",
    "chattisgarh": "22",
    "chhatisgarh": "22",
    "chattisgadh": "22",
    "daman and diu": "26",
    "dadra and nagar haveli": "26",
    "dadra nagar haveli": "26",
    "dnh and dd": "26",
    "andhra pradesh": "37",
    "andhra pradesh old": "37",
    "pondicherry": "34",
    "pondichery": "34",
    "andaman and nicobar": "35",
    "andaman nicobar": "35",
    "andaman and nicobar island": "35",
    "andaman": "35",
    "tamilnadu": "33",
    "telengana": "36",
}

# Short forms are only trusted when they are the whole text, never inside a longer one.
_ABBREVIATIONS: dict[str, str] = {
    "j and k": "01",
    "jk": "01",
    "hp": "02",
    "up": "09",
    "wb": "19",
    "mp": "23",
    "tn": "33",
    "a and n": "35",
    "a and n islands": "35",
    "ap": "37",
}

_NOISE_PHRASES = re.compile(r"\b(?:place of supply|state code|state name|state|code|ut|india)\b")
_STANDALONE_CODE = re.compile(r"\b\d{1,2}\b")


def clean_gstin(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = _GSTIN_SEPARATORS.sub("", raw).upper()
    return cleaned or None


def gstin_check_char(body: str) -> str:
    """The checksum character for the first 14 characters of a GSTIN."""
    total = 0
    for i, char in enumerate(body[:14]):
        product = _ALPHABET.index(char) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _ALPHABET[(36 - total % 36) % 36]


def is_valid_gstin(gstin: str | None) -> bool:
    value = clean_gstin(gstin)
    if value is None or not _GSTIN_SHAPE.match(value):
        return False
    return value[:2] in STATE_CODES and gstin_check_char(value) == value[14]


def state_code_from_gstin(gstin: str | None) -> str | None:
    value = clean_gstin(gstin)
    if value is None or len(value) != 15:
        return None
    code = value[:2]
    return code if code in STATE_CODES else None


def _normalise(text: str) -> str:
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _current(code: str) -> str:
    return _SUCCESSOR.get(code, code)


def _name_index() -> dict[str, str]:
    index = {_normalise(name): _current(code) for code, name in STATE_CODES.items()}
    index.update(_NAME_ALIASES)
    return index


_NAMES = _name_index()
# Longest first, so "new delhi" wins over "delhi" when searching inside longer text.
_NAMES_BY_LENGTH = sorted(_NAMES.items(), key=lambda item: len(item[0]), reverse=True)


def _code_in_text(text: str) -> str | None:
    found: set[str] = set()
    remaining = f" {text} "
    for name, code in _NAMES_BY_LENGTH:
        needle = f" {name} "
        if needle in remaining:
            found.add(code)
            remaining = remaining.replace(needle, " | ")
    return found.pop() if len(found) == 1 else None


def _code_from_words(words: str) -> str | None:
    if not words:
        return None
    if words in _NAMES:
        return _NAMES[words]
    if words in _ABBREVIATIONS:
        return _ABBREVIATIONS[words]
    return _code_in_text(words)


def state_code_from_name(name: str | None) -> str | None:
    """The GST state code for a printed state, e.g. "NCT of Delhi", "(29) Karnataka" or "29".

    Legacy codes come back as their current code (28 as 37, 25 as 26), the one GSTINs use.
    Returns None when the text names no state, or names two that disagree.
    """
    if not name or not name.strip():
        return None
    numbers = {n.zfill(2) for n in _STANDALONE_CODE.findall(name)}
    printed_codes = {_current(n) for n in numbers if n in STATE_CODES}
    if len(printed_codes) > 1:
        return None
    printed = printed_codes.pop() if printed_codes else None

    words = _normalise(_STANDALONE_CODE.sub(" ", name))
    words = " ".join(_NOISE_PHRASES.sub(" ", words).split())
    named = _code_from_words(words)

    if printed is None or named is None:
        return printed or named
    return printed if printed == named else None
