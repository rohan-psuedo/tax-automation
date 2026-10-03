"""Finds the ledgers a voucher posts to among the ledgers synced from the accounting system.

Party names are compared after normalization (case, "M/s", punctuation, "&", and the usual
spellings of Pvt/Ltd/Co), so "M/S. SHARMA ELECTRONICS PVT. LTD." and "Sharma Electronics
Private Limited" are the same name. A GSTIN outranks any name: a ledger whose GSTIN differs
from the party's valid GSTIN is never matched on its name alone.

Ledgers are placed by their group. The sync passes only a ledger's immediate group, so a
ledger under a sub-group the company made (say "GST Input" under Duties & Taxes) is placed by
the words in the sub-group's name.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Literal

from rapidfuzz import fuzz

from app.accounting.types import Direction, LedgerInfo, LedgerMatch, MatchMethod

Supply = Literal["intra", "inter"]
RcmUse = Literal["exclude", "prefer", "only"]

PARTY_GROUPS: dict[Direction, str] = {"purchase": "Sundry Creditors", "sales": "Sundry Debtors"}
ITEM_GROUPS: dict[Direction, str] = {"purchase": "Purchase Accounts", "sales": "Sales Accounts"}
TAX_GROUP = "Duties & Taxes"
ROUND_OFF_GROUPS = ("Indirect Expenses", "Indirect Incomes")
EXPENSE_GROUPS = ("Direct Expenses", "Indirect Expenses")
INCOME_GROUPS = ("Direct Incomes", "Indirect Incomes")
# Where a ledger learned for a party may still take the items of an invoice on this side.
ITEM_SIDE_GROUPS: dict[Direction, tuple[str, ...]] = {
    "purchase": (ITEM_GROUPS["purchase"], *EXPENSE_GROUPS),
    "sales": (ITEM_GROUPS["sales"], *INCOME_GROUPS),
}
# Ledgers under these groups hold items, taxes or expenses; they are never the party.
NON_PARTY_GROUPS = (
    TAX_GROUP,
    "Purchase Accounts",
    "Sales Accounts",
    *EXPENSE_GROUPS,
    *INCOME_GROUPS,
)

EXACT_SCORE = 0.97
ALIAS_SCORE = 0.95
# A similar name is never as sure as an equal one, so fuzzy scores stay below an alias match.
MAX_FUZZY_SCORE = 0.94
WEAK_FUZZY_SCORE = 0.6
STRONG_FUZZY = 92
WEAK_FUZZY = 85
PLAUSIBLE = 60
MAX_PARTY_CANDIDATES = 5
MAX_ITEM_CANDIDATES = 10
MAX_NAME_LENGTH = 255

_GENERIC_ITEM_NAMES: dict[Direction, frozenset[str]] = {
    "purchase": frozenset({"purchase", "purchases", "purchase account", "purchase accounts"}),
    "sales": frozenset({"sales", "sale", "sales account", "sales accounts"}),
}

# Tally's own groups, matched by name. Any other group is one the company made.
_TALLY_GROUPS = frozenset(
    " ".join(g.casefold().replace("&", " and ").split())
    for g in (
        "Bank Accounts",
        "Bank OD A/c",
        "Branch / Divisions",
        "Capital Account",
        "Cash-in-Hand",
        "Current Assets",
        "Current Liabilities",
        "Deposits (Asset)",
        "Direct Expenses",
        "Direct Incomes",
        "Duties & Taxes",
        "Fixed Assets",
        "Indirect Expenses",
        "Indirect Incomes",
        "Investments",
        "Loans & Advances (Asset)",
        "Loans (Liability)",
        "Misc. Expenses (ASSET)",
        "Provisions",
        "Purchase Accounts",
        "Reserves & Surplus",
        "Sales Accounts",
        "Secured Loans",
        "Stock-in-Hand",
        "Sundry Creditors",
        "Sundry Debtors",
        "Suspense A/c",
        "Unsecured Loans",
    )
)
_TAX_GROUP_WORD = r"\b(?:(?:[cisu]|ut)?gst\b|tax|dut(?:y|ies)\b|cess\b)"
_TAX_SIDE_WORD = r"\b(?:input|output|payable|receivable|itc)\b"
# First hit wins: "Creditors for Expenses" holds parties, "GST Input" taxes, "Purchase - GST"
# purchases.
_SUB_GROUP_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:creditor|supplier|vendor)"), "Sundry Creditors"),
    (re.compile(r"\b(?:debtor|customer)"), "Sundry Debtors"),
    (re.compile(rf"(?=.*{_TAX_GROUP_WORD})(?=.*{_TAX_SIDE_WORD})"), TAX_GROUP),
    (re.compile(r"\bdirect exp"), "Direct Expenses"),
    (re.compile(r"\bexpense"), "Indirect Expenses"),
    (re.compile(r"\bpurchase"), "Purchase Accounts"),
    (re.compile(r"\bsales?\b"), "Sales Accounts"),
    (re.compile(r"\bdirect inc"), "Direct Incomes"),
    (re.compile(r"\bincome"), "Indirect Incomes"),
    (re.compile(_TAX_GROUP_WORD), TAX_GROUP),
)

_GSTIN_SHAPE = re.compile(r"^[0-9A-Z]{15}$")
_HONORIFIC = re.compile(r"^\s*(?:m\s*/\s*s|messrs)\b\.?\s*", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+")
_EQUIVALENT_WORDS = {"private": "pvt", "limited": "ltd", "company": "co"}
_LEGAL_FORMS = frozenset({"pvt", "ltd", "co", "llp"})
_LETTER_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
_ANY_RATE = re.compile(r"\d+(?:\.\d+)?%")
_ROUND = re.compile(r"\bround", re.IGNORECASE)
_METHOD_TIER: dict[MatchMethod | None, int] = {"exact": 0, "alias": 1, "fuzzy": 2, None: 3}

# Tax names are read on case-folded ledger names. A token starts a word, or follows "input"
# or "output" written without a space, so "Input GST" is not UTGST and "Excess" is not cess;
# dots and spaces may split it, as in "C.G.S.T".
_WORD_START = r"(?:(?<![a-z])|(?<=input)|(?<=output))"
_TAX_TOKENS: dict[str, tuple[str, ...]] = {
    "cgst": ("cgst",),
    "sgst": ("sgst", "utgst"),
    "igst": ("igst",),
    "cess": ("cess",),
}
_TAX_NAMES = {
    tax: re.compile("|".join(_WORD_START + r"[\s.\-_]*".join(t) for t in tokens))
    for tax, tokens in _TAX_TOKENS.items()
}
_SIDE_NAMES: dict[Direction, re.Pattern[str]] = {
    "purchase": re.compile(r"(?<![a-z])(?:input|itc|receivable|credit|claimable)"),
    "sales": re.compile(r"(?<![a-z])(?:output|payable|liabilit)"),
}
_RCM_NAME = re.compile(r"(?<![a-z])(?:rcm|reverse)")
_SUPPLY_NAMES: dict[Supply, re.Pattern[str]] = {
    "intra": re.compile(r"\b(?:local|intra|within)|(?<![a-z])[cs]gst"),
    "inter": re.compile(r"\b(?:inter(?!nat)|outside|central)|(?<![a-z])igst"),
}


# -- names, groups, GSTINs ------------------------------------------------------------------


def clean_gstin(raw: str | None) -> str | None:
    value = re.sub(r"[\s-]", "", raw or "").upper()
    return value or None


def is_gstin_shaped(gstin: str | None) -> bool:
    return bool(gstin and _GSTIN_SHAPE.match(gstin))


def normalize_name(name: str | None) -> str:
    text = _HONORIFIC.sub(" ", (name or "").casefold()).replace("&", " and ")
    return " ".join(_EQUIVALENT_WORDS.get(t, t) for t in _NON_WORD.sub(" ", text).split())


def _legal_forms(normalized: str) -> frozenset[str]:
    return frozenset(t for t in normalized.split() if t in _LEGAL_FORMS)


def _without_legal_forms(normalized: str) -> str:
    return " ".join(t for t in normalized.split() if t not in _LEGAL_FORMS) or normalized


def _similarity(a: str, b: str) -> float:
    """0..100 on normalized names. A legal form on only one name is ignored, so "Sharma
    Electronics Pvt Ltd" finds a ledger named just "Sharma Electronics"; two different legal
    forms (an LLP and a Pvt Ltd) are different legal persons, so they count against a match."""
    if not a or not b:
        return 0.0
    plain = fuzz.token_sort_ratio(a, b)
    forms_a, forms_b = _legal_forms(a), _legal_forms(b)
    if forms_a and forms_b and forms_a != forms_b:
        return plain
    return max(plain, fuzz.token_sort_ratio(_without_legal_forms(a), _without_legal_forms(b)))


def name_similarity(a: str | None, b: str | None) -> float:
    return _similarity(normalize_name(a), normalize_name(b))


def display_name(raw: str | None) -> str:
    """A party name fit for a new ledger: trimmed, without "M/s", title case if ALL CAPS."""
    name = " ".join(_HONORIFIC.sub("", (raw or "").strip()).split())
    if name.isupper():
        name = _LETTER_WORD.sub(lambda m: m[0].capitalize(), name)
    return name[:MAX_NAME_LENGTH].strip()


def unique_ledger_name(name: str, ledgers: Iterable[LedgerInfo], *qualifiers: str | None) -> str:
    """`name` with the first qualifier, or else a number, that no ledger uses yet. Tally keeps
    names and aliases in one namespace, so aliases count as taken too."""
    taken = {n.casefold() for ledger in ledgers for n in (ledger.name, *ledger.aliases)}
    suffixes = [f" ({q})" for q in qualifiers if q and len(q) < MAX_NAME_LENGTH // 2]
    suffixes += [f" ({n})" for n in range(2, 1000)]
    options = (f"{name[: MAX_NAME_LENGTH - len(s)].rstrip()}{s}" for s in suffixes)
    return next(o for o in options if o.casefold() not in taken)


def _group_key(group: str | None) -> str:
    return " ".join((group or "").casefold().replace("&", " and ").split())


@cache
def _placed_group(parent: str | None) -> str:
    """The Tally group a ledger's immediate group stands for, as a group key."""
    key = _group_key(parent)
    if key in _TALLY_GROUPS:
        return key
    return next((_group_key(g) for hint, g in _SUB_GROUP_HINTS if hint.search(key)), key)


def in_group(ledger: LedgerInfo, *groups: str) -> bool:
    return _placed_group(ledger.parent) in {_group_key(g) for g in groups}


def is_party_ledger(ledger: LedgerInfo) -> bool:
    return not in_group(ledger, *NON_PARTY_GROUPS)


def find_ledger(ledgers: Iterable[LedgerInfo], name: str | None) -> LedgerInfo | None:
    """The ledger with this name: exact spelling first, then ignoring case."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    ledgers = list(ledgers)
    exact = next((ledger for ledger in ledgers if ledger.name == wanted), None)
    folded = wanted.casefold()
    return exact or next((ledger for ledger in ledgers if ledger.name.casefold() == folded), None)


def find_name_holder(ledgers: Sequence[LedgerInfo], name: str) -> LedgerInfo | None:
    """The ledger Tally would resolve `name` to: one with that name, else with that alias."""
    folded = name.strip().casefold()
    by_alias = (x for x in ledgers if any(a.casefold() == folded for a in x.aliases))
    return find_ledger(ledgers, name) or next(by_alias, None)


def gstin_conflict(ledger: LedgerInfo, gstin: str | None, gstin_valid: bool) -> bool:
    """True when the party has a valid GSTIN and the ledger carries a different one."""
    theirs = clean_gstin(ledger.gstin)
    return bool(gstin_valid and gstin and theirs and theirs != clean_gstin(gstin))


def top(
    names: Iterable[str | None], exclude: str | None = None, limit: int = MAX_PARTY_CANDIDATES
) -> list[str]:
    """Distinct names in order, without `exclude`, at most `limit` of them."""
    seen = dict.fromkeys(n for n in names if n and n != exclude)
    return list(seen)[:limit]


# -- party -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _NameHit:
    ledger: LedgerInfo
    similarity: float
    method: MatchMethod | None  # None: not a match, maybe still a candidate
    score: float


def _name_hit(query: str, ledger: LedgerInfo) -> _NameHit:
    own = normalize_name(ledger.name)
    if own == query:
        return _NameHit(ledger, 100.0, "exact", EXACT_SCORE)
    aliases = [normalize_name(a) for a in ledger.aliases]
    if query in aliases:
        return _NameHit(ledger, 100.0, "alias", ALIAS_SCORE)
    similarity = max(_similarity(query, n) for n in [own, *aliases])
    if similarity >= STRONG_FUZZY:
        score = min(round(similarity / 100, 4), MAX_FUZZY_SCORE)
        return _NameHit(ledger, similarity, "fuzzy", score)
    if similarity >= WEAK_FUZZY:
        return _NameHit(ledger, similarity, "fuzzy", WEAK_FUZZY_SCORE)
    return _NameHit(ledger, similarity, None, 0.0)


def is_weak_match(match: LedgerMatch) -> bool:
    return match.method == "fuzzy" and match.score <= WEAK_FUZZY_SCORE


def _group_order(ledger: LedgerInfo, preferred_group: str) -> tuple[bool, str]:
    return (not in_group(ledger, preferred_group), ledger.name.casefold())


def _hit_order(hit: _NameHit, preferred_group: str) -> tuple[int, bool, float, str]:
    preferred, name = _group_order(hit.ledger, preferred_group)
    return (_METHOD_TIER[hit.method], preferred, -hit.similarity, name)


def match_party(
    name: str | None,
    gstin: str | None,
    gstin_valid: bool,
    ledgers: Sequence[LedgerInfo],
    preferred_group: str,
) -> LedgerMatch:
    """GSTIN, then exact name, alias and fuzzy name. Ties go to `preferred_group`."""
    gstin = clean_gstin(gstin)
    query = normalize_name(name)
    hits = (
        [_name_hit(query, ledger) for ledger in ledgers if is_party_ledger(ledger)] if query else []
    )
    plausible = sorted(
        (h for h in hits if h.method or h.similarity >= PLAUSIBLE),
        key=lambda h: _hit_order(h, preferred_group),
    )
    plausible_names = [h.ledger.name for h in plausible]

    if is_gstin_shaped(gstin):
        by_gstin = sorted(
            (ledger for ledger in ledgers if clean_gstin(ledger.gstin) == gstin),
            key=lambda ledger: _group_order(ledger, preferred_group),
        )
        if by_gstin:
            best = by_gstin[0].name
            others = [ledger.name for ledger in by_gstin[1:]] + plausible_names
            return LedgerMatch(
                ledger=best, method="gstin", score=1.0, candidates=top(others, exclude=best)
            )

    matches = [
        h for h in plausible if h.method and not gstin_conflict(h.ledger, gstin, gstin_valid)
    ]
    if matches:
        best_hit = matches[0]
        return LedgerMatch(
            ledger=best_hit.ledger.name,
            method=best_hit.method,
            score=best_hit.score,
            candidates=top(plausible_names, exclude=best_hit.ledger.name),
        )
    return LedgerMatch(ledger=None, method="none", score=0.0, candidates=top(plausible_names))


# -- rates -----------------------------------------------------------------------------------


def names_rate(name: str, rate: Decimal) -> bool:
    """Whether a ledger name carries this percentage, e.g. "Purchase @ 18%" for 18."""
    text = re.sub(r"\s+", "", name.casefold())
    digits = format(rate.normalize(), "f")
    tail = r"0*%" if "." in digits else r"(?:\.0+)?%"
    return re.search(rf"(?<![\d.]){re.escape(digits)}{tail}", text) is not None


def _rate_tier(name: str, rate: Decimal | None) -> int:
    """0: the name carries `rate`, 1: it carries no rate, 2: it carries another rate."""
    if rate is not None and names_rate(name, rate):
        return 0
    return 2 if _ANY_RATE.search(re.sub(r"\s+", "", name)) else 1


def _supply_tier(name: str, supply: Supply | None) -> int:
    """0: the name says this supply type (local or inter-state), 1: neither, 2: the other."""
    if supply is None:
        return 1
    other: Supply = "inter" if supply == "intra" else "intra"
    text = name.casefold()
    if _SUPPLY_NAMES[supply].search(text):
        return 0
    return 2 if _SUPPLY_NAMES[other].search(text) else 1


# -- item ------------------------------------------------------------------------------------


def rank_item_ledgers(
    ledgers: Iterable[LedgerInfo],
    direction: Direction,
    rate: Decimal | None,
    supply: Supply | None = None,
) -> list[LedgerInfo]:
    """Purchase/sales ledgers, best first. A ledger named for the other supply type (an
    "Interstate" ledger for a local bill) comes last, since Tally checks the tax against it.
    Then: the invoice's GST rate in the name, the plain "Purchase"/"Sales" ledger, ledgers
    without a rate, the rest; a name saying this supply type first, then A-Z."""
    generic = _GENERIC_ITEM_NAMES[direction]

    def order(ledger: LedgerInfo) -> tuple[bool, int, int, str]:
        rate_tier = _rate_tier(ledger.name, rate)
        is_generic = " ".join(ledger.name.casefold().split()) in generic
        tier = 0 if rate_tier == 0 else 1 if is_generic else rate_tier + 1
        supply_tier = _supply_tier(ledger.name, supply)
        return (supply_tier == 2, tier, supply_tier, ledger.name.casefold())

    return sorted((x for x in ledgers if in_group(x, ITEM_GROUPS[direction])), key=order)


def is_item_misfit(ledger: LedgerInfo, rate: Decimal | None, supply: Supply | None) -> bool:
    """Whether the ledger is named for another GST rate or the other supply type."""
    return _rate_tier(ledger.name, rate) == 2 or _supply_tier(ledger.name, supply) == 2


def can_take_learned_items(ledger: LedgerInfo, direction: Direction, rate: Decimal | None) -> bool:
    """Whether a ledger learned for the party still fits this invoice: it is on the invoice's
    side (a customer who is also a supplier must not get the purchase ledger on a sale) and
    not named for another GST rate."""
    on_side = in_group(ledger, *ITEM_SIDE_GROUPS[direction]) and not is_round_off(ledger)
    return on_side and _rate_tier(ledger.name, rate) != 2


def item_alternatives(
    ledgers: Sequence[LedgerInfo],
    direction: Direction,
    rate: Decimal | None,
    supply: Supply | None = None,
) -> list[str]:
    names = [ledger.name for ledger in rank_item_ledgers(ledgers, direction, rate, supply)]
    if direction == "purchase":
        expenses = (x for x in ledgers if in_group(x, *EXPENSE_GROUPS) and not is_round_off(x))
        names += sorted((x.name for x in expenses), key=str.casefold)
    return names


# -- taxes and round-off ---------------------------------------------------------------------


@dataclass(frozen=True)
class TaxLedgerLookup:
    ledger: LedgerInfo | None
    # Ledgers for this tax and side that were passed over because they name another rate.
    other_rates: tuple[str, ...] = ()


def _rate_rank(name: str, rates: tuple[Decimal, ...] | None) -> int | None:
    """None when the name carries a rate other than `rates`. Otherwise 0 for the first rate,
    1 for no rate, 2 for a later one (CGST ledgers some companies name by the total rate).
    `rates` None: the rate does not matter (cess)."""
    if rates is None:
        return 0
    for i, rate in enumerate(rates):
        if names_rate(name, rate):
            return 0 if i == 0 else 2
    return None if _ANY_RATE.search(re.sub(r"\s+", "", name)) else 1


def _side_of(name: str) -> Direction | Literal["both"] | None:
    text = name.casefold()
    sides = [d for d, pattern in _SIDE_NAMES.items() if pattern.search(text)]
    return sides[0] if len(sides) == 1 else "both" if sides else None


def find_tax_ledger(
    ledgers: Iterable[LedgerInfo],
    direction: Direction,
    tax: str,
    rates: tuple[Decimal, ...] | None,
    rcm: RcmUse = "exclude",
) -> TaxLedgerLookup:
    """An input (purchase) or output (sales) ledger for cgst/sgst/igst/cess under Duties &
    Taxes: one named for that side, else one named for neither side. Within that, one named
    for `rates` (see _rate_rank); a ledger named for another rate is never used. `rcm` says
    whether reverse charge ledgers are left out, preferred, or the only ones wanted."""
    found = [
        x
        for x in ledgers
        if in_group(x, TAX_GROUP)
        and _TAX_NAMES[tax].search(x.name.casefold())
        and _rcm_allowed(x, rcm)
    ]
    passed_over: list[str] = []
    for side in (direction, None):
        pool = [x for x in found if _side_of(x.name) == side]
        ranked = [(rank, x) for x in pool if (rank := _rate_rank(x.name, rates)) is not None]
        if ranked:
            best = min(ranked, key=lambda p: (_rcm_rank(p[1], rcm), p[0], p[1].name.casefold()))
            return TaxLedgerLookup(best[1])
        passed_over += sorted((x.name for x in pool), key=str.casefold)
    return TaxLedgerLookup(None, tuple(passed_over))


def _is_rcm(ledger: LedgerInfo) -> bool:
    return _RCM_NAME.search(ledger.name.casefold()) is not None


def _rcm_allowed(ledger: LedgerInfo, rcm: RcmUse) -> bool:
    return rcm == "prefer" or _is_rcm(ledger) == (rcm == "only")


def _rcm_rank(ledger: LedgerInfo, rcm: RcmUse) -> int:
    return 0 if rcm != "prefer" or _is_rcm(ledger) else 1


def is_round_off(ledger: LedgerInfo) -> bool:
    # Word start, so "Rounding Off" counts and "Ground Rent" does not.
    is_named = _ROUND.search(ledger.name) is not None
    return is_named and not in_group(ledger, *PARTY_GROUPS.values())


def find_round_off_ledger(ledgers: Iterable[LedgerInfo]) -> LedgerInfo | None:
    return min(
        (x for x in ledgers if is_round_off(x)),
        key=lambda x: (not in_group(x, *ROUND_OFF_GROUPS), x.name.casefold()),
        default=None,
    )
