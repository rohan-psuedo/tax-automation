from datetime import date
from decimal import Decimal

import pytest

from app.accounting.types import AccountingProblem, AccountingResult, LedgerMatch
from app.schemas.canonical import ProposedLedger, VoucherKind
from app.schemas.invoice import InvoiceLine, NormalizedInvoice, Party
from app.validation.engine import KEY_FIELDS, VALID_GST_RATES, format_inr, validate
from app.validation.types import ValidationReport

TODAY = date(2026, 10, 2)
D = Decimal


def make_invoice(**overrides) -> NormalizedInvoice:
    """Intra-state purchase in Karnataka: 10,000 taxable + 9% CGST + 9% SGST = 11,800."""
    fields = dict(
        invoice_number="SE/2026/0042",
        invoice_date=date(2026, 9, 28),
        seller=Party(
            name="Sharma Electronics",
            gstin="29ABCDE1234F1Z5",
            gstin_valid=True,
            state_code="29",
            state="Karnataka",
        ),
        buyer=Party(
            name="Demo Traders",
            gstin="29AAACD1234A1Z9",
            gstin_valid=True,
            state_code="29",
            state="Karnataka",
        ),
        lines=[InvoiceLine(description="Router", taxable_value=D("10000"), gst_rate=D("18"))],
        taxable_value=D("10000"),
        cgst=D("900"),
        sgst=D("900"),
        grand_total=D("11800"),
        confidence={"invoice_number": 0.95, "grand_total": 0.97},
    )
    fields.update(overrides)
    return NormalizedInvoice(**fields)


def make_accounting(**overrides) -> AccountingResult:
    fields = dict(
        direction="purchase",
        direction_reason="The buyer GSTIN on the invoice is the company's GSTIN.",
        voucher_kind=VoucherKind.PURCHASE,
        party=LedgerMatch(ledger="Sharma Electronics", method="gstin", score=1.0),
        item=LedgerMatch(ledger="Purchase", method="learned", score=0.98),
    )
    fields.update(overrides)
    return AccountingResult(**fields)


def run(
    invoice: NormalizedInvoice | None = None,
    accounting: AccountingResult | None = None,
    *,
    always_review: bool = False,
    auto_create_ledgers: bool = False,
    duplicates: list[str] | None = None,
    calls: list | None = None,
) -> ValidationReport:
    def find_duplicates(gstin, name, number):
        if calls is not None:
            calls.append((gstin, name, number))
        return duplicates or []

    return validate(
        invoice or make_invoice(),
        accounting or make_accounting(),
        always_review=always_review,
        auto_create_ledgers=auto_create_ledgers,
        find_duplicates=find_duplicates,
        today=TODAY,
    )


def codes(report: ValidationReport) -> list[str]:
    return [i.code for i in report.issues]


def only(report: ValidationReport, code: str):
    found = [i for i in report.issues if i.code == code]
    assert len(found) == 1, codes(report)
    return found[0]


def seller(**changes) -> Party:
    return make_invoice().seller.model_copy(update=changes)


def buyer(**changes) -> Party:
    return make_invoice().buyer.model_copy(update=changes)


# --- routing, confidence, formatting ---------------------------------------------------


def test_clean_invoice_is_ready():
    report = run()
    assert report.issues == []
    assert report.route == "ready"
    assert not report.has_errors
    assert report.confidence == pytest.approx(0.95)


def test_always_review_sends_clean_invoice_to_review():
    report = run(always_review=True)
    assert report.issues == []
    assert report.route == "needs_review"


def test_a_warning_alone_sends_to_review():
    report = run(make_invoice(reverse_charge=True))
    assert not report.has_errors
    assert report.route == "needs_review"


@pytest.mark.parametrize(
    "amount, text",
    [
        (D("11800"), "₹11,800.00"),
        (D("1234567"), "₹12,34,567.00"),
        (D("123456789.5"), "₹12,34,56,789.50"),
        (D("999"), "₹999.00"),
        (D("1000"), "₹1,000.00"),
        (D("0.005"), "₹0.01"),
        (D("0"), "₹0.00"),
        (D("-100000"), "-₹1,00,000.00"),
    ],
)
def test_format_inr_uses_indian_grouping(amount, text):
    assert format_inr(amount) == text


def test_messages_use_indian_money_format():
    report = run(make_invoice(grand_total=D("1234567")))
    assert "₹12,34,567.00" in only(report, "totals_mismatch").message


def test_format_inr_handles_amounts_beyond_default_precision():
    assert format_inr(D(10) ** 26) == "₹10," + "00," * 11 + "000.00"
    assert format_inr(D("1E+40")).endswith(",000.00")


@pytest.mark.parametrize(
    "overrides",
    [
        {"grand_total": D(10) ** 26},
        {"grand_total": D("1E+40")},
        {"round_off": D(10) ** 26},
    ],
)
def test_huge_misread_amounts_are_reported_not_raised(overrides):
    assert "totals_mismatch" in codes(run(make_invoice(**overrides)))


def test_huge_consistent_invoice_does_not_raise():
    t = D(10) ** 27
    invoice = make_invoice(
        lines=[InvoiceLine(taxable_value=t, gst_rate=D("18"))],
        taxable_value=t,
        cgst=t * D("0.09"),
        sgst=t * D("0.09"),
        grand_total=t * D("1.18"),
    )
    assert run(invoice).issues == []


def test_confidence_is_lowest_of_fields_party_and_item():
    invoice = make_invoice(confidence={"invoice_number": 0.8, "notes": 0.1})
    assert run(invoice).confidence == pytest.approx(0.8)  # non-key fields are ignored
    weak_item = make_accounting(item=LedgerMatch(ledger="Purchase", method="exact", score=0.75))
    assert run(invoice, weak_item).confidence == pytest.approx(0.75)


def test_confidence_is_clamped():
    accounting = make_accounting(
        party=LedgerMatch(ledger="Sharma Electronics", method="gstin", score=1.5),
        item=LedgerMatch(ledger="Purchase", method="exact", score=1.2),
    )
    assert run(make_invoice(confidence={}), accounting).confidence == 1.0
    # Invoice confidences are range-checked by the schema; match scores are not.
    negative = make_accounting(
        party=LedgerMatch(ledger="Sharma Electronics", method="gstin", score=-0.2)
    )
    assert run(accounting=negative).confidence == 0.0


def test_errors_come_before_warnings_in_stable_order():
    invoice = make_invoice(
        reverse_charge=True,  # warning
        invoice_number=None,  # error
        round_off=D("2"),  # warning
        grand_total=D("11802"),
        document_type="credit_note",  # warning
        invoice_date=None,  # error
    )
    report = run(invoice)
    severities = [i.severity for i in report.issues]
    assert severities == sorted(severities, key=lambda s: s != "error")
    assert codes(report) == [
        "missing_invoice_number",
        "missing_invoice_date",
        "large_round_off",
        "reverse_charge",
        "note_document",
    ]


# --- document type -------------------------------------------------------------------


def test_not_an_invoice():
    issue = only(run(make_invoice(is_invoice=False)), "not_invoice")
    assert issue.severity == "error"
    assert issue.field == "is_invoice"


@pytest.mark.parametrize("document_type", ["proforma", "receipt"])
def test_proforma_and_receipt_are_not_invoices(document_type):
    issue = only(run(make_invoice(document_type=document_type)), "not_invoice")
    assert issue.severity == "error"
    assert issue.field == "document_type"
    assert "Reject it" in issue.message


def test_not_invoice_hides_follow_on_issues():
    invoice = make_invoice(is_invoice=False, invoice_number=None, grand_total=None)
    problem = AccountingProblem(code="missing_grand_total", message="The total is missing.")
    report = run(invoice, make_accounting(problems=[problem]))
    assert codes(report) == ["not_invoice"]
    assert report.route == "needs_review"


@pytest.mark.parametrize("document_type", ["credit_note", "debit_note"])
def test_notes_ask_to_check_the_voucher_type(document_type):
    issue = only(run(make_invoice(document_type=document_type)), "note_document")
    assert issue.severity == "warning"
    assert document_type.replace("_", " ") in issue.message
    assert "voucher type" in issue.message


# --- missing fields ------------------------------------------------------------------


def test_missing_required_fields():
    invoice = make_invoice(
        invoice_number="  ",
        invoice_date=None,
        grand_total=None,
        seller=seller(name=None),
        confidence={"invoice_number": 0.1, "invoice_date": 0.1, "grand_total": 0.1},
    )
    report = run(invoice)
    assert codes(report) == [
        "missing_invoice_number",
        "missing_invoice_date",
        "missing_grand_total",
        "missing_party_name",
    ]
    assert all(i.severity == "error" for i in report.issues)
    assert only(report, "missing_party_name").field == "seller.name"
    assert "supplier" in only(report, "missing_party_name").message


def test_missing_party_name_is_the_buyer_on_sales():
    invoice = make_invoice(buyer=buyer(name=None), seller=seller(name=None))
    accounting = make_accounting(direction="sales", voucher_kind=VoucherKind.SALES)
    issue = only(run(invoice, accounting), "missing_party_name")
    assert issue.field == "buyer.name"
    assert "customer" in issue.message


def test_missing_own_company_name_is_not_an_error():
    report = run(make_invoice(buyer=buyer(name=None)))
    assert "missing_party_name" not in codes(report)


def _walk_in_sale(party: LedgerMatch, **overrides) -> tuple[NormalizedInvoice, AccountingResult]:
    invoice = make_invoice(buyer=Party(state_code="29", state="Karnataka"), **overrides)
    accounting = make_accounting(
        direction="sales",
        voucher_kind=VoucherKind.SALES,
        party=party,
        item=LedgerMatch(ledger="Sales", method="learned", score=1.0),
    )
    return invoice, accounting


def test_unnamed_unregistered_customer_is_fine_once_the_ledger_is_picked():
    cash = LedgerMatch(ledger="Cash", method="choice", score=1.0)
    assert run(*_walk_in_sale(cash)).issues == []
    large = _walk_in_sale(
        cash,
        lines=[InvoiceLine(taxable_value=D("100000"), gst_rate=D("18"))],
        taxable_value=D("100000"),
        cgst=D("9000"),
        sgst=D("9000"),
        grand_total=D("118000"),
    )
    assert run(*large).issues == []


def test_unnamed_customer_below_the_b2c_limit_is_not_missing_a_name():
    unmatched = LedgerMatch(ledger=None, method="none", score=0.0)
    invoice, accounting = _walk_in_sale(unmatched)
    assert "missing_party_name" not in codes(run(invoice, accounting))


def test_unnamed_customer_still_needs_a_name_when_the_law_requires_one():
    unmatched = LedgerMatch(ledger=None, method="none", score=0.0)
    large = _walk_in_sale(
        unmatched,
        lines=[InvoiceLine(taxable_value=D("50000"), gst_rate=D("18"))],
        taxable_value=D("50000"),
        cgst=D("4500"),
        sgst=D("4500"),
        grand_total=D("59000"),
    )
    assert codes(run(*large)) == ["missing_party_name"]
    cash = LedgerMatch(ledger="Cash", method="choice", score=1.0)
    invoice, accounting = _walk_in_sale(cash)
    registered = invoice.model_copy(
        update={"buyer": buyer(name=None)}  # a registered customer must be named
    )
    assert codes(run(registered, accounting)) == ["missing_party_name"]
    purchase = make_invoice(seller=seller(name=None))
    chosen = make_accounting(party=LedgerMatch(ledger="Cash", method="choice", score=1.0))
    assert codes(run(purchase, chosen)) == ["missing_party_name"]


def test_missing_grand_total_skips_tax_math():
    invoice = make_invoice(
        grand_total=None,
        taxable_value=D("5000"),  # would mismatch the lines and the tax rate
    )
    assert codes(run(invoice)) == ["missing_grand_total"]


# --- dates ---------------------------------------------------------------------------


def test_future_date_is_an_error():
    issue = only(run(make_invoice(invoice_date=date(2026, 10, 3))), "future_date")
    assert issue.severity == "error"
    assert "03 Oct 2026" in issue.message


def test_old_invoice_is_a_warning():
    issue = only(run(make_invoice(invoice_date=date(2025, 10, 1))), "old_invoice")
    assert issue.severity == "warning"
    assert issue.field == "invoice_date"


@pytest.mark.parametrize("invoice_date", [TODAY, date(2025, 10, 2)])
def test_today_and_exactly_a_year_old_are_fine(invoice_date):
    assert run(make_invoice(invoice_date=invoice_date)).issues == []


# --- GSTIN and direction -------------------------------------------------------------


def test_invalid_gstins():
    invoice = make_invoice(
        seller=seller(gstin="29ABCDE1234F1Z0", gstin_valid=False),
        buyer=buyer(gstin_valid=False),
        confidence={"seller.gstin": 0.3},
    )
    report = run(invoice)
    issues = [i for i in report.issues if i.code == "invalid_gstin"]
    assert [i.field for i in issues] == ["seller.gstin", "buyer.gstin"]
    assert all(i.severity == "error" for i in issues)
    assert "29ABCDE1234F1Z0" in issues[0].message
    assert "misread" in issues[0].message
    assert "low_confidence" not in codes(report)  # same root cause, reported once


def test_missing_gstin_is_not_invalid():
    assert run(make_invoice(seller=seller(gstin=None, gstin_valid=False))).issues == []


def test_company_not_on_invoice():
    accounting = make_accounting(
        direction_reason="Neither party matches the company, so purchase was assumed."
    )
    issue = only(run(accounting=accounting), "company_not_on_invoice")
    assert issue.severity == "warning"
    assert "purchase" in issue.message


# --- amounts ---------------------------------------------------------------------------


def test_lines_total_mismatch():
    invoice = make_invoice(
        lines=[
            InvoiceLine(taxable_value=D("6000"), gst_rate=D("18")),
            InvoiceLine(taxable_value=D("3000"), gst_rate=D("18")),
        ]
    )
    report = run(invoice)
    issue = only(report, "lines_total_mismatch")
    assert issue.severity == "warning"
    assert "₹9,000.00" in issue.message and "₹10,000.00" in issue.message
    assert codes(report) == ["lines_total_mismatch"]  # the line misread, reported once


def test_trade_discount_is_reported_once():
    # The lines are before an invoice-level discount; GST is on the discounted value.
    invoice = make_invoice(
        taxable_value=D("9000"), cgst=D("810"), sgst=D("810"), grand_total=D("10620")
    )
    assert codes(run(invoice)) == ["lines_total_mismatch"]


def test_lines_total_within_one_rupee_is_fine():
    invoice = make_invoice(lines=[InvoiceLine(taxable_value=D("9999.20"), gst_rate=D("18"))])
    assert "lines_total_mismatch" not in codes(run(invoice))


def test_lines_without_values_are_not_compared():
    invoice = make_invoice(lines=[InvoiceLine(description="Router")])
    assert run(invoice).issues == []


def test_totals_mismatch():
    issue = only(run(make_invoice(grand_total=D("11880"))), "totals_mismatch")
    assert issue.severity == "error"
    assert issue.field == "grand_total"
    assert "₹11,800.00" in issue.message and "₹11,880.00" in issue.message


def test_totals_include_cess_and_round_off():
    invoice = make_invoice(cess=D("100"), round_off=D("-0.40"), grand_total=D("11899.60"))
    assert run(invoice).issues == []
    off_by_a_rupee = make_invoice(grand_total=D("11801"))
    assert run(off_by_a_rupee).issues == []


def test_large_round_off():
    invoice = make_invoice(round_off=D("-1.50"), grand_total=D("11798.50"))
    issue = only(run(invoice), "large_round_off")
    assert issue.severity == "warning"
    assert "-₹1.50" in issue.message


def test_mixed_gst_reported_once():
    invoice = make_invoice(cgst=D("900"), sgst=D("0"), igst=D("900"))
    report = run(invoice)
    assert codes(report) == ["mixed_gst"]
    assert only(report, "mixed_gst").severity == "error"


def test_cgst_sgst_unequal_error():
    invoice = make_invoice(cgst=D("900"), sgst=D("90"), grand_total=D("10990"))
    report = run(invoice)
    issue = only(report, "cgst_sgst_unequal")
    assert issue.severity == "error"
    assert "₹810.00" in issue.message
    assert "tax_rate_mismatch" not in codes(report)  # same misread, reported once


def test_one_misread_tax_amount_is_reported_once():
    # The grand total on the document is right; only one tax figure was misread.
    assert codes(run(make_invoice(sgst=D("90")))) == ["cgst_sgst_unequal"]
    assert codes(run(make_invoice(igst=D("1800")))) == ["mixed_gst"]
    igst_in_cgst_column = make_invoice(
        buyer=buyer(state_code="27", state="Maharashtra"), cgst=D("1800"), sgst=D("0")
    )
    assert codes(run(igst_in_cgst_column)) == ["cgst_sgst_unequal"]


def test_tax_split_issues_cover_every_tax_field_they_name():
    misread_sgst = make_invoice(sgst=D("90"), confidence={"sgst": 0.3})
    assert codes(run(misread_sgst)) == ["cgst_sgst_unequal"]
    misread_cgst = make_invoice(igst=D("1800"), confidence={"cgst": 0.3, "sgst": 0.3})
    assert codes(run(misread_cgst)) == ["mixed_gst"]


def test_cgst_sgst_unequal_warning_for_rounding():
    invoice = make_invoice(cgst=D("900.50"), sgst=D("900"), grand_total=D("11800.50"))
    issue = only(run(invoice), "cgst_sgst_unequal")
    assert issue.severity == "warning"


def test_cgst_sgst_one_paisa_apart_is_fine():
    invoice = make_invoice(cgst=D("900.01"), sgst=D("900"), grand_total=D("11800.01"))
    assert run(invoice).issues == []


def test_igst_within_one_state_is_wrong_gst_type():
    invoice = make_invoice(cgst=D("0"), sgst=D("0"), igst=D("1800"))
    issue = only(run(invoice), "wrong_gst_type")
    assert issue.severity == "warning"
    assert "Karnataka (29)" in issue.message


def test_cgst_sgst_between_states_is_wrong_gst_type():
    invoice = make_invoice(buyer=buyer(state_code="27", state="Maharashtra"))
    issue = only(run(invoice), "wrong_gst_type")
    assert "Karnataka (29)" in issue.message and "Maharashtra (27)" in issue.message


def test_place_of_supply_overrides_buyer_state():
    invoice = make_invoice(
        buyer=buyer(state_code="27", state="Maharashtra"), place_of_supply_code="29"
    )
    assert run(invoice).issues == []
    inter_state = make_invoice(place_of_supply_code="33")
    assert "state 33" in only(run(inter_state), "wrong_gst_type").message


@pytest.mark.parametrize(
    "current, legacy, state",
    [
        ("37", "28", "Andhra Pradesh"),
        ("26", "25", "Dadra and Nagar Haveli and Daman and Diu"),
    ],
)
def test_legacy_place_of_supply_code_counts_as_its_successor(current, legacy, state):
    invoice = make_invoice(
        seller=seller(gstin=f"{current}ABCDE1234F1Z5", state_code=current, state=state),
        buyer=buyer(gstin=f"{current}AAACD1234A1Z9", state_code=current, state=state),
        place_of_supply_code=legacy,
    )
    assert run(invoice).issues == []
    elsewhere = make_invoice(place_of_supply_code=legacy)  # seller in Karnataka
    assert codes(run(elsewhere)) == ["wrong_gst_type"]


def test_unknown_state_skips_gst_type_check():
    invoice = make_invoice(seller=seller(state_code=None), igst=D("1800"), cgst=D("0"), sgst=D("0"))
    assert run(invoice).issues == []


def test_invalid_gst_rate_once_per_distinct_rate():
    invoice = make_invoice(
        lines=[
            InvoiceLine(taxable_value=D("4000"), gst_rate=D("13")),
            InvoiceLine(taxable_value=D("3000"), gst_rate=D("13.00")),
            InvoiceLine(taxable_value=D("3000"), gst_rate=D("19")),
        ]
    )
    report = run(invoice)
    issues = [i for i in report.issues if i.code == "invalid_gst_rate"]
    assert [i.severity for i in issues] == ["warning", "warning"]
    assert "13%" in issues[0].message and "19%" in issues[1].message
    assert "tax_rate_mismatch" not in codes(report)  # the rate itself is suspect


SPEC_GST_RATES = ["0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40"]


def test_valid_gst_rates():
    assert VALID_GST_RATES == {D(r) for r in SPEC_GST_RATES}
    lines = [
        InvoiceLine(taxable_value=D("5000"), gst_rate=D("18.0")),
        InvoiceLine(taxable_value=D("5000"), gst_rate=D("18")),
    ]
    assert run(make_invoice(lines=lines)).issues == []


@pytest.mark.parametrize("rate", SPEC_GST_RATES)
def test_every_valid_rate_is_accepted(rate):
    invoice = make_invoice(lines=[InvoiceLine(taxable_value=D("10000"), gst_rate=D(rate))])
    assert "invalid_gst_rate" not in codes(run(invoice))


def test_tax_rate_mismatch():
    invoice = make_invoice(lines=[InvoiceLine(taxable_value=D("10000"), gst_rate=D("12"))])
    issue = only(run(invoice), "tax_rate_mismatch")
    assert issue.severity == "warning"
    assert "₹1,200.00" in issue.message and "₹1,800.00" in issue.message


def test_tax_rate_tolerance_is_half_a_percent_of_expected_tax():
    big = make_invoice(
        lines=[InvoiceLine(taxable_value=D("1000000"), gst_rate=D("18"))],
        taxable_value=D("1000000"),
        cgst=D("90400"),  # 800 over the expected 1,80,000; tolerance is 900
        sgst=D("90400"),
        grand_total=D("1180800"),
    )
    assert run(big).issues == []
    over = make_invoice(cgst=D("905"), sgst=D("905"), grand_total=D("11810"))
    assert codes(run(over)) == ["tax_rate_mismatch"]  # ₹10 over; tolerance is ₹9
    small = make_invoice(
        lines=[InvoiceLine(taxable_value=D("100"), gst_rate=D("18"))],
        taxable_value=D("100"),
        cgst=D("10"),
        sgst=D("10"),
        grand_total=D("120"),
    )
    assert codes(run(small)) == ["tax_rate_mismatch"]  # ₹2 over; tolerance floors at ₹1


def test_tax_rate_check_needs_a_rate_on_every_line():
    invoice = make_invoice(
        lines=[
            InvoiceLine(taxable_value=D("5000"), gst_rate=D("5")),
            InvoiceLine(taxable_value=D("5000"), gst_rate=None),
        ]
    )
    assert run(invoice).issues == []


def test_totals_mismatch_skips_rate_check():
    invoice = make_invoice(cgst=D("9000"), sgst=D("9000"))
    assert codes(run(invoice)) == ["totals_mismatch"]


def _credit_note(**overrides) -> NormalizedInvoice:
    """The fixture invoice as a credit note printed with minus signs."""
    fields = dict(
        document_type="credit_note",
        lines=[InvoiceLine(taxable_value=D("-10000"), gst_rate=D("18"))],
        taxable_value=D("-10000"),
        cgst=D("-900"),
        sgst=D("-900"),
        grand_total=D("-11800"),
    )
    fields.update(overrides)
    return make_invoice(**fields)


def test_negative_credit_note_is_checked_like_a_positive_one():
    assert codes(run(_credit_note())) == ["note_document"]
    mixed = _credit_note(cgst=D("-450"), sgst=D("-450"), igst=D("-900"))
    assert codes(run(mixed)) == ["mixed_gst", "note_document"]
    igst_within_state = _credit_note(cgst=D("0"), sgst=D("0"), igst=D("-1800"))
    assert codes(run(igst_within_state)) == ["wrong_gst_type", "note_document"]
    between_states = _credit_note(buyer=buyer(state_code="27", state="Maharashtra"))
    assert codes(run(between_states)) == ["wrong_gst_type", "note_document"]


def test_negative_tax_gets_the_same_rate_tolerance():
    big = _credit_note(
        lines=[InvoiceLine(taxable_value=D("-1000000"), gst_rate=D("18"))],
        taxable_value=D("-1000000"),
        cgst=D("-90400"),  # 800 over the expected 1,80,000; tolerance is 900
        sgst=D("-90400"),
        grand_total=D("-1180800"),
    )
    assert codes(run(big)) == ["note_document"]


# --- flags ---------------------------------------------------------------------------


def test_reverse_charge():
    issue = only(run(make_invoice(reverse_charge=True)), "reverse_charge")
    assert issue.severity == "warning"
    assert "recipient" in issue.message


def _inter_state_rcm(**overrides) -> NormalizedInvoice:
    """GTA freight from Maharashtra to Karnataka at 5% under reverse charge."""
    fields = dict(
        reverse_charge=True,
        seller=seller(gstin="27ABCDE1234F1Z5", state_code="27", state="Maharashtra"),
        lines=[InvoiceLine(description="Freight", taxable_value=D("10000"), gst_rate=D("5"))],
        cgst=D("0"),
        sgst=D("0"),
        igst=D("500"),
        grand_total=D("10000"),
    )
    fields.update(overrides)
    return make_invoice(**fields)


def test_reverse_charge_total_may_leave_out_the_tax():
    assert codes(run(_inter_state_rcm())) == ["reverse_charge"]
    assert codes(run(_inter_state_rcm(grand_total=D("10500")))) == ["reverse_charge"]
    report = run(_inter_state_rcm(grand_total=D("10250")))
    assert codes(report) == ["totals_mismatch", "reverse_charge"]
    assert "₹10,000.00" in only(report, "totals_mismatch").message


def test_tax_left_out_of_the_total_is_a_mismatch_without_reverse_charge():
    report = run(_inter_state_rcm(reverse_charge=False))
    assert codes(report) == ["totals_mismatch"]


def test_reverse_charge_without_tax_skips_rate_check():
    assert codes(run(_inter_state_rcm(igst=D("0")))) == ["reverse_charge"]
    intra_state = make_invoice(
        reverse_charge=True, cgst=D("0"), sgst=D("0"), grand_total=D("10000")
    )
    assert codes(run(intra_state)) == ["reverse_charge"]
    printed_wrong = _inter_state_rcm(igst=D("1800"))  # tax printed, so the rate is checked
    assert codes(run(printed_wrong)) == ["tax_rate_mismatch", "reverse_charge"]


# --- duplicates ----------------------------------------------------------------------


def test_duplicate_lookup_uses_supplier_on_purchase():
    calls: list = []
    report = run(make_invoice(invoice_number=" SE/2026/0042 "), calls=calls)
    assert calls == [("29ABCDE1234F1Z5", "Sharma Electronics", "SE/2026/0042")]
    assert report.issues == []


def test_duplicate_lookup_uses_customer_on_sales():
    calls: list = []
    invoice = make_invoice(buyer=buyer(name="Acme Retail", gstin=None))
    accounting = make_accounting(
        direction="sales",
        voucher_kind=VoucherKind.SALES,
        party=LedgerMatch(ledger="Acme Retail", method="exact", score=1.0),
        item=LedgerMatch(ledger="Sales", method="learned", score=1.0),
    )
    run(invoice, accounting, calls=calls)
    assert calls == [(None, "Acme Retail", "SE/2026/0042")]


def test_duplicate_invoice_lists_the_other_entries():
    found = ["Entry #12 dated 28 Sep 2026 (posted)", "Entry #15 (needs review)"]
    issue = only(run(duplicates=found), "duplicate_invoice")
    assert issue.severity == "error"
    assert issue.field == "invoice_number"
    assert found[0] in issue.message and found[1] in issue.message
    assert "Sharma Electronics" in issue.message


def test_no_duplicate_lookup_without_invoice_number():
    calls: list = []
    run(make_invoice(invoice_number=None), calls=calls)
    assert calls == []


# --- accounting results ----------------------------------------------------------------


def test_each_accounting_problem_is_an_error_with_its_code_and_field():
    problems = [
        AccountingProblem(
            code="missing_taxable_value",
            message="The taxable value is missing.",
            field="taxable_value",
        ),
        AccountingProblem(
            code="missing_item_ledger",
            message="No purchase ledger exists in Tally.",
            field="item_ledger",
        ),
    ]
    report = run(accounting=make_accounting(problems=problems))
    assert [(i.code, i.severity, i.message, i.field) for i in report.issues] == [
        (p.code, "error", p.message, p.field) for p in problems
    ]


def _unmatched_party(method: str = "none") -> AccountingResult:
    return make_accounting(
        party=LedgerMatch(ledger=None, method=method, score=0.0),
        proposed_party=ProposedLedger(name="Sharma Electronics", parent_group="Sundry Creditors"),
    )


def test_new_party_needs_approval():
    issue = only(run(accounting=_unmatched_party()), "party_ledger_needs_approval")
    assert issue.severity == "error"
    assert "not in Tally yet" in issue.message
    assert '"Sharma Electronics"' in issue.message and "Sundry Creditors" in issue.message
    assert "pick an existing ledger" in issue.message


def test_new_party_with_auto_create_is_a_warning():
    report = run(accounting=_unmatched_party(), auto_create_ledgers=True)
    issue = only(report, "new_party_ledger")
    assert issue.severity == "warning"
    assert "will be created" in issue.message
    assert "party_ledger_needs_approval" not in codes(report)


def test_approved_new_party_is_fine():
    accounting = _unmatched_party(method="choice")
    accounting.party.score = 1.0
    assert run(accounting=accounting).issues == []


def test_weak_party_match():
    accounting = make_accounting(
        party=LedgerMatch(ledger="Sharma Electricals", method="fuzzy", score=0.85)
    )
    issue = only(run(accounting=accounting), "weak_party_match")
    assert issue.severity == "warning"
    assert '"Sharma Electricals"' in issue.message


@pytest.mark.parametrize("score", [0.6, 0.92, 1.0])
def test_every_name_only_match_is_flagged(score):
    # Legal-form words are ignored when matching names, so "ABC Pvt Ltd" and "ABC LLP" (two
    # different firms) can match at 1.0; a person confirms every name-only match.
    accounting = make_accounting(
        party=LedgerMatch(ledger="Sharma Electronics LLP", method="fuzzy", score=score)
    )
    assert only(run(accounting=accounting), "weak_party_match").severity == "warning"


def test_weak_item_ledger():
    accounting = make_accounting(item=LedgerMatch(ledger="Purchase", method="default", score=0.5))
    issue = only(run(accounting=accounting), "weak_item_ledger")
    assert issue.severity == "warning"
    assert '"Purchase"' in issue.message
    confident = make_accounting(item=LedgerMatch(ledger="Purchase", method="default", score=0.7))
    assert run(accounting=confident).issues == []


# --- low confidence ------------------------------------------------------------------


def test_low_confidence_fields_are_warnings():
    invoice = make_invoice(
        confidence={"grand_total": 0.4, "seller.name": 0.5, "buyer.gstin": 0.6, "cgst": 0.9}
    )
    report = run(invoice)
    issues = [i for i in report.issues if i.code == "low_confidence"]
    assert [i.field for i in issues] == ["grand_total", "seller.name"]
    assert all(i.severity == "warning" for i in issues)
    assert "₹11,800.00" in issues[0].message
    assert '"Sharma Electronics"' in issues[1].message
    assert report.confidence == pytest.approx(0.4)


SPEC_KEY_FIELDS = [
    "invoice_number",
    "invoice_date",
    "grand_total",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "seller.gstin",
    "buyer.gstin",
    "seller.name",
    "buyer.name",
]


def test_every_key_field_is_checked_for_low_confidence():
    assert list(KEY_FIELDS) == SPEC_KEY_FIELDS
    report = run(make_invoice(confidence=dict.fromkeys(SPEC_KEY_FIELDS, 0.59)))
    assert [i.field for i in report.issues] == SPEC_KEY_FIELDS
    assert set(codes(report)) == {"low_confidence"}
    assert report.confidence == pytest.approx(0.59)


@pytest.mark.parametrize("path", SPEC_KEY_FIELDS)
def test_each_key_field_feeds_the_confidence_score(path):
    report = run(make_invoice(confidence={path: 0.3}))
    assert [(i.code, i.field) for i in report.issues] == [("low_confidence", path)]
    assert report.confidence == pytest.approx(0.3)


def test_fields_missing_from_confidence_count_as_typed():
    assert run(make_invoice(confidence={})).issues == []


def test_low_confidence_skipped_for_fields_already_reported():
    invoice = make_invoice(
        invoice_number=None,
        invoice_date=date(2026, 12, 1),
        confidence={"invoice_number": 0.2, "invoice_date": 0.2},
    )
    assert codes(run(invoice)) == ["missing_invoice_number", "future_date"]


def test_low_confidence_ignores_non_key_fields():
    invoice = make_invoice(confidence={"place_of_supply_code": 0.1, "lines.0.rate": 0.2})
    assert run(invoice).issues == []


# --- edge cases found by probing ---------------------------------------------------------


@pytest.mark.parametrize("placeholder", ["N/A", "na", " NIL ", "-", "0", "None"])
def test_placeholder_invoice_number_counts_as_missing(placeholder):
    issue = only(run(make_invoice(invoice_number=placeholder)), "missing_invoice_number")
    assert issue.severity == "error" and "placeholder" in issue.message
    assert "invoice_number_format" not in codes(run(make_invoice(invoice_number=placeholder)))


@pytest.mark.parametrize(
    ("number", "flagged"),
    [
        ("SE/2026/0042", False),
        ("INV-000000000001", False),  # 16 characters is the limit
        ("INV-0000000000012", True),  # 17
        ("INV/2026-27/BLR/WAREHOUSE/00042", True),
        ("INV 0042", True),  # space
        ("Invoice No. 42", True),  # label read into the number
    ],
)
def test_invoice_number_format(number, flagged):
    report = run(make_invoice(invoice_number=number))
    assert ("invoice_number_format" in codes(report)) is flagged
    if flagged:
        assert only(report, "invoice_number_format").severity == "warning"


def test_pre_gst_date_replaces_old_invoice_warning():
    report = run(make_invoice(invoice_date=date(2017, 6, 30)))
    assert codes(report) == ["pre_gst_date"]
    assert "1 July 2017" in only(report, "pre_gst_date").message
    assert codes(run(make_invoice(invoice_date=date(2017, 7, 1)))) == ["old_invoice"]


def test_same_gstin_on_both_sides_is_one_error():
    same = make_invoice(
        seller=seller(gstin="29AAACD1234A1Z9"),
        confidence={"seller.gstin": 0.4, "buyer.gstin": 0.4},
    )
    report = run(same)
    assert codes(report) == ["same_gstin_both_sides"]
    issue = only(report, "same_gstin_both_sides")
    assert issue.severity == "error" and issue.field == "seller.gstin"


def test_same_gstin_check_ignores_case_and_spaces():
    report = run(make_invoice(seller=seller(gstin=" 29aaacd1234a1z9 ")))
    assert "same_gstin_both_sides" in codes(report)


def test_long_duplicate_list_is_capped():
    report = run(duplicates=[f"document #{n} (in review)" for n in range(25)])
    message = only(report, "duplicate_invoice").message
    assert "document #2 (in review); and 22 more" in message
    assert "document #3" not in message


def test_intra_state_igst_warning_mentions_sez_and_exports():
    invoice = make_invoice(cgst=D("0"), sgst=D("0"), igst=D("1800"))
    assert "SEZ" in only(run(invoice), "wrong_gst_type").message


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 5.0, -1.0])
def test_confidence_must_be_a_finite_fraction(bad):
    with pytest.raises(ValueError):
        make_invoice(confidence={"grand_total": bad})


def test_non_finite_scores_count_as_zero_confidence():
    # Ledger match scores are not range-checked by their model; they must not read as 100%.
    accounting = make_accounting(
        party=LedgerMatch(ledger="Sharma Electronics", method="gstin", score=float("nan"))
    )
    assert run(accounting=accounting).confidence == 0.0


def test_same_gstin_suppresses_the_party_ledger_symptom():
    proposed = ProposedLedger(name="Demo Traders", parent_group="Sundry Debtors")
    accounting = make_accounting(
        direction="sales",
        party=LedgerMatch(ledger=None, method="none", score=0.0),
        proposed_party=proposed,
    )
    report = run(make_invoice(seller=seller(gstin="29AAACD1234A1Z9")), accounting)
    assert "same_gstin_both_sides" in codes(report)
    assert "party_ledger_needs_approval" not in codes(report)


# --- one root cause, one issue ---------------------------------------------------------------


def _problem(code: str, field: str | None = None) -> AccountingProblem:
    return AccountingProblem(code=code, message=f"engine says {code}", field=field)


@pytest.mark.parametrize(
    ("invoice_changes", "engine_code", "validation_code"),
    [
        ({"invoice_date": None}, "missing_invoice_date", "missing_invoice_date"),
        ({"grand_total": None}, "missing_grand_total", "missing_grand_total"),
        ({"grand_total": D("12300")}, "totals_mismatch", "totals_mismatch"),
        ({"igst": D("1800"), "grand_total": D("13600")}, "totals_mismatch", "mixed_gst"),
        ({"seller": seller(name=None)}, "missing_party_name", "missing_party_name"),
    ],
)
def test_engine_problem_already_reported_is_dropped(invoice_changes, engine_code, validation_code):
    accounting = make_accounting(problems=[_problem(engine_code)])
    report = run(make_invoice(**invoice_changes), accounting)
    assert validation_code in codes(report)
    assert not any(i.message == f"engine says {engine_code}" for i in report.issues)


def test_engine_problem_validation_does_not_cover_is_kept():
    accounting = make_accounting(problems=[_problem("missing_tax_ledger", "tax_ledgers")])
    issue = only(run(accounting=accounting), "missing_tax_ledger")
    assert issue.severity == "error" and issue.field == "tax_ledgers"


def test_engine_totals_problem_is_kept_when_validation_only_warns():
    # Validation saw nothing wrong with the totals, so the engine's finding is the only one.
    accounting = make_accounting(problems=[_problem("totals_mismatch", "grand_total")])
    assert codes(run(accounting=accounting)) == ["totals_mismatch"]


def test_implausible_amount_explains_the_sums_built_on_it():
    huge = make_invoice(grand_total=D("1e20"))
    accounting = make_accounting(problems=[_problem("implausible_amount", "grand_total")])
    assert codes(run(huge, accounting)) == ["implausible_amount"]
