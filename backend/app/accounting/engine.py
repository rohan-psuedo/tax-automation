"""Accounting engine: NormalizedInvoice -> balanced CanonicalTransaction.

CONTRACT:

def build_voucher(
    invoice: NormalizedInvoice,
    ctx: AccountingContext,
    choices: LedgerChoices | None = None,
    voucher_id: uuid.UUID | None = None,
) -> AccountingResult

- Pure function. Never raises for bad data: problems go into AccountingResult.problems (plain
  sentences) and transaction is None. Party/item/tax matches are still reported.
- voucher_id: reused as CanonicalTransaction.id so re-running after edits keeps the same id
  (posting idempotency depends on it).
- Direction (unless choices.direction): the company's GSTIN as seller -> sales, as buyer ->
  purchase; else the company name against seller/buyer names; else purchase is assumed.
  The counterparty is the buyer for sales and the seller for purchases. A debit note the
  company issued to a supplier (a buyer found under Sundry Creditors, or any buyer when the
  reviewer marks the note as a purchase) is a purchase return: purchase side, the buyer is
  the counterparty.
- Voucher kind: invoices -> PURCHASE/SALES. credit_note -> CREDIT_NOTE (sales side, reverses
  a sale) or DEBIT_NOTE (purchase side, a purchase return). debit_note -> DEBIT_NOTE with
  sales-style entries (sales side), DEBIT_NOTE reversing a purchase (a purchase return the
  company issued) or PURCHASE (purchase side). Proformas, receipts and non-invoices get a
  problem and no transaction.
- Party ledger: choice > GSTIN > exact name > alias > fuzzy (app.accounting.matching). No
  match, or only a weak fuzzy one -> proposed_party (a new Sundry Creditors/Debtors ledger,
  named so it clashes with no ledger name or alias); the voucher is still built with the
  matched or proposed ledger on its party entry, and validation decides whether to post.
  choices.create_party_ledger approves the proposal (method "choice").
- Item ledger: choice > learned for this party (while it is on the invoice's side and not
  named for another GST rate) > default purchase/sales ledger, preferring one named for the
  invoice's GST rate and supply type (local or inter-state).
- Taxes go to input (purchase side) or output (sales side) ledgers under Duties & Taxes. A
  ledger named for another GST rate is never used; if only such ledgers exist, that is a
  problem. A difference of up to Rs 1.00 between the total and taxable + tax is round-off.
- Reverse charge: the party is owed the taxable value only (the bill may print its total with
  or without the tax). Purchase side: the tax is debited to input ledgers and credited to a
  reverse charge payable ledger (tax_ledgers keys "cgst_rcm" etc.); sales side: the customer
  pays the tax to the government, so no tax is booked.
- Purchase-style entries: Dr item, Dr taxes, Cr party (bill_ref = invoice number). Sales
  style mirrors it; credit notes and purchase returns reverse their side's style.
"""

import re
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import ValidationError

from app.accounting.matching import (
    ALIAS_SCORE,
    EXACT_SCORE,
    MAX_ITEM_CANDIDATES,
    MAX_PARTY_CANDIDATES,
    PARTY_GROUPS,
    RcmUse,
    Supply,
    can_take_learned_items,
    clean_gstin,
    display_name,
    find_ledger,
    find_name_holder,
    find_round_off_ledger,
    find_tax_ledger,
    gstin_conflict,
    in_group,
    is_gstin_shaped,
    is_item_misfit,
    is_party_ledger,
    is_weak_match,
    item_alternatives,
    match_party,
    name_similarity,
    rank_item_ledgers,
    top,
    unique_ledger_name,
)
from app.accounting.types import (
    AccountingContext,
    AccountingProblem,
    AccountingResult,
    Direction,
    LedgerChoices,
    LedgerMatch,
    MatchMethod,
)
from app.normalization.gst import STATE_CODES
from app.schemas.canonical import (
    CENT,
    CanonicalTransaction,
    Entry,
    GstDetails,
    LedgerRef,
    ProposedLedger,
    Side,
    VoucherKind,
)
from app.schemas.invoice import NormalizedInvoice, Party

TAXES = ("cgst", "sgst", "igst", "cess")
_TAX_LABELS = {"cgst": "CGST", "sgst": "SGST", "igst": "IGST", "cess": "Cess"}
_SIDE_LABELS: dict[Direction, str] = {"purchase": "Input", "sales": "Output"}
_ROUND_OFF_LIMIT = Decimal("1.00")
_MAX_AMOUNT = Decimal("1e13")  # far above any real invoice, far below Decimal's precision
_COMPANY_NAME_MATCH = 90
_SHORT_DESCRIPTION = 60
_SHOWN_LEDGERS = 3
# Total GST rates an effective rate (tax / taxable) is snapped to when lines carry no rate.
_STANDARD_RATES = tuple(
    Decimal(r)
    for r in ("0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40")
)
_RATE_TOLERANCE = Decimal("0.1")
_RATE_PLACES = Decimal("0.001")
_SURE_PARTY_METHODS: frozenset[MatchMethod] = frozenset({"gstin", "exact", "alias"})

CompanyRole = Literal["seller", "buyer"]
# (ledger, posted on the item's side, amount): every entry but the party's.
_Posting = tuple[str, bool, Decimal]


@dataclass(frozen=True)
class _Amounts:
    taxable: Decimal
    taxes: dict[str, Decimal]
    party: Decimal  # the total, less any tax the company pays itself under reverse charge
    round_off: Decimal  # signed: party - taxable - tax charged by the party


def _problem(code: str, message: str, field: str | None = None) -> AccountingProblem:
    return AccountingProblem(code=code, message=message, field=field)


def build_voucher(
    invoice: NormalizedInvoice,
    ctx: AccountingContext,
    choices: LedgerChoices | None = None,
    voucher_id: uuid.UUID | None = None,
) -> AccountingResult:
    choices = choices or LedgerChoices()
    role, evidence = _company_role(invoice, ctx)
    direction, reason = _direction(invoice, ctx, choices, role, evidence)
    returned = (
        direction == "purchase" and role == "seller" and invoice.document_type == "debit_note"
    )
    kind = VoucherKind.DEBIT_NOTE if returned else _voucher_kind(invoice, direction)
    if refusal := _refusal(invoice) or _implausible_amount(invoice):
        return AccountingResult(
            direction=direction,
            direction_reason=reason,
            voucher_kind=kind,
            party=LedgerMatch(ledger=None, method="none", score=0.0),
            item=LedgerMatch(ledger=None, method="none", score=0.0),
            problems=[refusal],
        )

    problems: list[AccountingProblem] = []
    counterparty = invoice.buyer if direction == "sales" or returned else invoice.seller
    party, proposed = _resolve_party(counterparty, direction, ctx, choices, problems)
    party_name = party.ledger or (proposed.name if proposed else None)
    taxes = {t: _money(getattr(invoice, t).copy_abs()) for t in TAXES}  # notes print negatives
    amounts = _amounts(invoice, taxes, problems)
    rate = _gst_rate(invoice, amounts.taxable if amounts else None)
    supply = _supply(taxes)
    item = _resolve_item(direction, ctx, choices, party_name, rate, supply, problems)
    rcm = invoice.reverse_charge
    ledgers = _resolve_tax_ledgers(taxes, direction, rcm, ctx, rate, problems)
    if amounts and amounts.round_off:
        _resolve_round_off(amounts.round_off, ctx, ledgers, problems)
    if invoice.invoice_date is None:
        problems.append(
            _problem(
                "missing_invoice_date",
                "The invoice date is missing. Enter it on the review screen.",
                "invoice_date",
            )
        )

    transaction = None
    if not problems and amounts and party_name and item.ledger:
        to_create = proposed if proposed and proposed.name == party_name else None
        party_ref = LedgerRef(name=party_name, proposed=to_create)
        postings = _postings(amounts, item.ledger, ledgers, direction, rcm)
        transaction = _transaction(
            invoice,
            direction,
            returned,
            kind,
            party_ref,
            postings,
            amounts.party,
            _gst_details(invoice, counterparty, ctx),
            voucher_id,
            problems,
        )
    return AccountingResult(
        direction=direction,
        direction_reason=reason,
        voucher_kind=kind,
        party=party,
        proposed_party=proposed,
        item=item,
        tax_ledgers=ledgers,
        transaction=transaction,
        problems=problems,
    )


# -- direction and kind ----------------------------------------------------------------------


def _company_role(
    invoice: NormalizedInvoice, ctx: AccountingContext
) -> tuple[CompanyRole | None, str]:
    """Where the company appears on the document, and the evidence for it."""
    company = clean_gstin(ctx.company_gstin)
    if company and company == clean_gstin(invoice.seller.gstin):
        return "seller", "The seller's GSTIN is the company's own GSTIN"
    if company and company == clean_gstin(invoice.buyer.gstin):
        return "buyer", "The buyer's GSTIN is the company's own GSTIN"
    as_seller = name_similarity(ctx.company_name, invoice.seller.name)
    as_buyer = name_similarity(ctx.company_name, invoice.buyer.name)
    if as_seller >= _COMPANY_NAME_MATCH and as_seller > as_buyer:
        return "seller", "The seller's name matches the company name"
    if as_buyer >= _COMPANY_NAME_MATCH:
        return "buyer", "The buyer's name matches the company name"
    return None, ""


def _direction(
    invoice: NormalizedInvoice,
    ctx: AccountingContext,
    choices: LedgerChoices,
    role: CompanyRole | None,
    evidence: str,
) -> tuple[Direction, str]:
    if choices.direction:
        noun = "sale" if choices.direction == "sales" else "purchase"
        return choices.direction, f"The reviewer marked this document as a {noun}."
    if role == "seller" and invoice.document_type == "debit_note":
        if _is_supplier(invoice.buyer, ctx):
            return "purchase", (
                f"{evidence}, and the buyer is a supplier in Tally, so this debit note is a "
                "purchase return."
            )
    if role == "seller":
        return "sales", f"{evidence}, so this is a sale."
    if role == "buyer":
        return "purchase", f"{evidence}, so this is a purchase."
    return "purchase", (
        "The company was not found on the document as seller or buyer, so purchase was "
        "assumed. Change the direction if this is a sale."
    )


def _is_supplier(party: Party, ctx: AccountingContext) -> bool:
    """Whether the party is surely a ledger under Sundry Creditors, with no customer ledger
    matching it as well (customers are preferred on a tie)."""
    customer_first = PARTY_GROUPS["sales"]
    match = match_party(party.name, party.gstin, party.gstin_valid, ctx.ledgers, customer_first)
    ledger = find_ledger(ctx.ledgers, match.ledger)
    sure = match.method in _SURE_PARTY_METHODS
    return bool(ledger and sure and in_group(ledger, PARTY_GROUPS["purchase"]))


def _voucher_kind(invoice: NormalizedInvoice, direction: Direction) -> VoucherKind:
    sales = direction == "sales"
    match invoice.document_type:
        case "credit_note":
            return VoucherKind.CREDIT_NOTE if sales else VoucherKind.DEBIT_NOTE
        case "debit_note":
            return VoucherKind.DEBIT_NOTE if sales else VoucherKind.PURCHASE
        case "receipt":
            return VoucherKind.RECEIPT if sales else VoucherKind.PAYMENT
    return VoucherKind.SALES if sales else VoucherKind.PURCHASE


def _refusal(invoice: NormalizedInvoice) -> AccountingProblem | None:
    if invoice.document_type == "receipt":
        return _not_accounting(
            "This document is a receipt. Receipts are not supported yet, so no voucher was "
            "made; enter it in Tally by hand."
        )
    if invoice.document_type == "proforma":
        return _not_accounting(
            "This document is a proforma invoice, which is not an accounting document, so no "
            "voucher was made. Upload the final tax invoice instead."
        )
    if not invoice.is_invoice:
        return _not_accounting(
            "This document is not an invoice, so no voucher was made. If it is one, mark it "
            "as an invoice on the review screen."
        )
    return None


def _not_accounting(message: str) -> AccountingProblem:
    return _problem("not_accounting_document", message, "document_type")


def _implausible_amount(invoice: NormalizedInvoice) -> AccountingProblem | None:
    values = [invoice.taxable_value, invoice.grand_total, *(getattr(invoice, t) for t in TAXES)]
    values += [v for x in invoice.lines for v in (x.taxable_value, x.gst_rate)]
    # copy_abs() needs no arithmetic context, so a huge exponent cannot overflow the check.
    if any(v is not None and v.copy_abs() >= _MAX_AMOUNT for v in values):
        return _problem(
            "implausible_amount",
            "An amount on the invoice is too large to be right. Check the amounts on the "
            "review screen.",
            "grand_total",
        )
    return None


# -- ledgers ---------------------------------------------------------------------------------


def _resolve_party(
    party: Party,
    direction: Direction,
    ctx: AccountingContext,
    choices: LedgerChoices,
    problems: list[AccountingProblem],
) -> tuple[LedgerMatch, ProposedLedger | None]:
    group = PARTY_GROUPS[direction]
    auto = match_party(party.name, party.gstin, party.gstin_valid, ctx.ledgers, group)
    if choices.party_ledger:
        chosen = find_ledger(ctx.ledgers, choices.party_ledger)
        if chosen:
            others = top([auto.ledger, *auto.candidates], chosen.name, MAX_PARTY_CANDIDATES)
            match = LedgerMatch(ledger=chosen.name, method="choice", score=1.0, candidates=others)
            return match, None
        problems.append(
            _problem(
                "unknown_party_ledger",
                f"The ledger '{choices.party_ledger}' picked for the party does not exist in "
                "Tally. "
                "Pick another ledger or sync ledgers.",
                "party_ledger",
            )
        )
    # A weak match may be a different firm with a similar name, so a new ledger is still
    # proposed for the reviewer to approve instead.
    if auto.ledger and not is_weak_match(auto):
        return auto, None

    role = "buyer" if direction == "sales" else "seller"
    name = display_name(party.name)
    if not name:
        problems.append(
            _problem(
                "missing_party_name",
                f"The {role}'s name is missing, so no party ledger could be found or proposed. "
                f"Enter the {role}'s name or pick a party ledger on the review screen.",
                f"{role}.name",
            )
        )
        return auto, None
    holder = find_name_holder(ctx.ledgers, name)
    conflict = holder and gstin_conflict(holder, party.gstin, party.gstin_valid)
    if holder and is_party_ledger(holder) and not conflict:
        by_alias = holder.name.casefold() != name.casefold()
        method, score = ("alias", ALIAS_SCORE) if by_alias else ("exact", EXACT_SCORE)
        others = top([auto.ledger, *auto.candidates], holder.name, MAX_PARTY_CANDIDATES)
        return LedgerMatch(ledger=holder.name, method=method, score=score, candidates=others), None
    if holder:
        # Tally refuses a second ledger with a name or alias in use, so the new one is qualified.
        gstin = clean_gstin(party.gstin) if party.gstin_valid else None
        name = unique_ledger_name(name, ctx.ledgers, party.state, gstin)
    proposed = _proposal(party, name, group)
    if choices.create_party_ledger:
        others = top([auto.ledger, *auto.candidates], proposed.name, MAX_PARTY_CANDIDATES)
        match = LedgerMatch(ledger=proposed.name, method="choice", score=1.0, candidates=others)
        return match, proposed
    return auto, proposed


def _proposal(party: Party, name: str, group: str) -> ProposedLedger:
    gstin = clean_gstin(party.gstin) if party.gstin_valid else None
    gstin = gstin if is_gstin_shaped(gstin) else None
    lines = [s for part in re.split(r"[\n,]", party.address or "") if (s := part.strip())]
    return ProposedLedger(
        name=name,
        parent_group=group,
        gstin=gstin,
        gst_registration_type="Regular" if gstin else "Unregistered",
        state=party.state,
        address_lines=lines[:4],
        bill_wise=True,
    )


def _resolve_item(
    direction: Direction,
    ctx: AccountingContext,
    choices: LedgerChoices,
    party_name: str | None,
    rate: Decimal | None,
    supply: Supply | None,
    problems: list[AccountingProblem],
) -> LedgerMatch:
    alternatives = item_alternatives(ctx.ledgers, direction, rate, supply)

    def match(name: str | None, method: MatchMethod, score: float) -> LedgerMatch:
        others = top(alternatives, name, MAX_ITEM_CANDIDATES)
        return LedgerMatch(ledger=name, method=method, score=score, candidates=others)

    if choices.item_ledger:
        if chosen := find_ledger(ctx.ledgers, choices.item_ledger):
            return match(chosen.name, "choice", 1.0)
        problems.append(
            _problem(
                "unknown_item_ledger",
                f"The ledger '{choices.item_ledger}' picked for the items does not exist in "
                "Tally. "
                "Pick another ledger or sync ledgers.",
                "item_ledger",
            )
        )
    if party_name and (learned := ctx.learned_item_ledgers.get(party_name)):
        known = find_ledger(ctx.ledgers, learned)
        if known and can_take_learned_items(known, direction, rate):
            return match(known.name, "learned", 0.9)
    ranked = rank_item_ledgers(ctx.ledgers, direction, rate, supply)
    if ranked:
        # A ledger named for another rate or supply type is only a guess, however few exist.
        sure = len(ranked) == 1 and not is_item_misfit(ranked[0], rate, supply)
        return match(ranked[0].name, "default", 0.8 if sure else 0.6)
    if direction == "purchase":
        problems.append(
            _problem(
                "missing_item_ledger",
                "No purchase ledger was found. Create a ledger under Purchase Accounts in Tally "
                "(for example 'Purchase'), then sync ledgers.",
                "item_ledger",
            )
        )
    else:
        problems.append(
            _problem(
                "missing_item_ledger",
                "No sales ledger was found. Create a ledger under Sales Accounts in Tally "
                "(for example 'Sales'), then sync ledgers.",
                "item_ledger",
            )
        )
    return match(None, "none", 0.0)


def _resolve_tax_ledgers(
    taxes: dict[str, Decimal],
    direction: Direction,
    reverse_charge: bool,
    ctx: AccountingContext,
    rate: Decimal | None,
    problems: list[AccountingProblem],
) -> dict[str, str]:
    found: dict[str, str] = {}
    if reverse_charge and direction == "sales":
        return found  # the customer pays the tax to the government, so none is booked here
    rcm: RcmUse = "prefer" if reverse_charge else "exclude"
    for tax, amount in taxes.items():
        if amount <= 0:
            continue
        rates = _tax_rates(tax, rate)
        label = _TAX_LABELS[tax]
        wanted = f"{_SIDE_LABELS[direction]} {label}"
        lookup = find_tax_ledger(ctx.ledgers, direction, tax, rates, rcm)
        if lookup.ledger:
            found[tax] = lookup.ledger.name
        else:
            problems.append(
                _problem(
                    "missing_tax_ledger",
                    _no_tax_ledger(label, amount, wanted, rates, lookup.other_rates),
                    "tax_ledgers",
                )
            )
        if not reverse_charge:
            continue
        payable = find_tax_ledger(ctx.ledgers, "sales", tax, rates, "only")
        if payable.ledger:
            found[f"{tax}_rcm"] = payable.ledger.name
        else:
            wanted = f"{label} RCM Payable"
            problems.append(
                _problem(
                    "missing_rcm_ledger",
                    _no_tax_ledger(label, amount, wanted, rates, payable.other_rates)
                    if payable.other_rates
                    else f"This invoice is under reverse charge, so the company pays the {label} "
                    f"of {inr(amount)} to the government itself, but no ledger for that liability "
                    f"was found. Create a ledger named '{wanted}' under Duties & Taxes in Tally, "
                    "then sync ledgers.",
                    "tax_ledgers",
                )
            )
    return found


def _no_tax_ledger(
    label: str,
    amount: Decimal,
    wanted: str,
    rates: tuple[Decimal, ...] | None,
    other_rates: tuple[str, ...],
) -> str:
    if not other_rates:
        return (
            f"No '{wanted}' ledger was found for the {label} of {inr(amount)}. "
            f"Create an '{wanted}' ledger under Duties & Taxes in Tally, then sync ledgers."
        )
    shown = ", ".join(f"'{n}'" for n in other_rates[:_SHOWN_LEDGERS])
    if rates:
        percent = f"{rates[0].normalize():f}"
        return (
            f"The {label} of {inr(amount)} is at {percent}%, but the {label} ledgers in Tally "
            f"are for other rates ({shown}). Create a ledger named '{wanted} @ {percent}%' "
            "under Duties & Taxes in Tally, then sync ledgers."
        )
    return (
        "The GST rate of this invoice could not be worked out, because its lines have "
        f"different rates or none, and the {label} ledgers in Tally are kept by rate "
        f"({shown}), so the {label} of {inr(amount)} cannot be posted to one of them. Post "
        f"this invoice in Tally by hand, or create a ledger named '{wanted}' with no rate in "
        "its name under Duties & Taxes in Tally, then sync ledgers."
    )


def _resolve_round_off(
    diff: Decimal,
    ctx: AccountingContext,
    ledgers: dict[str, str],
    problems: list[AccountingProblem],
) -> None:
    if ledger := find_round_off_ledger(ctx.ledgers):
        ledgers["round_off"] = ledger.name
        return
    problems.append(
        _problem(
            "missing_round_off_ledger",
            f"The invoice total differs from taxable value plus tax by {inr(abs(diff))}, and no "
            "round-off ledger was found. Create a 'Round Off' ledger under Indirect Expenses in "
            "Tally, then sync ledgers.",
            "tax_ledgers",
        )
    )


# -- amounts and rates -----------------------------------------------------------------------


def _amounts(
    invoice: NormalizedInvoice, taxes: dict[str, Decimal], problems: list[AccountingProblem]
) -> _Amounts | None:
    taxable = invoice.taxable_value
    if taxable is None:
        values = [x.taxable_value for x in invoice.lines if x.taxable_value is not None]
        taxable = sum(values, Decimal("0")) if values else None
    if taxable is None:
        problems.append(
            _problem(
                "missing_taxable_value",
                "The taxable value is missing. Enter it on the review screen.",
                "taxable_value",
            )
        )
    if invoice.grand_total is None:
        problems.append(
            _problem(
                "missing_grand_total",
                "The invoice total is missing. Enter it on the review screen.",
                "grand_total",
            )
        )
    if taxable is None or invoice.grand_total is None:
        return None

    taxable, grand_total = _money(taxable.copy_abs()), _money(invoice.grand_total.copy_abs())
    if grand_total == 0:
        problems.append(
            _problem(
                "zero_total",
                "The invoice total is zero, so there is nothing to post. Check the amounts on the "
                "review screen.",
                "grand_total",
            )
        )
        return None
    tax = sum(taxes.values(), Decimal("0.00"))
    if invoice.reverse_charge:
        return _reverse_charge_amounts(taxable, taxes, tax, grand_total, problems)
    computed = taxable + tax
    diff = grand_total - computed
    if abs(diff) > _ROUND_OFF_LIMIT:
        problems.append(
            _problem(
                "totals_mismatch",
                f"The amounts do not add up: taxable value {inr(taxable)} plus tax {inr(tax)} "
                f"comes to {inr(computed)}, but the invoice total is {inr(grand_total)}. Correct "
                "the amounts on the review screen so they agree.",
                "grand_total",
            )
        )
        return None
    return _Amounts(taxable=taxable, taxes=taxes, party=grand_total, round_off=diff)


def _reverse_charge_amounts(
    taxable: Decimal,
    taxes: dict[str, Decimal],
    tax: Decimal,
    grand_total: Decimal,
    problems: list[AccountingProblem],
) -> _Amounts | None:
    # The recipient pays the tax to the government, so the supplier is owed the taxable value
    # whether the bill prints its total with the tax or without it.
    for total in (taxable, taxable + tax):
        diff = grand_total - total
        if abs(diff) <= _ROUND_OFF_LIMIT:
            return _Amounts(taxable=taxable, taxes=taxes, party=taxable + diff, round_off=diff)
    with_tax = f", or {inr(taxable + tax)} with the tax of {inr(tax)}" if tax else ""
    problems.append(
        _problem(
            "totals_mismatch",
            f"The amounts do not add up: under reverse charge the invoice total should be the "
            f"taxable value {inr(taxable)}{with_tax}, but it is {inr(grand_total)}. Correct the "
            "amounts on the review screen so they agree.",
            "grand_total",
        )
    )
    return None


def _gst_rate(invoice: NormalizedInvoice, taxable: Decimal | None) -> Decimal | None:
    """The one GST rate of all lines; if no line has a rate, the standard rate the totals
    imply. None for mixed rates."""
    rates = {
        x.gst_rate.copy_abs().quantize(_RATE_PLACES, rounding=ROUND_HALF_UP)
        for x in invoice.lines
        if x.gst_rate is not None
    }
    if rates:
        return rates.pop() if len(rates) == 1 else None
    if not taxable:
        return None
    gst = sum((v.copy_abs() for v in (invoice.cgst, invoice.sgst, invoice.igst)), Decimal("0"))
    effective = gst * 100 / taxable
    return next((r for r in _STANDARD_RATES if abs(effective - r) <= _RATE_TOLERANCE), None)


def _tax_rates(tax: str, rate: Decimal | None) -> tuple[Decimal, ...] | None:
    """The rates a ledger for this tax may be named with, best first: () when the invoice's
    rate is unknown, None when the rate does not matter (cess). CGST/SGST ledgers are usually
    named for their half of the rate, sometimes for the total."""
    if tax == "cess":
        return None
    if rate is None:
        return ()
    return (rate,) if tax == "igst" else (rate / 2, rate)


def _supply(taxes: dict[str, Decimal]) -> Supply | None:
    if taxes["igst"] > 0:
        return "inter"
    if taxes["cgst"] > 0 or taxes["sgst"] > 0:
        return "intra"
    return None


# -- voucher ---------------------------------------------------------------------------------


def _postings(
    amounts: _Amounts,
    item_ledger: str,
    ledgers: dict[str, str],
    direction: Direction,
    reverse_charge: bool,
) -> list[_Posting]:
    postings: list[_Posting] = [(item_ledger, True, amounts.taxable)]
    if not (reverse_charge and direction == "sales"):
        for tax, amount in amounts.taxes.items():
            if amount <= 0:
                continue
            postings.append((ledgers[tax], True, amount))
            if reverse_charge:
                postings.append((ledgers[f"{tax}_rcm"], False, amount))
    if amounts.round_off:
        postings.append((ledgers["round_off"], amounts.round_off > 0, abs(amounts.round_off)))
    return postings


def _transaction(
    invoice: NormalizedInvoice,
    direction: Direction,
    returned: bool,
    kind: VoucherKind,
    party: LedgerRef,
    postings: list[_Posting],
    party_amount: Decimal,
    gst: GstDetails,
    voucher_id: uuid.UUID | None,
    problems: list[AccountingProblem],
) -> CanonicalTransaction | None:
    # Purchase style debits item and taxes; sales style credits them; a credit note or a
    # purchase return reverses whichever style its side uses.
    reverse = invoice.document_type == "credit_note" or returned
    item_side = Side.DR if (direction == "purchase") != reverse else Side.CR
    party_side = Side.CR if item_side == Side.DR else Side.DR
    sides = {True: item_side, False: party_side}
    number = (invoice.invoice_number or "").strip() or None
    fields = {"id": voucher_id} if voucher_id else {}
    try:
        # Entry.amount must be > 0, so a zero taxable value (tax-only notes) gets no entry.
        entries = [
            Entry(ledger=LedgerRef(name=name), side=sides[with_item], amount=amount)
            for name, with_item, amount in postings
            if amount > 0
        ]
        entries.append(
            Entry(
                ledger=party,
                side=party_side,
                amount=party_amount,
                is_party=True,
                bill_ref=number,
            )
        )
        return CanonicalTransaction(
            **fields,
            voucher_kind=kind,
            date=invoice.invoice_date,
            reference_no=number,
            reference_date=invoice.invoice_date,
            voucher_number=number if direction == "sales" else None,
            narration=_narration(invoice, direction == "sales" or returned, number, party.name),
            gst=gst,
            entries=entries,
        )
    except ValidationError as exc:
        problems.append(
            _problem(
                "voucher_invalid",
                f"The voucher could not be built: {exc.errors()[0]['msg']}. Check the amounts and "
                "ledgers on the review screen.",
            )
        )
        return None


def _gst_details(
    invoice: NormalizedInvoice, counterparty: Party, ctx: AccountingContext
) -> GstDetails:
    party_gstin = clean_gstin(counterparty.gstin) if counterparty.gstin_valid else None
    party_gstin = party_gstin if is_gstin_shaped(party_gstin) else None
    company_gstin = clean_gstin(ctx.company_gstin)
    # The place of supply printed on the invoice, else the buyer's state: where goods are
    # usually delivered and services usually received.
    company_buys = counterparty is invoice.seller
    pos = (
        invoice.place_of_supply_code
        or invoice.buyer.state_code
        or (ctx.company_state_code if company_buys else None)
    )
    return GstDetails(
        party_gstin=party_gstin,
        party_registration_type="Regular" if party_gstin else "Unregistered",
        party_state=counterparty.state,
        place_of_supply=STATE_CODES.get(pos) if pos else None,
        company_gstin=company_gstin if is_gstin_shaped(company_gstin) else None,
    )


def _narration(
    invoice: NormalizedInvoice, company_issued: bool, number: str | None, party: str
) -> str:
    side = "Sales" if company_issued else "Purchase"
    label = {
        "credit_note": "Credit note",
        "debit_note": "Debit note",
        "bill_of_supply": f"{side} bill of supply",
    }.get(invoice.document_type, f"{side} invoice")
    parts = [label, number, f"dated {invoice.invoice_date:%d-%m-%Y}"]
    parts.append(f"{'to' if company_issued else 'from'} {party}")
    text = " ".join(p for p in parts if p)
    first = " ".join(invoice.lines[0].description.split()) if invoice.lines else ""
    if first and len(first) <= _SHORT_DESCRIPTION:
        text += f" - {first}"
    return text


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def inr(amount: Decimal) -> str:
    """Rupees with Indian digit grouping, e.g. Rs 1,18,000.00."""
    rounded = _money(amount)
    sign = "-" if rounded < 0 else ""
    whole, fraction = f"{abs(rounded):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = [head[max(i - 2, 0) : i] for i in range(len(head), 0, -2)][::-1]
    return f"Rs {sign}{','.join([*groups, tail])}.{fraction}"
