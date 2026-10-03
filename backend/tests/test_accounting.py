import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.accounting.engine import build_voucher, inr
from app.accounting.matching import display_name, normalize_name
from app.accounting.types import (
    AccountingContext,
    AccountingResult,
    LedgerChoices,
    LedgerInfo,
)
from app.schemas.canonical import CanonicalTransaction, Side, VoucherKind
from app.schemas.invoice import InvoiceLine, NormalizedInvoice, Party

D = Decimal
DR, CR = Side.DR, Side.CR

COMPANY_GSTIN = "29AAACD1234A1Z9"
SHARMA_GSTIN = "29ABCDE1234F1Z5"
MEHTA_GSTIN = "27MEHTA1234B1Z3"
GUPTA_GSTIN = "27PQRSX6789K1Z2"

LEDGERS = (
    LedgerInfo("Cash", "Cash-in-Hand"),
    LedgerInfo("HDFC Bank", "Bank Accounts"),
    LedgerInfo("Purchase", "Purchase Accounts"),
    LedgerInfo("Sales", "Sales Accounts"),
    LedgerInfo("Input CGST", "Duties & Taxes"),
    LedgerInfo("Input SGST", "Duties & Taxes"),
    LedgerInfo("Input IGST", "Duties & Taxes"),
    LedgerInfo("Output CGST", "Duties & Taxes"),
    LedgerInfo("Output SGST", "Duties & Taxes"),
    LedgerInfo("Output IGST", "Duties & Taxes"),
    LedgerInfo("Round Off", "Indirect Expenses"),
    LedgerInfo("Office Expenses", "Indirect Expenses"),
    LedgerInfo("Freight Inward", "Direct Expenses"),
    LedgerInfo(
        "Sharma Electronics", "Sundry Creditors", gstin=SHARMA_GSTIN, aliases=("Sharma Elec",)
    ),
    LedgerInfo("Mehta Steels", "Sundry Creditors", gstin=MEHTA_GSTIN),
    LedgerInfo("Kapoor Textiles", "Sundry Creditors"),
    LedgerInfo("Gupta Retail", "Sundry Debtors", gstin=GUPTA_GSTIN),
)

COMPANY = Party(
    name="Demo Traders Pvt Ltd",
    gstin=COMPANY_GSTIN,
    gstin_valid=True,
    state_code="29",
    state="Karnataka",
)
SHARMA = Party(
    name="Sharma Electronics",
    gstin=SHARMA_GSTIN,
    gstin_valid=True,
    state_code="29",
    state="Karnataka",
    address="14, SP Road, Bengaluru",
)
MEHTA = Party(
    name="MEHTA STEELS", gstin=MEHTA_GSTIN, gstin_valid=True, state_code="27", state="Maharashtra"
)
GUPTA = Party(
    name="Gupta Retail", gstin=GUPTA_GSTIN, gstin_valid=True, state_code="27", state="Maharashtra"
)


def msgs(result) -> list[str]:
    return [p.message for p in result.problems]


def ctx(ledgers=LEDGERS, **kwargs) -> AccountingContext:
    return AccountingContext(
        company_name="Demo Traders Pvt Ltd",
        company_gstin=COMPANY_GSTIN,
        company_state_code="29",
        ledgers=tuple(ledgers),
        **kwargs,
    )


def line(description: str = "24-port network switch", rate: str | None = "18", amount="10000"):
    return InvoiceLine(
        description=description,
        quantity=D("2"),
        rate=D(amount) / 2,
        taxable_value=D(amount),
        gst_rate=D(rate) if rate is not None else None,
    )


def purchase(**overrides) -> NormalizedInvoice:
    """Intra-state purchase: 10,000 taxable + 9% CGST + 9% SGST = 11,800."""
    fields = {
        "invoice_number": "SE/2026/0042",
        "invoice_date": date(2026, 9, 28),
        "seller": SHARMA,
        "buyer": COMPANY,
        "lines": [line()],
        "taxable_value": D("10000"),
        "cgst": D("900"),
        "sgst": D("900"),
        "grand_total": D("11800"),
    }
    return NormalizedInvoice(**(fields | overrides))


def igst_purchase(**overrides) -> NormalizedInvoice:
    fields = {"seller": MEHTA, "cgst": D("0"), "sgst": D("0"), "igst": D("1800")}
    return purchase(**(fields | overrides))


def sale(**overrides) -> NormalizedInvoice:
    """Inter-state sale to Maharashtra: 10,000 taxable + 18% IGST = 11,800."""
    fields = {
        "invoice_number": "DT/2026/101",
        "invoice_date": date(2026, 10, 1),
        "seller": COMPANY,
        "buyer": GUPTA,
        "lines": [line("Steel almirah")],
        "taxable_value": D("10000"),
        "igst": D("1800"),
        "grand_total": D("11800"),
    }
    return NormalizedInvoice(**(fields | overrides))


def built(result: AccountingResult) -> CanonicalTransaction:
    assert msgs(result) == []
    tx = result.transaction
    assert tx is not None
    assert tx.total(DR) == tx.total(CR)
    return tx


def entries(tx: CanonicalTransaction) -> list[tuple[str, Side, Decimal]]:
    return [(e.ledger.name, e.side, e.amount) for e in tx.entries]


def without(*names: str) -> tuple[LedgerInfo, ...]:
    return tuple(x for x in LEDGERS if x.name not in names)


# -- happy paths -------------------------------------------------------------------------------


def test_intra_state_purchase():
    result = build_voucher(purchase(), ctx())
    tx = built(result)

    assert result.direction == "purchase"
    assert "buyer's GSTIN" in result.direction_reason
    assert result.voucher_kind == tx.voucher_kind == VoucherKind.PURCHASE
    assert (result.party.ledger, result.party.method, result.party.score) == (
        "Sharma Electronics",
        "gstin",
        1.0,
    )
    assert result.proposed_party is None
    assert (result.item.ledger, result.item.method, result.item.score) == (
        "Purchase",
        "default",
        0.8,
    )
    assert "Office Expenses" in result.item.candidates
    assert "Freight Inward" in result.item.candidates
    assert "Round Off" not in result.item.candidates
    assert result.tax_ledgers == {"cgst": "Input CGST", "sgst": "Input SGST"}
    assert entries(tx) == [
        ("Purchase", DR, D("10000.00")),
        ("Input CGST", DR, D("900.00")),
        ("Input SGST", DR, D("900.00")),
        ("Sharma Electronics", CR, D("11800.00")),
    ]
    party = tx.party_entry
    assert party is not None and party.bill_ref == "SE/2026/0042"
    assert party.ledger.proposed is None
    assert tx.date == tx.reference_date == date(2026, 9, 28)
    assert tx.reference_no == "SE/2026/0042"
    assert tx.voucher_number is None
    assert tx.narration == (
        "Purchase invoice SE/2026/0042 dated 28-09-2026 from Sharma Electronics"
        " - 24-port network switch"
    )


def test_inter_state_purchase_uses_igst():
    result = build_voucher(igst_purchase(), ctx())
    tx = built(result)

    assert result.party.ledger == "Mehta Steels"
    assert result.tax_ledgers == {"igst": "Input IGST"}
    assert entries(tx) == [
        ("Purchase", DR, D("10000.00")),
        ("Input IGST", DR, D("1800.00")),
        ("Mehta Steels", CR, D("11800.00")),
    ]


def test_sales_invoice():
    result = build_voucher(sale(), ctx())
    tx = built(result)

    assert result.direction == "sales"
    assert "seller's GSTIN" in result.direction_reason
    assert result.voucher_kind == VoucherKind.SALES
    assert (result.party.ledger, result.party.method) == ("Gupta Retail", "gstin")
    assert (result.item.ledger, result.item.method) == ("Sales", "default")
    assert "Office Expenses" not in result.item.candidates
    assert entries(tx) == [
        ("Sales", CR, D("10000.00")),
        ("Output IGST", CR, D("1800.00")),
        ("Gupta Retail", DR, D("11800.00")),
    ]
    assert tx.voucher_number == "DT/2026/101"
    assert tx.party_entry.bill_ref == "DT/2026/101"
    assert tx.narration.startswith("Sales invoice DT/2026/101 dated 01-10-2026 to Gupta Retail")


@pytest.mark.parametrize(
    ("factory", "taxable", "tax", "side"),
    [
        (purchase, "999.70", "89.97", DR),  # 1,179.64 printed as 1,180: +0.36
        (purchase, "1000.30", "90.03", CR),  # 1,180.36 printed as 1,180: -0.36
        (sale, "999.70", "89.97", CR),
        (sale, "1000.30", "90.03", DR),
    ],
)
def test_round_off(factory, taxable, tax, side):
    invoice = factory(
        taxable_value=D(taxable),
        cgst=D(tax),
        sgst=D(tax),
        igst=D("0"),
        grand_total=D("1180"),
        lines=[line(amount=taxable)],
    )
    result = build_voucher(invoice, ctx())
    tx = built(result)

    assert result.tax_ledgers["round_off"] == "Round Off"
    assert ("Round Off", side, D("0.36")) in entries(tx)
    assert tx.party_entry.amount == D("1180.00")


def test_difference_of_exactly_one_rupee_is_round_off():
    tx = built(build_voucher(purchase(grand_total=D("11801")), ctx()))
    assert ("Round Off", DR, D("1.00")) in entries(tx)


def test_rate_specific_ledgers_are_preferred():
    ledgers = LEDGERS + (
        LedgerInfo("Purchase @ 12%", "Purchase Accounts"),
        LedgerInfo("Purchase @ 18%", "Purchase Accounts"),
        LedgerInfo("Input CGST @ 6%", "Duties & Taxes"),
        LedgerInfo("Input CGST @ 9%", "Duties & Taxes"),
        LedgerInfo("Input SGST @ 6%", "Duties & Taxes"),
        LedgerInfo("Input SGST @ 9%", "Duties & Taxes"),
        LedgerInfo("Input IGST 18 %", "Duties & Taxes"),
    )
    at_18 = build_voucher(purchase(), ctx(ledgers))
    assert at_18.item.ledger == "Purchase @ 18%"
    assert at_18.item.score == 0.6  # several purchase ledgers to choose from
    assert at_18.tax_ledgers == {"cgst": "Input CGST @ 9%", "sgst": "Input SGST @ 9%"}

    twelve = {"cgst": D("600"), "sgst": D("600"), "grand_total": D("11200")}
    at_12 = build_voucher(purchase(lines=[line(rate="12")], **twelve), ctx(ledgers))
    assert at_12.item.ledger == "Purchase @ 12%"
    assert at_12.tax_ledgers == {"cgst": "Input CGST @ 6%", "sgst": "Input SGST @ 6%"}

    # No rate on the lines: the rate is read from the totals.
    inferred = build_voucher(purchase(lines=[], **twelve), ctx(ledgers))
    assert inferred.tax_ledgers["cgst"] == "Input CGST @ 6%"

    five = {"cgst": D("250"), "sgst": D("250"), "grand_total": D("10500")}
    at_5 = build_voucher(purchase(lines=[line(rate="5")], **five), ctx(ledgers))
    assert at_5.item.ledger == "Purchase"  # no 5% ledger: the plain one, not another rate
    assert at_5.tax_ledgers == {"cgst": "Input CGST", "sgst": "Input SGST"}

    igst = build_voucher(igst_purchase(), ctx(ledgers))
    assert igst.tax_ledgers == {"igst": "Input IGST 18 %"}
    built(igst)


def test_tax_ledger_names_with_dots_and_utgst():
    ledgers = without("Input CGST", "Input SGST") + (
        LedgerInfo("Input C.G.S.T", "Duties & Taxes"),
        LedgerInfo("Input UTGST", "Duties & Taxes"),
    )
    result = build_voucher(purchase(), ctx(ledgers))
    assert result.tax_ledgers == {"cgst": "Input C.G.S.T", "sgst": "Input UTGST"}
    built(result)


def test_tax_ledger_without_side_word_is_a_fallback():
    ledgers = without("Input IGST") + (LedgerInfo("IGST", "Duties & Taxes"),)
    result = build_voucher(igst_purchase(), ctx(ledgers))
    assert result.tax_ledgers == {"igst": "IGST"}
    built(result)


RATE_WISE = without("Input CGST", "Input SGST") + (
    LedgerInfo("Input CGST @ 6%", "Duties & Taxes"),
    LedgerInfo("Input CGST @ 9%", "Duties & Taxes"),
    LedgerInfo("Input SGST @ 6%", "Duties & Taxes"),
    LedgerInfo("Input SGST @ 9%", "Duties & Taxes"),
)
FIVE_PERCENT = {
    "lines": [line(rate="5")],
    "cgst": D("250"),
    "sgst": D("250"),
    "grand_total": D("10500"),
}


def test_tax_ledger_for_another_rate_is_never_used():
    result = build_voucher(purchase(**FIVE_PERCENT), ctx(RATE_WISE))

    assert result.transaction is None
    assert result.tax_ledgers == {}
    assert msgs(result) == [
        "The CGST of Rs 250.00 is at 2.5%, but the CGST ledgers in Tally are for other rates "
        "('Input CGST @ 6%', 'Input CGST @ 9%'). Create a ledger named 'Input CGST @ 2.5%' under "
        "Duties & Taxes in Tally, then sync ledgers.",
        "The SGST of Rs 250.00 is at 2.5%, but the SGST ledgers in Tally are for other rates "
        "('Input SGST @ 6%', 'Input SGST @ 9%'). Create a ledger named 'Input SGST @ 2.5%' under "
        "Duties & Taxes in Tally, then sync ledgers.",
    ]


def test_mixed_rates_cannot_go_to_rate_wise_tax_ledgers():
    lines = [line(rate="12", amount="5000"), line("Patch cords", rate="18", amount="5000")]
    mixed = purchase(lines=lines, cgst=D("750"), sgst=D("750"), grand_total=D("11500"))

    rate_wise = build_voucher(mixed, ctx(RATE_WISE))
    assert rate_wise.transaction is None
    assert len(msgs(rate_wise)) == 2
    assert msgs(rate_wise)[0].startswith(
        "The GST rate of this invoice could not be worked out, because its lines have "
        "different rates or none, and the CGST ledgers in Tally are kept by rate"
    )
    assert "create a ledger named 'Input CGST' with no rate" in msgs(rate_wise)[0]

    plain = build_voucher(mixed, ctx(RATE_WISE + (LedgerInfo("Input CGST", "Duties & Taxes"),)))
    assert plain.tax_ledgers["cgst"] == "Input CGST"


def test_cgst_ledgers_named_by_the_total_rate():
    ledgers = without("Input CGST", "Input SGST") + (
        LedgerInfo("Input CGST @ 12%", "Duties & Taxes"),
        LedgerInfo("Input CGST @ 18%", "Duties & Taxes"),
        LedgerInfo("Input SGST @ 12%", "Duties & Taxes"),
        LedgerInfo("Input SGST @ 18%", "Duties & Taxes"),
    )
    result = build_voucher(purchase(), ctx(ledgers))
    assert result.tax_ledgers == {"cgst": "Input CGST @ 18%", "sgst": "Input SGST @ 18%"}
    built(result)


def test_reverse_charge_tax_ledgers_are_not_used_for_forward_charge():
    ledgers = RATE_WISE + (
        LedgerInfo("Input CGST RCM", "Duties & Taxes"),
        LedgerInfo("Input SGST RCM", "Duties & Taxes"),
    )
    at_5 = build_voucher(purchase(**FIVE_PERCENT), ctx(ledgers))
    assert at_5.tax_ledgers == {}
    assert "Create a ledger named 'Input CGST @ 2.5%'" in msgs(at_5)[0]

    at_18 = build_voucher(purchase(), ctx(ledgers))
    assert at_18.tax_ledgers == {"cgst": "Input CGST @ 9%", "sgst": "Input SGST @ 9%"}


def test_item_ledger_named_for_another_rate_is_only_a_guess():
    ledgers = without("Purchase") + (LedgerInfo("Purchase @ 12%", "Purchase Accounts"),)
    result = build_voucher(purchase(), ctx(ledgers))
    assert (result.item.ledger, result.item.method, result.item.score) == (
        "Purchase @ 12%",
        "default",
        0.6,  # below validation's weak-item threshold, so the reviewer is asked to check
    )


def test_payable_and_receivable_tax_ledgers():
    ledgers = without("Input CGST", "Input SGST", "Output CGST", "Output SGST") + (
        LedgerInfo("CGST Payable", "Duties & Taxes"),
        LedgerInfo("CGST Receivable", "Duties & Taxes"),
        LedgerInfo("SGST Payable", "Duties & Taxes"),
        LedgerInfo("SGST Receivable", "Duties & Taxes"),
    )
    bought = build_voucher(purchase(), ctx(ledgers))
    assert bought.tax_ledgers == {"cgst": "CGST Receivable", "sgst": "SGST Receivable"}
    built(bought)
    local_sale = sale(igst=D("0"), cgst=D("900"), sgst=D("900"))
    sold = build_voucher(local_sale, ctx(ledgers))
    assert sold.tax_ledgers == {"cgst": "CGST Payable", "sgst": "SGST Payable"}

    itc = without("Input IGST", "Output IGST") + (
        LedgerInfo("IGST Payable", "Duties & Taxes"),
        LedgerInfo("ITC IGST", "Duties & Taxes"),
    )
    assert build_voucher(igst_purchase(), ctx(itc)).tax_ledgers == {"igst": "ITC IGST"}
    assert build_voucher(sale(), ctx(itc)).tax_ledgers == {"igst": "IGST Payable"}


def test_generic_gst_ledger_is_not_an_sgst_ledger():
    generic = (
        LedgerInfo("Input GST", "Duties & Taxes"),
        LedgerInfo("Output GST", "Duties & Taxes"),
    )
    bought = build_voucher(purchase(), ctx(LEDGERS + generic))
    assert bought.tax_ledgers == {"cgst": "Input CGST", "sgst": "Input SGST"}
    local_sale = sale(igst=D("0"), cgst=D("900"), sgst=D("900"))
    sold = build_voucher(local_sale, ctx(LEDGERS + generic))
    assert sold.tax_ledgers == {"cgst": "Output CGST", "sgst": "Output SGST"}

    missing = build_voucher(purchase(), ctx(without("Input SGST") + generic))
    assert missing.transaction is None
    assert msgs(missing) == [
        "No 'Input SGST' ledger was found for the SGST of Rs 900.00. "
        "Create an 'Input SGST' ledger under Duties & Taxes in Tally, then sync ledgers."
    ]


def test_item_ledger_follows_the_supply_type():
    ledgers = without("Purchase") + (
        LedgerInfo("Purchase Interstate @ 18%", "Purchase Accounts"),
        LedgerInfo("Purchase Local @ 18%", "Purchase Accounts"),
    )
    assert build_voucher(purchase(), ctx(ledgers)).item.ledger == "Purchase Local @ 18%"
    inter = build_voucher(igst_purchase(), ctx(ledgers))
    assert inter.item.ledger == "Purchase Interstate @ 18%"
    assert inter.item.candidates[0] == "Purchase Local @ 18%"

    # A ledger for the other supply type is passed over even when it names the right rate.
    plain = LEDGERS + (LedgerInfo("Purchase Interstate @ 18%", "Purchase Accounts"),)
    assert build_voucher(purchase(), ctx(plain)).item.ledger == "Purchase"


def test_ledgers_under_sub_groups():
    ledgers = without("Input CGST", "Input SGST", "Purchase") + (
        LedgerInfo("Input CGST", "GST Input"),
        LedgerInfo("Input SGST", "GST Input"),
        LedgerInfo("Purchase GST 18%", "Purchase - GST"),
        LedgerInfo("Printing & Stationery", "Admin Expenses"),
        LedgerInfo("GUPTA RETAIL.", "Creditors for Goods"),
    )
    result = build_voucher(purchase(), ctx(ledgers))
    tx = built(result)
    assert result.tax_ledgers == {"cgst": "Input CGST", "sgst": "Input SGST"}
    assert (result.item.ledger, result.item.score) == ("Purchase GST 18%", 0.8)
    assert "Printing & Stationery" in result.item.candidates
    assert entries(tx)[0] == ("Purchase GST 18%", DR, D("10000.00"))

    # An expense ledger in a sub-group is never the party; a creditor in one is preferred.
    expense = build_voucher(purchase(seller=Party(name="Printing & Stationery")), ctx(ledgers))
    assert expense.party.method == "none"
    supplier = build_voucher(purchase(seller=Party(name="Gupta Retail")), ctx(ledgers))
    assert (supplier.party.ledger, supplier.party.method) == ("GUPTA RETAIL.", "exact")


# -- party matching ----------------------------------------------------------------------------


def test_gstin_match_beats_name_match():
    seller = MEHTA.model_copy(update={"name": "Gupta Retail"})
    result = build_voucher(igst_purchase(seller=seller), ctx())

    assert (result.party.ledger, result.party.method) == ("Mehta Steels", "gstin")
    assert "Gupta Retail" in result.party.candidates


def test_gstin_match_prefers_the_sides_group():
    ledgers = LEDGERS + (
        LedgerInfo("Gupta Retail (Supplier)", "Sundry Creditors", gstin=GUPTA_GSTIN),
    )
    result = build_voucher(igst_purchase(seller=GUPTA), ctx(ledgers))

    assert (result.party.ledger, result.party.method) == ("Gupta Retail (Supplier)", "gstin")
    assert result.party.candidates[0] == "Gupta Retail"


def test_gstin_conflict_blocks_a_name_match():
    other_branch = SHARMA.model_copy(
        update={"gstin": "27ABCDE1234F1Z9", "state_code": "27", "state": "Maharashtra"}
    )
    result = build_voucher(purchase(seller=other_branch), ctx())
    tx = built(result)

    assert (result.party.ledger, result.party.method, result.party.score) == (None, "none", 0.0)
    assert "Sharma Electronics" in result.party.candidates
    proposed = result.proposed_party
    assert proposed is not None
    # Tally refuses duplicate names, so the new ledger is qualified.
    assert proposed.name == "Sharma Electronics (Maharashtra)"
    assert proposed.gstin == "27ABCDE1234F1Z9"
    assert tx.party_entry.ledger.proposed == proposed


def test_invalid_gstin_does_not_block_a_name_match():
    misread = SHARMA.model_copy(update={"gstin": "29ABCDE1234F1Z0", "gstin_valid": False})
    result = build_voucher(purchase(seller=misread), ctx())

    assert (result.party.ledger, result.party.method, result.party.score) == (
        "Sharma Electronics",
        "exact",
        0.97,
    )


def test_exact_match_after_normalization():
    seller = Party(name="M/S. SHARMA ELECTRONICS")
    result = build_voucher(purchase(seller=seller), ctx())
    assert (result.party.ledger, result.party.method) == ("Sharma Electronics", "exact")
    assert normalize_name("M/s. Sharma & Co. Pvt. Ltd.") == normalize_name(
        "Sharma and Company Private Limited"
    )


def test_exact_match_prefers_the_sides_group():
    ledgers = LEDGERS + (LedgerInfo("GUPTA RETAIL.", "Sundry Creditors"),)
    result = build_voucher(purchase(seller=Party(name="Gupta Retail")), ctx(ledgers))
    assert (result.party.ledger, result.party.method) == ("GUPTA RETAIL.", "exact")
    assert result.party.candidates[0] == "Gupta Retail"


def test_alias_match():
    result = build_voucher(purchase(seller=Party(name="SHARMA ELEC")), ctx())
    assert (result.party.ledger, result.party.method, result.party.score) == (
        "Sharma Electronics",
        "alias",
        0.95,
    )


def test_fuzzy_match():
    strong = build_voucher(purchase(seller=Party(name="M/s. Sharma Electronic Pvt. Ltd.")), ctx())
    assert (strong.party.ledger, strong.party.method) == ("Sharma Electronics", "fuzzy")
    assert 0.92 <= strong.party.score < 1

    weak = build_voucher(purchase(seller=Party(name="Kapur Textiles")), ctx())
    assert (weak.party.ledger, weak.party.method, weak.party.score) == (
        "Kapoor Textiles",
        "fuzzy",
        0.6,
    )
    # It may be another firm, so creating a new ledger is offered too.
    assert weak.proposed_party.name == "Kapur Textiles"
    party = built(weak).party_entry
    assert (party.ledger.name, party.ledger.proposed) == ("Kapoor Textiles", None)


def test_weak_match_can_be_replaced_by_a_new_ledger():
    choices = LedgerChoices(create_party_ledger=True)
    result = build_voucher(purchase(seller=Party(name="Kapur Textiles")), ctx(), choices)
    tx = built(result)

    assert (result.party.ledger, result.party.method, result.party.score) == (
        "Kapur Textiles",
        "choice",
        1.0,
    )
    assert result.party.candidates == ["Kapoor Textiles"]
    assert tx.party_entry.ledger.proposed == result.proposed_party
    assert entries(tx)[-1] == ("Kapur Textiles", CR, D("11800.00"))


def test_fuzzy_match_never_outranks_an_exact_or_alias_match():
    result = build_voucher(purchase(seller=Party(name="Sharma Electronics Pvt Ltd")), ctx())
    assert (result.party.ledger, result.party.method) == ("Sharma Electronics", "fuzzy")
    assert result.party.score < 0.95


def test_different_legal_forms_are_different_parties():
    ledgers = without("Sharma Electronics") + (
        LedgerInfo("Sharma Electronics Pvt Ltd", "Sundry Creditors"),
    )
    llp = Party(name="Sharma Electronics LLP", gstin="29ABCFS1234A1Z5", gstin_valid=True)
    result = build_voucher(purchase(seller=llp), ctx(ledgers))

    # Only a weak match, which validation asks the reviewer to confirm.
    assert (result.party.ledger, result.party.method, result.party.score) == (
        "Sharma Electronics Pvt Ltd",
        "fuzzy",
        0.6,
    )
    assert result.proposed_party.name == "Sharma Electronics LLP"
    assert result.proposed_party.gstin == "29ABCFS1234A1Z5"


def test_unrelated_name_does_not_match_a_non_party_ledger():
    result = build_voucher(purchase(seller=Party(name="Office Expenses")), ctx())
    assert result.party.method == "none"
    assert result.proposed_party.name == "Office Expenses (2)"


# -- new parties -------------------------------------------------------------------------------

NEW_AGE = Party(
    name="NEW AGE SUPPLIES PVT LTD",
    gstin="29NEWAG1234C1Z1",
    gstin_valid=True,
    state_code="29",
    state="Karnataka",
    address="No. 12, 1st Cross\nMG Road, Bengaluru\n\nKarnataka - 560001, India",
)


def test_new_party_is_proposed_and_the_voucher_still_built():
    result = build_voucher(purchase(seller=NEW_AGE), ctx())
    tx = built(result)

    assert (result.party.ledger, result.party.method, result.party.score) == (None, "none", 0.0)
    proposed = result.proposed_party
    assert proposed is not None
    assert proposed.name == "New Age Supplies Pvt Ltd"
    assert proposed.parent_group == "Sundry Creditors"
    assert proposed.gstin == "29NEWAG1234C1Z1"
    assert proposed.gst_registration_type == "Regular"
    assert proposed.state == "Karnataka"
    assert proposed.address_lines == ["No. 12", "1st Cross", "MG Road", "Bengaluru"]
    assert proposed.bill_wise is True
    party = tx.party_entry
    assert party.ledger.name == proposed.name and party.ledger.proposed == proposed
    assert tx.ledgers_to_create() == [proposed]
    assert tx.narration.startswith("Purchase invoice SE/2026/0042 dated 28-09-2026 from New Age")


def test_approved_new_party():
    choices = LedgerChoices(create_party_ledger=True)
    result = build_voucher(purchase(seller=NEW_AGE), ctx(), choices)
    tx = built(result)

    assert (result.party.ledger, result.party.method, result.party.score) == (
        "New Age Supplies Pvt Ltd",
        "choice",
        1.0,
    )
    assert result.proposed_party is not None  # still has to be created in Tally
    assert tx.party_entry.ledger.proposed == result.proposed_party


def test_unregistered_customer_is_proposed_under_sundry_debtors():
    buyer = Party(name="  Ravi   Kumar  ", address="Hubli")
    result = build_voucher(sale(buyer=buyer, igst=D("0"), grand_total=D("10000")), ctx())
    built(result)

    proposed = result.proposed_party
    assert proposed.name == "Ravi Kumar"
    assert proposed.parent_group == "Sundry Debtors"
    assert proposed.gstin is None
    assert proposed.gst_registration_type == "Unregistered"
    assert proposed.address_lines == ["Hubli"]


def test_existing_ledger_with_the_proposed_name_is_reused():
    created = LedgerInfo("New Age Supplies Pvt Ltd", "Sundry Creditors")
    seller = NEW_AGE.model_copy(update={"gstin": None, "gstin_valid": False})
    choices = LedgerChoices(create_party_ledger=True)
    result = build_voucher(purchase(seller=seller), ctx(LEDGERS + (created,)), choices)
    tx = built(result)

    assert (result.party.ledger, result.party.method) == ("New Age Supplies Pvt Ltd", "exact")
    assert result.proposed_party is None
    assert tx.party_entry.ledger.proposed is None
    assert tx.ledgers_to_create() == []

    # Synced with its GSTIN, the same ledger is found by GSTIN.
    synced = LedgerInfo("New Age Supplies Pvt Ltd", "Sundry Creditors", gstin=NEW_AGE.gstin)
    again = build_voucher(purchase(seller=NEW_AGE), ctx(LEDGERS + (synced,)), choices)
    assert (again.party.method, again.proposed_party) == ("gstin", None)


def test_proposed_name_never_repeats_an_alias():
    # Tally keeps names and aliases in one namespace, so "Sharma Elec" is taken.
    other_branch = SHARMA.model_copy(
        update={
            "name": "Sharma Elec",
            "gstin": "27ABCDE1234F1Z9",
            "state_code": "27",
            "state": "Maharashtra",
        }
    )
    choices = LedgerChoices(create_party_ledger=True)
    result = build_voucher(purchase(seller=other_branch), ctx(), choices)
    built(result)

    assert result.proposed_party.name == "Sharma Elec (Maharashtra)"
    assert result.party.ledger == "Sharma Elec (Maharashtra)"


def test_qualified_proposed_name_fits_tallys_limit():
    name = "Shree " + "Balaji " * 30 + "Traders"
    state = "Dadra and Nagar Haveli and Daman and Diu"
    clash = LedgerInfo(name, "Indirect Expenses")
    result = build_voucher(purchase(seller=Party(name=name, state=state)), ctx(LEDGERS + (clash,)))
    built(result)

    proposed = result.proposed_party.name
    assert len(proposed) <= 255
    assert proposed.startswith("Shree Balaji") and proposed.endswith(f" ({state})")


def test_party_without_a_name_is_a_problem():
    result = build_voucher(purchase(seller=Party()), ctx())
    assert result.transaction is None
    assert msgs(result) == [
        "The seller's name is missing, so no party ledger could be found or proposed. "
        "Enter the seller's name or pick a party ledger on the review screen."
    ]


# -- reviewer choices and learning -------------------------------------------------------------


def test_choices_override_the_engine():
    choices = LedgerChoices(party_ledger="Mehta Steels", item_ledger="Office Expenses")
    result = build_voucher(purchase(), ctx(), choices)
    tx = built(result)

    assert (result.party.ledger, result.party.method, result.party.score) == (
        "Mehta Steels",
        "choice",
        1.0,
    )
    assert result.party.candidates[0] == "Sharma Electronics"
    assert (result.item.ledger, result.item.method) == ("Office Expenses", "choice")
    assert entries(tx)[0] == ("Office Expenses", DR, D("10000.00"))
    assert entries(tx)[-1] == ("Mehta Steels", CR, D("11800.00"))


def test_direction_choice():
    stranger = Party(name="Lakshmi Stores")
    invoice = purchase(seller=Party(name="Some Supplier"), buyer=stranger)
    result = build_voucher(invoice, ctx(), LedgerChoices(direction="sales"))
    tx = built(result)

    assert result.direction == "sales"
    assert result.direction_reason == "The reviewer marked this document as a sale."
    assert result.voucher_kind == VoucherKind.SALES
    assert result.proposed_party.name == "Lakshmi Stores"
    assert result.proposed_party.parent_group == "Sundry Debtors"
    assert result.tax_ledgers == {"cgst": "Output CGST", "sgst": "Output SGST"}
    assert tx.party_entry.side == DR


def test_unknown_chosen_ledgers_are_problems():
    choices = LedgerChoices(party_ledger="Deleted Party", item_ledger="Deleted Purchase")
    result = build_voucher(purchase(), ctx(), choices)

    assert result.transaction is None
    assert msgs(result) == [
        "The ledger 'Deleted Party' picked for the party does not exist in Tally. "
        "Pick another ledger or sync ledgers.",
        "The ledger 'Deleted Purchase' picked for the items does not exist in Tally. "
        "Pick another ledger or sync ledgers.",
    ]


def test_learned_item_ledger():
    learned = ctx(learned_item_ledgers={"Sharma Electronics": "Freight Inward"})
    result = build_voucher(purchase(), learned)
    built(result)
    assert (result.item.ledger, result.item.method, result.item.score) == (
        "Freight Inward",
        "learned",
        0.9,
    )

    gone = ctx(learned_item_ledgers={"Sharma Electronics": "Old Purchase Ledger"})
    assert build_voucher(purchase(), gone).item.method == "default"


def test_learned_item_ledger_must_suit_the_invoice():
    # Gupta Retail is a customer and a supplier: its purchase ledger must not take a sale.
    other_side = ctx(learned_item_ledgers={"Gupta Retail": "Purchase"})
    result = build_voucher(sale(), other_side)
    assert (result.item.ledger, result.item.method) == ("Sales", "default")
    assert ("Sales", CR, D("10000.00")) in entries(built(result))

    ledgers = LEDGERS + (
        LedgerInfo("Purchase @ 12%", "Purchase Accounts"),
        LedgerInfo("Purchase @ 18%", "Purchase Accounts"),
    )
    learned = ctx(ledgers, learned_item_ledgers={"Sharma Electronics": "Purchase @ 12%"})
    at_18 = build_voucher(purchase(), learned)
    assert (at_18.item.ledger, at_18.item.method) == ("Purchase @ 18%", "default")
    twelve = {"cgst": D("600"), "sgst": D("600"), "grand_total": D("11200")}
    at_12 = build_voucher(purchase(lines=[line(rate="12")], **twelve), learned)
    assert (at_12.item.ledger, at_12.item.method) == ("Purchase @ 12%", "learned")


# -- problems ----------------------------------------------------------------------------------


def test_missing_tax_ledger():
    result = build_voucher(igst_purchase(), ctx(without("Input IGST")))

    assert result.transaction is None
    assert result.party.ledger == "Mehta Steels"  # matches are still reported
    assert msgs(result) == [
        "No 'Input IGST' ledger was found for the IGST of Rs 1,800.00. "
        "Create an 'Input IGST' ledger under Duties & Taxes in Tally, then sync ledgers."
    ]


def test_totals_mismatch():
    result = build_voucher(purchase(grand_total=D("12000")), ctx())

    assert result.transaction is None
    assert msgs(result) == [
        "The amounts do not add up: taxable value Rs 10,000.00 plus tax Rs 1,800.00 comes to "
        "Rs 11,800.00, but the invoice total is Rs 12,000.00. Correct the amounts on the "
        "review screen so they agree."
    ]
    assert build_voucher(purchase(grand_total=D("11801.01")), ctx()).transaction is None


def test_missing_round_off_ledger():
    invoice = purchase(grand_total=D("11800.40"))
    ledgers = without("Round Off") + (LedgerInfo("Ground Rent", "Indirect Expenses"),)
    result = build_voucher(invoice, ctx(ledgers))
    assert result.transaction is None
    assert "Create a 'Round Off' ledger under Indirect Expenses" in msgs(result)[0]
    assert "Rs 0.40" in msgs(result)[0]


def test_missing_purchase_ledger():
    result = build_voucher(purchase(), ctx(without("Purchase")))
    assert result.transaction is None
    assert result.item.method == "none"
    assert msgs(result) == [
        "No purchase ledger was found. Create a ledger under Purchase Accounts in Tally "
        "(for example 'Purchase'), then sync ledgers."
    ]


def test_missing_date_and_amounts():
    result = build_voucher(
        purchase(invoice_date=None, taxable_value=None, lines=[], grand_total=None), ctx()
    )
    assert result.transaction is None
    assert msgs(result) == [
        "The taxable value is missing. Enter it on the review screen.",
        "The invoice total is missing. Enter it on the review screen.",
        "The invoice date is missing. Enter it on the review screen.",
    ]


def test_taxable_value_falls_back_to_the_lines():
    lines = [line(amount="6000"), line("Patch cords", amount="4000")]
    tx = built(build_voucher(purchase(taxable_value=None, lines=lines), ctx()))
    assert entries(tx)[0] == ("Purchase", DR, D("10000.00"))


def test_zero_and_absurd_totals_are_problems():
    zero = {"taxable_value": D("0"), "cgst": D("0"), "sgst": D("0"), "grand_total": D("0")}
    assert "nothing to post" in msgs(build_voucher(purchase(**zero), ctx()))[0]
    absurd = build_voucher(purchase(grand_total=D("1e30")), ctx())
    assert absurd.transaction is None
    assert "too large" in msgs(absurd)[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"grand_total": D("1E+1000000")},
        {"cgst": D("-1E+1000000")},
        {"lines": [InvoiceLine(taxable_value=D("10000"), gst_rate=D("1E+1000000"))]},
    ],
)
def test_huge_exponents_are_problems_not_exceptions(overrides):
    result = build_voucher(purchase(**overrides), ctx())
    assert result.transaction is None
    assert len(msgs(result)) == 1 and "too large to be right" in msgs(result)[0]


# -- document types ----------------------------------------------------------------------------


def test_credit_note_on_the_sales_side_reverses_the_sale():
    invoice = sale(
        document_type="credit_note",
        invoice_number="CN/7",
        taxable_value=D("-10000"),
        igst=D("-1800"),
        grand_total=D("-11800"),
    )
    result = build_voucher(invoice, ctx())
    tx = built(result)

    assert result.voucher_kind == VoucherKind.CREDIT_NOTE
    assert entries(tx) == [
        ("Sales", DR, D("10000.00")),
        ("Output IGST", DR, D("1800.00")),
        ("Gupta Retail", CR, D("11800.00")),
    ]
    assert tx.voucher_number == "CN/7"
    assert tx.narration.startswith("Credit note CN/7 dated 01-10-2026 to Gupta Retail")


def test_credit_note_on_the_purchase_side_is_a_purchase_return():
    invoice = purchase(
        document_type="credit_note",
        invoice_number="SE/CN/3",
        taxable_value=D("999.70"),
        cgst=D("89.97"),
        sgst=D("89.97"),
        grand_total=D("1180"),
        lines=[line(amount="999.70")],
    )
    result = build_voucher(invoice, ctx())
    tx = built(result)

    assert result.voucher_kind == VoucherKind.DEBIT_NOTE
    assert entries(tx) == [
        ("Purchase", CR, D("999.70")),
        ("Input CGST", CR, D("89.97")),
        ("Input SGST", CR, D("89.97")),
        ("Round Off", CR, D("0.36")),
        ("Sharma Electronics", DR, D("1180.00")),
    ]
    assert tx.voucher_number is None
    assert tx.reference_no == "SE/CN/3"
    assert tx.narration.startswith("Credit note SE/CN/3 dated 28-09-2026 from Sharma Electronics")


def test_debit_notes():
    sales_side = build_voucher(sale(document_type="debit_note"), ctx())
    assert sales_side.voucher_kind == VoucherKind.DEBIT_NOTE
    assert entries(built(sales_side)) == [
        ("Sales", CR, D("10000.00")),
        ("Output IGST", CR, D("1800.00")),
        ("Gupta Retail", DR, D("11800.00")),
    ]

    purchase_side = build_voucher(purchase(document_type="debit_note"), ctx())
    assert purchase_side.voucher_kind == VoucherKind.PURCHASE
    tx = built(purchase_side)
    assert entries(tx)[-1] == ("Sharma Electronics", CR, D("11800.00"))
    assert tx.narration.startswith("Debit note SE/2026/0042")


def test_debit_note_issued_to_a_supplier_is_a_purchase_return():
    note = purchase(
        document_type="debit_note", invoice_number="DT/DN/4", seller=COMPANY, buyer=SHARMA
    )
    result = build_voucher(note, ctx())
    tx = built(result)

    assert result.direction == "purchase"
    assert result.direction_reason == (
        "The seller's GSTIN is the company's own GSTIN, and the buyer is a supplier in Tally, "
        "so this debit note is a purchase return."
    )
    assert result.voucher_kind == tx.voucher_kind == VoucherKind.DEBIT_NOTE
    assert (result.party.ledger, result.party.method) == ("Sharma Electronics", "gstin")
    assert entries(tx) == [
        ("Purchase", CR, D("10000.00")),
        ("Input CGST", CR, D("900.00")),
        ("Input SGST", CR, D("900.00")),
        ("Sharma Electronics", DR, D("11800.00")),
    ]
    assert tx.narration.startswith("Debit note DT/DN/4 dated 28-09-2026 to Sharma Electronics")

    # A supplier not in Tally yet: the reviewer marks the note as a purchase.
    stranger = purchase(document_type="debit_note", seller=COMPANY, buyer=NEW_AGE)
    assert build_voucher(stranger, ctx()).direction == "sales"
    chosen = build_voucher(stranger, ctx(), LedgerChoices(direction="purchase"))
    assert chosen.voucher_kind == VoucherKind.DEBIT_NOTE
    assert chosen.proposed_party.name == "New Age Supplies Pvt Ltd"
    assert chosen.proposed_party.parent_group == "Sundry Creditors"
    assert entries(built(chosen))[-1] == ("New Age Supplies Pvt Ltd", DR, D("11800.00"))


RCM_LEDGERS = LEDGERS + (
    LedgerInfo("CGST RCM Payable", "Duties & Taxes"),
    LedgerInfo("SGST RCM Payable", "Duties & Taxes"),
    LedgerInfo("IGST RCM Payable", "Duties & Taxes"),
)


@pytest.mark.parametrize(
    ("total", "round_off", "owed"),
    [
        ("10000", None, "10000.00"),  # the tax left out of the total, as the law expects
        ("11800", None, "10000.00"),  # the tax printed into the total
        ("10000.40", ("Round Off", DR, D("0.40")), "10000.40"),
    ],
)
def test_reverse_charge_purchase_books_the_tax_liability(total, round_off, owed):
    result = build_voucher(purchase(reverse_charge=True, grand_total=D(total)), ctx(RCM_LEDGERS))
    tx = built(result)

    taxes = {k: v for k, v in result.tax_ledgers.items() if k != "round_off"}
    assert taxes == {
        "cgst": "Input CGST",
        "cgst_rcm": "CGST RCM Payable",
        "sgst": "Input SGST",
        "sgst_rcm": "SGST RCM Payable",
    }
    expected = [
        ("Purchase", DR, D("10000.00")),
        ("Input CGST", DR, D("900.00")),
        ("CGST RCM Payable", CR, D("900.00")),
        ("Input SGST", DR, D("900.00")),
        ("SGST RCM Payable", CR, D("900.00")),
    ]
    expected += [round_off] if round_off else []
    assert entries(tx) == [*expected, ("Sharma Electronics", CR, D(owed))]


def test_reverse_charge_prefers_reverse_charge_input_ledgers():
    ledgers = RCM_LEDGERS + (LedgerInfo("Input IGST (RCM)", "Duties & Taxes"),)
    invoice = igst_purchase(reverse_charge=True, grand_total=D("10000"))
    result = build_voucher(invoice, ctx(ledgers))
    assert result.tax_ledgers == {"igst": "Input IGST (RCM)", "igst_rcm": "IGST RCM Payable"}
    built(result)


def test_reverse_charge_needs_a_liability_ledger():
    result = build_voucher(purchase(reverse_charge=True, grand_total=D("10000")), ctx())
    assert result.transaction is None
    assert msgs(result) == [
        "This invoice is under reverse charge, so the company pays the CGST of Rs 900.00 to the "
        "government itself, but no ledger for that liability was found. Create a ledger named "
        "'CGST RCM Payable' under Duties & Taxes in Tally, then sync ledgers.",
        "This invoice is under reverse charge, so the company pays the SGST of Rs 900.00 to the "
        "government itself, but no ledger for that liability was found. Create a ledger named "
        "'SGST RCM Payable' under Duties & Taxes in Tally, then sync ledgers.",
    ]


def test_reverse_charge_totals_mismatch():
    invoice = purchase(reverse_charge=True, grand_total=D("12500"))
    result = build_voucher(invoice, ctx(RCM_LEDGERS))
    assert result.transaction is None
    assert msgs(result) == [
        "The amounts do not add up: under reverse charge the invoice total should be the "
        "taxable value Rs 10,000.00, or Rs 11,800.00 with the tax of Rs 1,800.00, but it is "
        "Rs 12,500.00. Correct the amounts on the review screen so they agree."
    ]


def test_reverse_charge_sale_books_no_tax():
    result = build_voucher(sale(reverse_charge=True), ctx())
    tx = built(result)
    assert result.tax_ledgers == {}
    assert entries(tx) == [
        ("Sales", CR, D("10000.00")),
        ("Gupta Retail", DR, D("10000.00")),
    ]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"document_type": "proforma"}, "proforma invoice, which is not an accounting document"),
        ({"document_type": "receipt"}, "Receipts are not supported yet"),
        ({"is_invoice": False}, "This document is not an invoice"),
    ],
)
def test_documents_that_are_not_posted(overrides, expected):
    result = build_voucher(purchase(**overrides), ctx())
    assert result.transaction is None
    assert len(msgs(result)) == 1 and expected in msgs(result)[0]
    assert result.party.method == result.item.method == "none"


# -- direction ---------------------------------------------------------------------------------


def test_direction_by_company_name():
    unregistered = ctx()
    buyer = Party(name="DEMO TRADERS PRIVATE LIMITED")
    as_buyer = build_voucher(purchase(buyer=buyer), unregistered)
    assert as_buyer.direction == "purchase"
    assert as_buyer.direction_reason.startswith("The buyer's name matches the company name")

    seller = Party(name="Demo Traders")
    as_seller = build_voucher(sale(seller=seller), unregistered)
    assert as_seller.direction == "sales"
    assert as_seller.direction_reason.startswith("The seller's name matches the company name")
    built(as_seller)


def test_firm_with_another_legal_form_is_not_the_company():
    unregistered = AccountingContext(
        company_name="Demo Traders Pvt Ltd",
        company_gstin=None,
        company_state_code="29",
        ledgers=LEDGERS,
    )
    invoice = purchase(seller=Party(name="Demo Traders LLP"), buyer=Party(name="Kapoor Textiles"))
    result = build_voucher(invoice, unregistered)
    assert result.direction == "purchase"
    assert "purchase was assumed" in result.direction_reason


def test_unknown_direction_assumes_purchase():
    invoice = purchase(buyer=Party(name="Someone Else Enterprises"))
    result = build_voucher(invoice, ctx())

    assert result.direction == "purchase"
    assert "purchase was assumed" in result.direction_reason
    assert result.party.ledger == "Sharma Electronics"


# -- identity and balance ----------------------------------------------------------------------


def test_voucher_id_is_kept_across_reruns():
    voucher_id = uuid.uuid4()
    first = build_voucher(purchase(), ctx(), voucher_id=voucher_id)
    edited = build_voucher(purchase(grand_total=D("11800.40")), ctx(), voucher_id=voucher_id)
    assert built(first).id == built(edited).id == voucher_id

    assert build_voucher(purchase(), ctx()).transaction.id != voucher_id


@pytest.mark.parametrize(
    "invoice",
    [
        purchase(),
        igst_purchase(),
        sale(),
        purchase(grand_total=D("11799.01")),
        sale(grand_total=D("11800.99")),
        purchase(seller=NEW_AGE),
        purchase(cess=D("120.50"), grand_total=D("11920.50")),
        sale(document_type="credit_note", grand_total=D("-11800.50")),
        purchase(document_type="credit_note", grand_total=D("11799.50")),
        sale(document_type="debit_note"),
        purchase(document_type="debit_note"),
        purchase(document_type="bill_of_supply", cgst=D("0"), sgst=D("0"), grand_total=D("10000")),
        purchase(document_type="debit_note", seller=COMPANY, buyer=SHARMA),
        purchase(reverse_charge=True),
        purchase(reverse_charge=True, grand_total=D("9999.30")),
        purchase(reverse_charge=True, document_type="credit_note", grand_total=D("10000.50")),
        igst_purchase(reverse_charge=True, cess=D("120.50"), grand_total=D("10000")),
        sale(reverse_charge=True, grand_total=D("10000.20")),
        purchase(seller=Party(name="Kapur Textiles")),
    ],
)
def test_every_built_transaction_balances(invoice):
    ledgers = RCM_LEDGERS + (
        LedgerInfo("Input Cess", "Duties & Taxes"),
        LedgerInfo("Cess RCM Payable", "Duties & Taxes"),
    )
    tx = built(build_voucher(invoice, ctx(ledgers)))
    assert all(e.amount > 0 for e in tx.entries)
    assert sum(e.is_party for e in tx.entries) == 1
    assert CanonicalTransaction.model_validate(tx.model_dump()) == tx


# -- helpers -----------------------------------------------------------------------------------


def test_rupee_formatting():
    assert inr(D("11800")) == "Rs 11,800.00"
    assert inr(D("118000")) == "Rs 1,18,000.00"
    assert inr(D("0.5")) == "Rs 0.50"
    assert inr(D("-1234567.895")) == "Rs -12,34,567.90"


def test_display_name():
    assert display_name("M/S. SHARMA'S TRADING CO. (INDIA)") == "Sharma's Trading Co. (India)"
    assert display_name("  ABC Traders  ") == "ABC Traders"
