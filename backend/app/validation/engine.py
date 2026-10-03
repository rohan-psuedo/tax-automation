"""Validation engine.

CONTRACT:

DuplicateLookup = Callable[[str | None, str | None, str], list[str]]
    (party_gstin, party_name, invoice_number) -> descriptions of other, non-rejected entries
    for the same party and invoice number in this company (empty list if none). Supplied by
    the caller (it queries the DB); validation itself never touches the database.

def validate(
    invoice: NormalizedInvoice,
    accounting: AccountingResult,
    *,
    always_review: bool,
    auto_create_ledgers: bool,
    find_duplicates: DuplicateLookup,
    today: date,
) -> ValidationReport

Each finding is reported once, at its most likely root cause: a document that is not an
invoice gets only that error, amount cross-checks are skipped while the grand total is
missing or the tax amounts already contradict each other, and a field that already has an
issue is not flagged again for low read confidence.
"""

import math
import re
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.accounting.types import AccountingResult, Direction
from app.schemas.invoice import NormalizedInvoice, Party
from app.validation.types import Issue, Severity, ValidationReport

DuplicateLookup = Callable[[str | None, str | None, str], list[str]]

VALID_GST_RATES = frozenset(
    Decimal(r)
    for r in ("0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40")
)

CENT = Decimal("0.01")
ONE_RUPEE = Decimal("1")
RATE_TOLERANCE = Decimal("0.005")  # 0.5% of the expected tax
OLD_INVOICE_DAYS = 365
GST_START = date(2017, 7, 1)
# Rule 46(b): at most 16 characters, letters, digits, hyphen and slash.
INVOICE_NUMBER_MAX = 16
_INVOICE_NUMBER_RE = re.compile(r"[A-Za-z0-9/-]+")
# What gets typed or read when there is no number at all.
_PLACEHOLDER_NUMBERS = {"na", "n/a", "nil", "none", "null", "-", "--", "0", "tbd", "xxx"}
MAX_DUPLICATES_LISTED = 3
LOW_CONFIDENCE = 0.6
WEAK_ITEM_SCORE = 0.7
# Rule 46: an unregistered customer must be named only from this taxable value upwards.
UNREGISTERED_NAME_LIMIT = Decimal("50000")

# Codes still printed for states whose GST code changed: undivided Andhra Pradesh (28) is
# now 37, and Daman and Diu (25) merged into 26. GSTINs carry the current code.
_SUCCESSOR_STATE = {"25": "26", "28": "37"}

# Fields whose read confidence feeds the overall score and the low_confidence warning.
KEY_FIELDS: dict[str, str] = {
    "invoice_number": "invoice number",
    "invoice_date": "invoice date",
    "grand_total": "grand total",
    "taxable_value": "taxable value",
    "cgst": "CGST amount",
    "sgst": "SGST amount",
    "igst": "IGST amount",
    "seller.gstin": "seller GSTIN",
    "buyer.gstin": "buyer GSTIN",
    "seller.name": "seller name",
    "buyer.name": "buyer name",
}

# Issues that are about more fields than the one they are tagged with; a hard-to-read value
# in any of them is the same finding, so it is not flagged again for low confidence.
_ALSO_COVERS: dict[str, tuple[str, ...]] = {
    "mixed_gst": ("igst", "cgst", "sgst"),
    "cgst_sgst_unequal": ("cgst", "sgst"),
    "same_gstin_both_sides": ("seller.gstin", "buyer.gstin"),
}

_NOT_INVOICE_MESSAGES = {
    "proforma": (
        "This is a proforma invoice, which is a quotation and must not be posted. "
        "Reject it and upload the final tax invoice."
    ),
    "receipt": (
        "This is a payment receipt, not an invoice. "
        "Reject it and upload the invoice it was paid against."
    ),
}


def _cents(amount: Decimal) -> Decimal:
    # The precision grows with the amount, so a huge misread figure is reported like any
    # other instead of raising InvalidOperation.
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, amount.adjusted() + 3)
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def format_inr(amount: Decimal) -> str:
    """₹ with Indian digit grouping: 1234567 -> "₹12,34,567.00"."""
    value = _cents(amount)
    whole, fraction = f"{abs(value):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    sign = "-" if value < 0 else ""
    return f"{sign}₹{','.join([*groups, tail])}.{fraction}"


def _format_rate(rate: Decimal) -> str:
    return f"{rate.normalize():f}"


def _format_date(d: date) -> str:
    return d.strftime("%d %b %Y")


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _counterparty_key(direction: Direction) -> str:
    return "buyer" if direction == "sales" else "seller"


def _counterparty_role(direction: Direction) -> str:
    return "customer" if direction == "sales" else "supplier"


def _counterparty(invoice: NormalizedInvoice, direction: Direction) -> Party:
    return invoice.buyer if direction == "sales" else invoice.seller


def _issue(code: str, severity: Severity, message: str, field: str | None = None) -> Issue:
    return Issue(code=code, severity=severity, message=message, field=field)


def _not_invoice(invoice: NormalizedInvoice) -> Issue | None:
    if not invoice.is_invoice:
        return _issue(
            "not_invoice",
            "error",
            "This document does not look like an invoice. "
            "Reject it, or mark it as an invoice if it really is one.",
            "is_invoice",
        )
    if message := _NOT_INVOICE_MESSAGES.get(invoice.document_type):
        return _issue("not_invoice", "error", message, "document_type")
    return None


def _name_optional(invoice: NormalizedInvoice, accounting: AccountingResult) -> bool:
    """A sale to an unregistered customer may lack a name (a walk-in or cash sale): the law
    allows it below the limit, and a reviewer who picked the ledger has settled the party."""
    if accounting.direction != "sales" or not _blank(invoice.buyer.gstin):
        return False
    if accounting.party.method == "choice":
        return True
    value = invoice.taxable_value if invoice.taxable_value is not None else invoice.grand_total
    return value is not None and abs(value) < UNREGISTERED_NAME_LIMIT


def _placeholder_number(number: str | None) -> bool:
    return not _blank(number) and number.strip().casefold() in _PLACEHOLDER_NUMBERS


def _missing_fields(invoice: NormalizedInvoice, accounting: AccountingResult) -> list[Issue]:
    direction = accounting.direction
    issues: list[Issue] = []
    if _blank(invoice.invoice_number):
        issues.append(
            _issue(
                "missing_invoice_number",
                "error",
                "The invoice number is missing. Type it in from the document.",
                "invoice_number",
            )
        )
    elif _placeholder_number(invoice.invoice_number):
        issues.append(
            _issue(
                "missing_invoice_number",
                "error",
                f'The invoice number "{invoice.invoice_number.strip()}" is a placeholder, not a '
                "real number. Type in the number printed on the document.",
                "invoice_number",
            )
        )
    if invoice.invoice_date is None:
        issues.append(
            _issue(
                "missing_invoice_date",
                "error",
                "The invoice date is missing. Type it in from the document.",
                "invoice_date",
            )
        )
    if invoice.grand_total is None:
        issues.append(
            _issue(
                "missing_grand_total",
                "error",
                "The grand total is missing. Type it in from the document.",
                "grand_total",
            )
        )
    if _blank(_counterparty(invoice, direction).name) and not _name_optional(invoice, accounting):
        issues.append(
            _issue(
                "missing_party_name",
                "error",
                f"The {_counterparty_role(direction)} name is missing. "
                "Type it in from the document.",
                f"{_counterparty_key(direction)}.name",
            )
        )
    return issues


def _invoice_number_format(invoice: NormalizedInvoice) -> list[Issue]:
    number = (invoice.invoice_number or "").strip()
    if not number or _placeholder_number(number):
        return []
    too_long = len(number) > INVOICE_NUMBER_MAX
    odd_chars = not _INVOICE_NUMBER_RE.fullmatch(number)
    if not (too_long or odd_chars):
        return []
    found = f"{len(number)} characters" if too_long else "other characters"
    return [
        _issue(
            "invoice_number_format",
            "warning",
            f'The invoice number "{number}" has {found}; GST invoice numbers have at most '
            f"{INVOICE_NUMBER_MAX} letters, digits, hyphens and slashes. Extra text such as a "
            "date or a label may have been read into it. Check it against the document.",
            "invoice_number",
        )
    ]


def _dates(invoice: NormalizedInvoice, today: date) -> list[Issue]:
    d = invoice.invoice_date
    if d is None:
        return []
    if d < GST_START:
        return [
            _issue(
                "pre_gst_date",
                "warning",
                f"The invoice is dated {_format_date(d)}, before GST began on 1 July 2017. "
                "The year was probably misread; check the date against the document.",
                "invoice_date",
            )
        ]
    if d > today:
        return [
            _issue(
                "future_date",
                "error",
                f"The invoice date {_format_date(d)} is in the future. "
                "Check the date against the document; the day and month may have been swapped.",
                "invoice_date",
            )
        ]
    age = (today - d).days
    if age > OLD_INVOICE_DAYS:
        return [
            _issue(
                "old_invoice",
                "warning",
                f"The invoice is dated {_format_date(d)}, {age} days ago. "
                "Check the date, and check that this invoice has not been entered before.",
                "invoice_date",
            )
        ]
    return []


def _gstins(invoice: NormalizedInvoice, direction: Direction) -> list[Issue]:
    seller_gstin = (invoice.seller.gstin or "").strip().upper()
    if seller_gstin and seller_gstin == (invoice.buyer.gstin or "").strip().upper():
        # Nobody invoices themselves: one side was copied from the other (usually the
        # company's own GSTIN into the other party's box).
        key = _counterparty_key(direction)
        return [
            _issue(
                "same_gstin_both_sides",
                "error",
                f"The seller and the buyer have the same GSTIN {seller_gstin}. One of them was "
                f"probably copied by mistake; check the {key}'s GSTIN against the document.",
                f"{key}.gstin",
            )
        ]
    issues: list[Issue] = []
    for key, party in (("seller", invoice.seller), ("buyer", invoice.buyer)):
        if not _blank(party.gstin) and not party.gstin_valid:
            issues.append(
                _issue(
                    "invalid_gstin",
                    "error",
                    f"The {key} GSTIN {party.gstin} is not a valid GSTIN; a character was "
                    "probably misread. Compare it with the document and correct it.",
                    f"{key}.gstin",
                )
            )
    return issues


def _direction(accounting: AccountingResult) -> list[Issue]:
    # The accounting engine says "assumed" when neither party on the invoice is the company.
    if "assum" not in accounting.direction_reason.lower():
        return []
    return [
        _issue(
            "company_not_on_invoice",
            "warning",
            "Your company's name or GSTIN was not found on this invoice, so it was treated as "
            f"a {accounting.direction}. Check that the invoice belongs to this company and "
            "that the direction is right.",
        )
    ]


def _charges_cgst_sgst(invoice: NormalizedInvoice) -> bool:
    # Not "> 0": credit notes are often printed with negative amounts.
    return invoice.cgst != 0 or invoice.sgst != 0


def _mixed_gst(invoice: NormalizedInvoice) -> list[Issue]:
    if invoice.igst != 0 and _charges_cgst_sgst(invoice):
        return [
            _issue(
                "mixed_gst",
                "error",
                f"Both IGST ({format_inr(invoice.igst)}) and CGST/SGST "
                f"({format_inr(invoice.cgst)} / {format_inr(invoice.sgst)}) are charged, which "
                "cannot happen on one invoice. Check the tax amounts against the document.",
                "igst",
            )
        ]
    return []


def _cgst_sgst(invoice: NormalizedInvoice) -> list[Issue]:
    gap = abs(invoice.cgst - invoice.sgst)
    if gap <= CENT:
        return []
    amounts = (
        f"CGST ({format_inr(invoice.cgst)}) and SGST ({format_inr(invoice.sgst)}) should be "
        f"equal but differ by {format_inr(gap)}."
    )
    if gap > ONE_RUPEE:
        return [
            _issue(
                "cgst_sgst_unequal",
                "error",
                f"{amounts} Check both tax amounts against the document.",
                "cgst",
            )
        ]
    return [
        _issue(
            "cgst_sgst_unequal",
            "warning",
            f"{amounts} This is usually rounding; check both tax amounts before posting.",
            "cgst",
        )
    ]


def _state_label(code: str, invoice: NormalizedInvoice) -> str:
    for party in (invoice.seller, invoice.buyer):
        if party.state_code == code and party.state:
            return f"{party.state} ({code})"
    return f"state {code}"


def _current_state(code: str) -> str:
    return _SUCCESSOR_STATE.get(code, code)


def _gst_type(invoice: NormalizedInvoice) -> list[Issue]:
    supplier_state = invoice.seller.state_code
    supply_state = invoice.place_of_supply_code or invoice.buyer.state_code
    if not supplier_state or not supply_state:
        return []
    same_state = _current_state(supplier_state) == _current_state(supply_state)
    if same_state and invoice.igst != 0:
        return [
            _issue(
                "wrong_gst_type",
                "warning",
                "IGST is charged, but the seller and the place of supply are both in "
                f"{_state_label(supplier_state, invoice)}; a supply within one state should "
                "carry CGST and SGST unless the buyer is an SEZ unit or it is an export. "
                "Check the tax type and the place of supply.",
                "place_of_supply_code",
            )
        ]
    if not same_state and _charges_cgst_sgst(invoice):
        return [
            _issue(
                "wrong_gst_type",
                "warning",
                "CGST and SGST are charged, but the seller is in "
                f"{_state_label(supplier_state, invoice)} and the place of supply is "
                f"{_state_label(supply_state, invoice)}; a supply between states should carry "
                "IGST. Check the tax type and the place of supply.",
                "place_of_supply_code",
            )
        ]
    return []


def _round_off(invoice: NormalizedInvoice) -> list[Issue]:
    if abs(invoice.round_off) <= ONE_RUPEE:
        return []
    return [
        _issue(
            "large_round_off",
            "warning",
            f"The round off is {format_inr(invoice.round_off)}, which is more than ₹1.00. "
            "Check the round off and the grand total against the document.",
            "round_off",
        )
    ]


def _invalid_rates(invoice: NormalizedInvoice) -> list[Issue]:
    # Keyed by value so that 18 and 18.00 count as one rate.
    bad: dict[Decimal, None] = {}
    for line in invoice.lines:
        if line.gst_rate is not None and line.gst_rate not in VALID_GST_RATES:
            bad.setdefault(line.gst_rate, None)
    return [
        _issue(
            "invalid_gst_rate",
            "warning",
            f"A line has a GST rate of {_format_rate(rate)}%, which is not a valid GST rate. "
            "Check the rate against the document.",
            "lines",
        )
        for rate in bad
    ]


def _lines_total(invoice: NormalizedInvoice) -> list[Issue]:
    values = [line.taxable_value for line in invoice.lines if line.taxable_value is not None]
    if not values or invoice.taxable_value is None:
        return []
    lines_total = sum(values, Decimal("0"))
    if abs(lines_total - invoice.taxable_value) <= ONE_RUPEE:
        return []
    return [
        _issue(
            "lines_total_mismatch",
            "warning",
            f"The line items add up to {format_inr(lines_total)}, but the taxable value is "
            f"{format_inr(invoice.taxable_value)}. Check the line amounts and the taxable value "
            "against the document.",
            "taxable_value",
        )
    ]


def _totals(invoice: NormalizedInvoice) -> list[Issue]:
    if invoice.taxable_value is None or invoice.grand_total is None:
        return []
    computed = invoice.taxable_value + invoice.total_tax + invoice.round_off
    if abs(computed - invoice.grand_total) <= ONE_RUPEE:
        return []
    # Under reverse charge the recipient pays the tax, so the total often leaves it out and
    # the tax is printed only for information.
    untaxed = invoice.taxable_value + invoice.round_off
    if invoice.reverse_charge and abs(untaxed - invoice.grand_total) <= ONE_RUPEE:
        return []
    parts = (
        f"taxable value {format_inr(invoice.taxable_value)} + tax {format_inr(invoice.total_tax)}"
    )
    if invoice.round_off:
        parts += f" + round off {format_inr(invoice.round_off)}"
    message = (
        f"The amounts do not add up: {parts} = {format_inr(computed)}, but the grand total "
        f"is {format_inr(invoice.grand_total)}."
    )
    if invoice.reverse_charge:
        message += (
            " Under reverse charge the grand total may also leave the tax out, which would "
            f"make it {format_inr(untaxed)}."
        )
    message += " Check these amounts against the document."
    return [_issue("totals_mismatch", "error", message, "grand_total")]


def _tax_rate(invoice: NormalizedInvoice) -> list[Issue]:
    charged = invoice.cgst + invoice.sgst + invoice.igst
    if invoice.reverse_charge and charged == 0:
        return []  # the recipient pays the tax, so the supplier rightly charged none
    rated = [line for line in invoice.lines if line.taxable_value is not None]
    if not rated or any(line.gst_rate is None for line in rated):
        return []
    expected = _cents(
        sum((line.taxable_value * line.gst_rate / 100 for line in rated), Decimal("0"))
    )
    tolerance = max(ONE_RUPEE, abs(expected) * RATE_TOLERANCE)
    if abs(charged - expected) <= tolerance:
        return []
    return [
        _issue(
            "tax_rate_mismatch",
            "warning",
            f"The GST rates on the lines give tax of {format_inr(expected)}, but the invoice "
            f"charges {format_inr(charged)}. Check the GST rates and the tax amounts against "
            "the document.",
            "lines",
        )
    ]


def _has_error(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def _tax_heads(invoice: NormalizedInvoice) -> list[Issue]:
    """Checks how the tax is split. One misread tax figure is reported at its first symptom:
    mixed GST explains unequal halves and a wrong tax type, and unequal halves explain a
    wrong tax type (IGST read into the CGST column)."""
    if mixed := _mixed_gst(invoice):
        return mixed
    if _has_error(unequal := _cgst_sgst(invoice)):
        return unequal
    return unequal + _gst_type(invoice)


def _amounts(invoice: NormalizedInvoice) -> list[Issue]:
    heads = _tax_heads(invoice)
    bad_rates = _invalid_rates(invoice)
    issues = heads + _round_off(invoice) + bad_rates
    if invoice.grand_total is None:
        return issues
    lines = _lines_total(invoice)
    issues += lines
    if _has_error(heads):
        return issues  # the totals cannot add up until the tax amounts are corrected
    totals = _totals(invoice)
    issues += totals
    # The rate check only means something once the amounts and rates are themselves sound;
    # line values that disagree with the taxable value would give a wrong expected tax.
    if not (bad_rates or lines or totals):
        issues += _tax_rate(invoice)
    return issues


def _document_flags(invoice: NormalizedInvoice, accounting: AccountingResult) -> list[Issue]:
    issues: list[Issue] = []
    if invoice.reverse_charge:
        issues.append(
            _issue(
                "reverse_charge",
                "warning",
                "This invoice is under reverse charge, so the tax is payable by the recipient. "
                "Check that the entry books the tax correctly before posting.",
                "reverse_charge",
            )
        )
    if invoice.document_type in ("credit_note", "debit_note"):
        kind = accounting.voucher_kind.value.replace("_", " ")
        document = invoice.document_type.replace("_", " ")
        issues.append(
            _issue(
                "note_document",
                "warning",
                f"This document is a {document} and is set to post as a {kind} voucher. "
                "Check the voucher type before posting.",
                "document_type",
            )
        )
    return issues


def _duplicates(
    invoice: NormalizedInvoice, direction: Direction, find_duplicates: DuplicateLookup
) -> list[Issue]:
    number = (invoice.invoice_number or "").strip()
    if not number:
        return []
    party = _counterparty(invoice, direction)
    gstin = (party.gstin or "").strip() or None
    name = (party.name or "").strip() or None
    others = find_duplicates(gstin, name, number)
    if not others:
        return []
    who = name or f"the same {_counterparty_role(direction)}"
    listed = "; ".join(others[:MAX_DUPLICATES_LISTED])
    if len(others) > MAX_DUPLICATES_LISTED:
        listed += f"; and {len(others) - MAX_DUPLICATES_LISTED} more"
    return [
        _issue(
            "duplicate_invoice",
            "error",
            f'Invoice number "{number}" from {who} has already been entered: {listed}. '
            "Reject this entry if it is a duplicate.",
            "invoice_number",
        )
    ]


# Accounting problems that restate a root cause validation reports in its own words, keyed by
# the accounting problem code, with the validation error codes that already cover it.
_COVERED_BY: dict[str, frozenset[str]] = {
    "not_accounting_document": frozenset({"not_invoice"}),
    "missing_invoice_date": frozenset({"missing_invoice_date"}),
    "missing_grand_total": frozenset({"missing_grand_total"}),
    "missing_party_name": frozenset({"missing_party_name", "same_gstin_both_sides"}),
    # The totals can't add up while a tax amount is misread or the total is missing.
    "totals_mismatch": frozenset(
        {"totals_mismatch", "mixed_gst", "cgst_sgst_unequal", "missing_grand_total"}
    ),
}
# An absurd amount explains every sum built on it.
_EXPLAINED_BY_IMPLAUSIBLE = frozenset(
    {"totals_mismatch", "lines_total_mismatch", "tax_rate_mismatch", "large_round_off"}
)


def _voucher_problems(accounting: AccountingResult, issues: list[Issue]) -> list[Issue]:
    reported = {i.code for i in issues if i.severity == "error"}
    return [
        _issue(problem.code, "error", problem.message, problem.field)
        for problem in accounting.problems
        if not (_COVERED_BY.get(problem.code, frozenset()) & reported)
    ]


def _party_ledger(accounting: AccountingResult, auto_create_ledgers: bool) -> list[Issue]:
    party, proposed = accounting.party, accounting.proposed_party
    if party.method == "none" and proposed is not None:
        if auto_create_ledgers:
            return [
                _issue(
                    "new_party_ledger",
                    "warning",
                    f'The party is not in Tally yet. The ledger "{proposed.name}" will be '
                    f"created under {proposed.parent_group} when this entry is posted.",
                    "party_ledger",
                )
            ]
        return [
            _issue(
                "party_ledger_needs_approval",
                "error",
                f'The party is not in Tally yet. Approve creating the ledger "{proposed.name}" '
                f"under {proposed.parent_group}, or pick an existing ledger.",
                "party_ledger",
            )
        ]
    # Any name-only match gets a look: legal-form words ("Pvt Ltd", "LLP") are ignored when
    # matching, so a different firm with the same trade name can score as certain.
    if party.method == "fuzzy" and party.ledger:
        return [
            _issue(
                "weak_party_match",
                "warning",
                f'The party was matched by a similar name to the ledger "{party.ledger}". '
                "Confirm it is the same party, or pick the right ledger.",
                "party_ledger",
            )
        ]
    return []


def _item_ledger(accounting: AccountingResult) -> list[Issue]:
    item = accounting.item
    if item.method != "default" or item.score >= WEAK_ITEM_SCORE:
        return []
    ledger = f'"{item.ledger}"' if item.ledger else "ledger"
    return [
        _issue(
            "weak_item_ledger",
            "warning",
            f"The {accounting.direction} ledger {ledger} was used because nothing better "
            f"matched. Check that it is the right {accounting.direction} ledger for these items.",
            "item_ledger",
        )
    ]


def _field_value(invoice: NormalizedInvoice, path: str) -> object:
    value: object = invoice
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _shown(value: object) -> str:
    if isinstance(value, Decimal):
        return f" ({format_inr(value)})"
    if isinstance(value, date):
        return f" ({_format_date(value)})"
    if isinstance(value, str) and value.strip():
        return f' ("{value.strip()}")'
    return ""


def _flagged_fields(issues: list[Issue]) -> set[str | None]:
    flagged = {i.field for i in issues}
    for issue in issues:
        flagged.update(_ALSO_COVERS.get(issue.code, ()))
    return flagged


def _low_confidence(invoice: NormalizedInvoice, flagged: set[str | None]) -> list[Issue]:
    return [
        _issue(
            "low_confidence",
            "warning",
            f"The {label}{_shown(_field_value(invoice, path))} was hard to read on the "
            "document. Check it against the invoice.",
            path,
        )
        for path, label in KEY_FIELDS.items()
        if path not in flagged and invoice.confidence.get(path, 1.0) < LOW_CONFIDENCE
    ]


def _confidence(invoice: NormalizedInvoice, accounting: AccountingResult) -> float:
    scores = [invoice.confidence[path] for path in KEY_FIELDS if path in invoice.confidence]
    scores += [accounting.party.score, accounting.item.score]
    # A non-finite score is unknown, so it counts as 0 rather than vanishing from min():
    # min() over a NaN gives NaN or ignores it depending on order, and min(1.0, nan) is 1.0.
    clean = [min(1.0, max(0.0, s)) if math.isfinite(s) else 0.0 for s in scores]
    return min(clean, default=1.0)


def _find_issues(
    invoice: NormalizedInvoice,
    accounting: AccountingResult,
    auto_create_ledgers: bool,
    find_duplicates: DuplicateLookup,
    today: date,
) -> list[Issue]:
    if (not_invoice := _not_invoice(invoice)) is not None:
        return [not_invoice]  # everything else is noise until the document is confirmed
    direction = accounting.direction
    gstins = _gstins(invoice, direction)
    # With one GSTIN on both sides, the party picked or proposed is a guess built on the
    # mistake (often the company itself); that is reported once, as the GSTIN error.
    parties_unknown = any(i.code == "same_gstin_both_sides" for i in gstins)
    amounts = _amounts(invoice)
    if any(p.code == "implausible_amount" for p in accounting.problems):
        amounts = [i for i in amounts if i.code not in _EXPLAINED_BY_IMPLAUSIBLE]
    issues = [
        *_missing_fields(invoice, accounting),
        *_invoice_number_format(invoice),
        *_dates(invoice, today),
        *gstins,
        *_direction(accounting),
        *amounts,
        *_document_flags(invoice, accounting),
        *_duplicates(invoice, direction, find_duplicates),
    ]
    issues += _voucher_problems(accounting, issues)
    if not parties_unknown:
        issues += _party_ledger(accounting, auto_create_ledgers)
    issues += _item_ledger(accounting)
    return issues + _low_confidence(invoice, _flagged_fields(issues))


def validate(
    invoice: NormalizedInvoice,
    accounting: AccountingResult,
    *,
    always_review: bool,
    auto_create_ledgers: bool,
    find_duplicates: DuplicateLookup,
    today: date,
) -> ValidationReport:
    issues = _find_issues(invoice, accounting, auto_create_ledgers, find_duplicates, today)
    issues.sort(key=lambda i: i.severity != "error")  # stable: errors first, order kept
    needs_review = always_review or bool(issues)
    return ValidationReport(
        issues=issues,
        confidence=_confidence(invoice, accounting),
        route="needs_review" if needs_review else "ready",
    )
