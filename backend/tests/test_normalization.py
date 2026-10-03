from datetime import date
from decimal import Decimal

import pytest

from app.normalization.gst import (
    GST_RATES,
    STATE_CODES,
    clean_gstin,
    gstin_check_char,
    is_valid_gstin,
    state_code_from_gstin,
    state_code_from_name,
)
from app.normalization.normalize import CONFIDENCE, normalize
from app.normalization.values import parse_amount, parse_date
from app.schemas.extraction import (
    ExtractedLineItem,
    ExtractedParty,
    ExtractedTotals,
    ExtractedValue,
    InvoiceExtraction,
)

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def with_check(body: str) -> str:
    return body + gstin_check_char(body)


SELLER_GSTIN = with_check("29AABCK1234L1Z")  # Karnataka
BUYER_GSTIN = "27AAPFU0939F1ZV"  # Maharashtra, sample from the GST documentation


# --- gst.py -----------------------------------------------------------------------------


def test_state_codes_cover_every_gst_code():
    expected = {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}
    assert set(STATE_CODES) == expected
    assert STATE_CODES["29"] == "Karnataka"
    assert STATE_CODES["38"] == "Ladakh"
    assert STATE_CODES["26"] == "Dadra and Nagar Haveli and Daman and Diu"
    assert STATE_CODES["25"] == "Daman and Diu"
    assert STATE_CODES["37"] == "Andhra Pradesh"
    assert STATE_CODES["97"] == "Other Territory"
    assert STATE_CODES["99"] == "Centre Jurisdiction"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27aapfu0939f1zv", "27AAPFU0939F1ZV"),
        (" 27 AAPFU 0939 F1ZV ", "27AAPFU0939F1ZV"),
        ("27-AAPFU-0939-F1ZV", "27AAPFU0939F1ZV"),
        ("27.AAPFU.0939.F1Z.V", "27AAPFU0939F1ZV"),
        ("27AAPFU0939F1ZV\n", "27AAPFU0939F1ZV"),
        ("27–AAPFU–0939–F1ZV", "27AAPFU0939F1ZV"),  # en dash
        ("27—AAPFU—0939—F1ZV", "27AAPFU0939F1ZV"),  # em dash
        ("27−AAPFU−0939−F1ZV", "27AAPFU0939F1ZV"),  # minus sign
        ("27‐AAPFU‑0939‒F1ZV", "27AAPFU0939F1ZV"),  # hyphen, non-breaking hyphen, figure dash
        ("27AAPFU0939F1ZV​", "27AAPFU0939F1ZV"),  # zero-width space
        ("﻿27AAPFU‌0939F1ZV", "27AAPFU0939F1ZV"),
        ("", None),
        ("   ", None),
        (" - ", None),
        (None, None),
    ],
)
def test_clean_gstin(raw, expected):
    assert clean_gstin(raw) == expected


@pytest.mark.parametrize(
    "gstin",
    [
        "27AAPFU0939F1ZV",  # GST documentation sample
        "27AAACR5055K1Z7",
        "29AACCF0683K1ZD",
        "29AAGCB7383J1Z4",
        "27aapfu0939f1zv",  # cleaned before checking
        "27 AAPFU 0939 F1ZV",
    ],
)
def test_valid_published_gstins(gstin):
    assert is_valid_gstin(gstin)


@pytest.mark.parametrize(
    "body",
    ["29AABCK1234L1Z", "07AAACI1681G1Z", "33ABCDE1234F2Z", "38AAAAA0000A1Z", "99ZZZZZ9999Z9Z"],
)
def test_gstin_checksum_round_trip(body):
    gstin = with_check(body)
    assert is_valid_gstin(gstin)
    # Mod-36 Luhn catches every single-character substitution.
    for i, original in enumerate(gstin):
        for replacement in ("0", "7", "A", "Z"):
            if replacement != original:
                mutated = gstin[:i] + replacement + gstin[i + 1 :]
                assert not is_valid_gstin(mutated), mutated


@pytest.mark.parametrize(
    "gstin",
    [
        None,
        "",
        "27AAPFU0939F1ZW",  # wrong check character
        "27AAPFU0939F1Z",  # too short
        "27AAPFU0939F1ZVV",  # too long
        with_check("40AAPFU0939F1Z"),  # unknown state code
        with_check("00AAPFU0939F1Z"),
        "27AAPFU0939F1Z?",
        "UNREGISTERED",
        "27AAPFU०९३९F1ZV",  # Devanagari digits are not GSTIN characters
        "27AAPFU０９３９F1ZV",  # fullwidth digits
        "२७AAPFU0939F1ZV",
    ],
)
def test_invalid_gstins(gstin):
    assert not is_valid_gstin(gstin)


@pytest.mark.parametrize(
    "body",
    [
        "07DELA12345B1D",  # TDS deductor (government office): TAN-based, 'D' in place 14
        "0717UNO00157UN",  # UIN for a UN body or embassy: digits where a PAN has letters
        "9917USA29001OS",  # OIDAR / non-resident registration, centre jurisdiction
    ],
)
def test_registrations_not_built_on_a_pan_are_valid(body):
    gstin = with_check(body)
    assert is_valid_gstin(gstin)
    # The checksum still catches a misread character in these formats.
    misread = gstin[:5] + ("8" if gstin[5] != "8" else "9") + gstin[6:]
    assert not is_valid_gstin(misread)


@pytest.mark.parametrize(
    ("gstin", "code"),
    [
        ("27AAPFU0939F1ZV", "27"),
        ("29 aagcb7383j1z4", "29"),
        ("40AAPFU0939F1ZV", None),
        ("27AAPFU", None),
        (None, None),
    ],
)
def test_state_code_from_gstin(gstin, code):
    assert state_code_from_gstin(gstin) == code


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("Karnataka", "29"),
        ("KARNATAKA", "29"),
        ("karnataka.", "29"),
        ("Delhi", "07"),
        ("NCT of Delhi", "07"),
        ("New Delhi", "07"),
        ("National Capital Territory of Delhi", "07"),
        ("Orissa", "21"),
        ("Odisha", "21"),
        ("Pondicherry", "34"),
        ("Puducherry", "34"),
        ("J&K", "01"),
        ("Jammu & Kashmir", "01"),
        ("Jammu and Kashmir", "01"),
        ("Andaman & Nicobar", "35"),
        ("Andaman and Nicobar Islands", "35"),
        ("Tamil Nadu", "33"),
        ("Tamilnadu", "33"),
        ("West Bengal", "19"),
        ("Chhattisgarh", "22"),
        ("Chattisgarh", "22"),
        ("Uttaranchal", "05"),
        ("Ladakh", "38"),
        ("Telangana", "36"),
        ("Telangana State", "36"),
        ("Andhra Pradesh", "37"),
        # Legacy codes resolve to the code GSTINs use: no GST registration carries 28, and
        # Daman and Diu (25) merged into 26 in 2020.
        ("Andhra Pradesh (Before Division)", "37"),
        ("28-Andhra Pradesh", "37"),
        ("28", "37"),
        ("Dadra & Nagar Haveli & Daman & Diu", "26"),
        ("Daman and Diu", "26"),
        ("Daman & Diu (25)", "26"),
        ("25", "26"),
        ("28-Telangana", None),  # 28 became 37, not 36
        ("Other Territory", "97"),
        ("Centre Jurisdiction", "99"),
        ("UP", "09"),
        ("29-Karnataka", "29"),
        ("29 - Karnataka", "29"),
        ("(29) Karnataka", "29"),
        ("Karnataka (29)", "29"),
        ("Karnataka, State Code: 29", "29"),
        ("29", "29"),
        ("07", "07"),
        ("7", "07"),
        ("State Code: 07", "07"),
        ("Place of Supply: 33-Tamil Nadu", "33"),
        ("Mumbai, Maharashtra", "27"),
        ("Karnataka, India", "29"),
        ("27-Karnataka", None),  # code and name disagree
        ("Karnataka and Kerala", None),
        ("45", None),
        ("Atlantis", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_state_code_from_name(name, code):
    assert state_code_from_name(name) == code


def test_state_code_from_name_reads_every_canonical_name():
    for code, name in STATE_CODES.items():
        expected = {"25": "26", "28": "37"}.get(code, code)
        assert state_code_from_name(name) == expected, name
        assert state_code_from_name(f"{code}-{name}") == expected, name


@pytest.mark.parametrize(
    "rate", ["0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40"]
)
def test_gst_rates_valid(rate):
    assert Decimal(rate) in GST_RATES


@pytest.mark.parametrize("rate", ["2", "10", "15", "24", "0.5"])
def test_gst_rates_invalid(rate):
    assert Decimal(rate) not in GST_RATES


def test_gst_rates_are_decimals_and_ignore_scale():
    assert all(isinstance(rate, Decimal) for rate in GST_RATES)
    assert Decimal("18.00") in GST_RATES


# --- values.py ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₹1,23,456.50", "123456.50"),
        ("₹ 1,23,456.50", "123456.50"),
        ("Rs. 500/-", "500"),
        ("Rs.500/-", "500"),
        ("Rs 500.00 only", "500.00"),
        ("INR 1,000", "1000"),
        ("1 234.00", "1234.00"),
        ("12 34 567", "1234567"),
        ("1 234.50", "1234.50"),
        ("12,34,567", "1234567"),
        ("1,23,45,678.90", "12345678.90"),
        ("1,234,567.89", "1234567.89"),
        ("1,234", "1234"),
        ("1234.5", "1234.5"),
        ("1234", "1234"),
        ("0.75", "0.75"),
        (".5", "0.5"),
        ("0", "0"),
        ("(100)", "-100"),
        ("(₹ 2,500.00)", "-2500.00"),
        ("100-", "-100"),
        ("-100", "-100"),
        ("- 100", "-100"),
        ("−100", "-100"),
        ("₹ -1,000.00", "-1000.00"),
        ("+12.50", "12.50"),
        ("-0.30", "-0.30"),
        ("-0", "0"),
        # Tally prints negatives as "(-)".
        ("(-)0.40", "-0.40"),
        ("(-)1,234.00", "-1234.00"),
        ("(-) 1,180.00", "-1180.00"),
        ("₹ (-)1,180.00", "-1180.00"),
        ("(+)0.40", "0.40"),
        # A dash or "Nil" in an amount column means nothing is charged.
        ("-", "0"),
        ("–", "0"),
        ("Nil", "0"),
        ("NIL", "0"),
        ("nil.", "0"),
        ("₹ -", "0"),
        # Brackets around an amount with the Indian "/-" suffix.
        ("(Rs. 500/-)", "-500"),
        ("(₹ 500/-)", "-500"),
        ("(Rs. 80,000.30/-)", "-80000.30"),
        ("(500/=)", "-500"),
        ("(-)Rs. 500/-", "-500"),
    ],
)
def test_parse_amount(raw, expected):
    value = parse_amount(raw)
    assert isinstance(value, Decimal)
    assert value == Decimal(expected)
    assert str(value) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "₹",
        "Rs. /-",
        "1.234.567",  # dots as thousands separators: ambiguous
        "1.234,56",  # European format
        "123,45",  # decimal comma or a broken group: ambiguous
        "1,2345",
        "12,345,67",
        ",123",
        ".",
        "abc",
        "12a",
        "Five hundred only",
        "-(100)",
        "-100-",
        "1 2 3",
        "18%",
        # A leading zero group is a decimal comma or a misread, never thousands grouping.
        "0,500",
        "0,750",
        "0,000",
        "012,345",
        "00,000",
        "01,23,456",
        "(-)",
        "(-)(100)",
        "(-)100-",
        "--",
        "Nil 500",
    ],
)
def test_parse_amount_rejects_unclear_values(raw):
    assert parse_amount(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-28", date(2026, 9, 28)),
        ("2026/09/28", date(2026, 9, 28)),
        ("2026-09-28T10:15:00", date(2026, 9, 28)),
        ("28/09/2026", date(2026, 9, 28)),
        ("28-09-2026", date(2026, 9, 28)),
        ("28.09.2026", date(2026, 9, 28)),
        ("28/9/26", date(2026, 9, 28)),
        ("28-09-26", date(2026, 9, 28)),
        ("05/09/2026", date(2026, 9, 5)),  # day first: 5 September, never May 9
        ("5/9/2026", date(2026, 9, 5)),
        ("12/01/2026", date(2026, 1, 12)),
        ("28/09/2026 10:30 AM", date(2026, 9, 28)),
        ("28-Sep-2026", date(2026, 9, 28)),
        ("28-SEP-26", date(2026, 9, 28)),
        ("28 Sep 2026", date(2026, 9, 28)),
        ("28 Sept 2026", date(2026, 9, 28)),
        ("28 Sep, 2026", date(2026, 9, 28)),
        ("28 September 2026", date(2026, 9, 28)),
        ("September 28, 2026", date(2026, 9, 28)),
        ("Sep 28 2026", date(2026, 9, 28)),
        ("28th September 2026", date(2026, 9, 28)),
        ("1st Jan 2026", date(2026, 1, 1)),
        ("2nd February, 2026", date(2026, 2, 2)),
        ("23rd Mar 2026", date(2026, 3, 23)),
        ("29/02/2028", date(2028, 2, 29)),
        ("  28/09/2026  ", date(2026, 9, 28)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "  ",
        "31/02/2026",
        "29/02/2026",  # not a leap year
        "2026-02-30",
        "13/13/2026",
        "00/09/2026",
        "28/09-2026",  # mixed separators
        "28/09/202",
        "28-Foo-2026",
        "Smarch 28, 2026",
        "28092026",
        "yesterday",
    ],
)
def test_parse_date_rejects_invalid(raw):
    assert parse_date(raw) is None


# --- normalize.py -----------------------------------------------------------------------


def v(value: str | None, confidence: str = "high") -> ExtractedValue:
    return ExtractedValue(value=value, confidence=confidence)


def make_extraction(**overrides) -> InvoiceExtraction:
    """An interstate purchase: a Karnataka supplier billing a Maharashtra firm."""
    extraction = InvoiceExtraction(
        is_invoice=True,
        document_type="tax_invoice",
        invoice_number=v(" KS/2026-27/0142 "),
        invoice_date=v("28/09/2026"),
        seller=ExtractedParty(
            name=v("Kaveri Steel Traders"),
            gstin=v(f"{SELLER_GSTIN[:2]} {SELLER_GSTIN[2:].lower()}"),
            address="12 Industrial Area, Peenya, Bengaluru 560058",
            state="Karnataka",
        ),
        buyer=ExtractedParty(
            name=v("Unique Fabricators", "medium"),
            gstin=v("27-AAPFU-0939-F1ZV"),
            address="Plot 7, MIDC Bhosari, Pune",
            state="27-Maharashtra",
        ),
        place_of_supply="27-Maharashtra",
        reverse_charge=None,
        line_items=[
            ExtractedLineItem(
                description="MS Plate 6mm",
                hsn_sac="7208",
                quantity="1,250.500",
                unit="KGS",
                rate="₹ 62.40",
                taxable_value="78,031.20",
                gst_rate="18%",
            ),
            ExtractedLineItem(
                description="Cutting charges",
                hsn_sac="9988",
                quantity=None,
                rate=None,
                taxable_value="1,969.10",
                gst_rate="18",
            ),
            ExtractedLineItem(description="  ", quantity=None, taxable_value=""),
        ],
        totals=ExtractedTotals(
            taxable_value=v("80,000.30"),
            cgst=v(None),
            sgst=v(None),
            igst=v("14,400.05"),
            cess=v(None, "medium"),
            round_off=v("-0.35", "medium"),
            grand_total=v("₹94,400.00"),
        ),
        notes=["Stamp partly covers the buyer address."],
    )
    return extraction.model_copy(update=overrides)


def with_totals(**changes) -> InvoiceExtraction:
    base = make_extraction()
    return base.model_copy(update={"totals": base.totals.model_copy(update=changes)})


CONFIDENCE_KEYS = {
    "invoice_number",
    "invoice_date",
    "seller.name",
    "seller.gstin",
    "buyer.name",
    "buyer.gstin",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "cess",
    "round_off",
    "grand_total",
}


def test_normalize_realistic_invoice():
    inv = normalize(make_extraction())

    assert inv.is_invoice and inv.document_type == "tax_invoice"
    assert inv.invoice_number == "KS/2026-27/0142"
    assert inv.invoice_date == date(2026, 9, 28)

    assert inv.seller.name == "Kaveri Steel Traders"
    assert inv.seller.gstin == SELLER_GSTIN
    assert inv.seller.gstin_valid
    assert (inv.seller.state_code, inv.seller.state) == ("29", "Karnataka")
    assert inv.seller.address == "12 Industrial Area, Peenya, Bengaluru 560058"

    assert inv.buyer.gstin == "27AAPFU0939F1ZV"
    assert inv.buyer.gstin_valid
    assert (inv.buyer.state_code, inv.buyer.state) == ("27", "Maharashtra")
    assert inv.place_of_supply_code == "27"
    assert inv.reverse_charge is False

    assert len(inv.lines) == 2  # the blank third line is dropped
    plate, cutting = inv.lines
    assert plate.description == "MS Plate 6mm"
    assert plate.hsn_sac == "7208"
    assert plate.quantity == Decimal("1250.500")
    assert plate.unit == "KGS"
    assert plate.rate == Decimal("62.40")
    assert plate.taxable_value == Decimal("78031.20")
    assert plate.gst_rate == Decimal("18")
    assert cutting.quantity is None and cutting.rate is None
    assert cutting.taxable_value == Decimal("1969.10")

    assert inv.taxable_value == Decimal("80000.30")
    assert (inv.cgst, inv.sgst, inv.igst, inv.cess) == (0, 0, Decimal("14400.05"), 0)
    assert inv.round_off == Decimal("-0.35")
    assert inv.grand_total == Decimal("94400.00")
    assert inv.taxable_value + inv.total_tax + inv.round_off == inv.grand_total
    for amount in (inv.cgst, inv.sgst, inv.igst, inv.cess, inv.round_off, inv.grand_total):
        assert isinstance(amount, Decimal)

    assert CONFIDENCE_KEYS <= set(inv.confidence)
    assert inv.confidence["invoice_number"] == CONFIDENCE["high"]
    assert inv.confidence["buyer.name"] == CONFIDENCE["medium"]
    assert inv.confidence["cgst"] == CONFIDENCE["high"]  # null: the model's own confidence
    assert inv.confidence["cess"] == CONFIDENCE["medium"]
    assert inv.confidence["round_off"] == CONFIDENCE["medium"]
    assert inv.notes == ["Stamp partly covers the buyer address."]


def test_confidence_mapping():
    assert CONFIDENCE == {"high": 0.95, "medium": 0.7, "low": 0.4}


def test_confidence_keys_set_even_when_everything_is_null():
    blank = v(None, "low")
    extraction = InvoiceExtraction(
        is_invoice=False,
        document_type="other",
        invoice_number=blank,
        invoice_date=blank,
        seller=ExtractedParty(name=blank, gstin=blank),
        buyer=ExtractedParty(name=blank, gstin=blank),
        line_items=[],
        totals=ExtractedTotals(
            taxable_value=blank,
            cgst=blank,
            sgst=blank,
            igst=blank,
            cess=blank,
            round_off=blank,
            grand_total=blank,
        ),
    )
    inv = normalize(extraction)
    assert set(inv.confidence) == CONFIDENCE_KEYS
    assert all(score == CONFIDENCE["low"] for score in inv.confidence.values())
    assert not inv.is_invoice and inv.document_type == "other"
    assert inv.taxable_value is None and inv.grand_total is None
    assert inv.total_tax == 0 and inv.round_off == 0
    assert inv.seller.gstin is None and not inv.seller.gstin_valid
    assert inv.seller.state_code is None and inv.seller.state is None
    assert inv.place_of_supply_code is None
    assert inv.notes == []


def test_unreadable_values_get_zero_confidence_and_a_note():
    extraction = with_totals(igst=v("14.400.00"), grand_total=v("ninety four thousand"))
    extraction = extraction.model_copy(update={"invoice_date": v("31/02/2026")})
    inv = normalize(extraction)

    assert inv.invoice_date is None
    assert inv.igst == Decimal("0")
    assert inv.grand_total is None
    for key in ("invoice_date", "igst", "grand_total"):
        assert inv.confidence[key] == 0.0
    joined = " ".join(inv.notes)
    assert '"31/02/2026"' in joined and "invoice date" in joined
    assert '"14.400.00"' in joined and "IGST" in joined
    assert '"ninety four thousand"' in joined and "grand total" in joined
    assert inv.notes[0] == "Stamp partly covers the buyer address."
    assert all(note.endswith(".") for note in inv.notes)


def test_unreadable_line_value_is_noted():
    item = ExtractedLineItem(description="Bolts", quantity="ten", taxable_value="500")
    inv = normalize(make_extraction(line_items=[item]))
    assert inv.lines[0].quantity is None
    assert inv.lines[0].taxable_value == Decimal("500.00")
    assert inv.confidence["lines.0.quantity"] == 0.0
    assert any('"ten"' in note and "line 1" in note for note in inv.notes)


def test_taxable_value_falls_back_to_sum_of_lines():
    inv = normalize(with_totals(taxable_value=v(None, "low")))
    assert inv.taxable_value == Decimal("80000.30")
    assert inv.confidence["taxable_value"] == 0.7
    assert any("sum of the line items" in note for note in inv.notes)


def test_taxable_value_not_derived_when_a_priced_line_has_no_value():
    lines = [
        ExtractedLineItem(description="A", taxable_value="100"),
        ExtractedLineItem(description="B", quantity="2", rate="50"),
    ]
    extraction = with_totals(taxable_value=v(None, "low")).model_copy(update={"line_items": lines})
    inv = normalize(extraction)
    assert inv.taxable_value is None
    assert inv.confidence["taxable_value"] == CONFIDENCE["low"]


@pytest.mark.parametrize(
    "lines",
    [
        [  # a printed line amount that could not be read ("O" for "0")
            ExtractedLineItem(description="Steel", taxable_value="78,031.20"),
            ExtractedLineItem(description="Cutting", taxable_value="1,96O.10"),
        ],
        [
            ExtractedLineItem(description="MS Plate", taxable_value="1,000.00"),
            ExtractedLineItem(description="Freight", taxable_value="5OO.00"),
        ],
        [  # a printed rate that could not be read, and no line amount
            ExtractedLineItem(description="Steel", taxable_value="78,031.20"),
            ExtractedLineItem(description="Bolts", rate="1O.50"),
        ],
    ],
)
def test_taxable_value_not_derived_when_a_line_amount_is_unreadable(lines):
    extraction = with_totals(taxable_value=v(None, "low")).model_copy(update={"line_items": lines})
    inv = normalize(extraction)
    assert inv.taxable_value is None
    assert inv.confidence["taxable_value"] == CONFIDENCE["low"]
    assert not any("sum of the line items" in note for note in inv.notes)
    assert any("line 2" in note for note in inv.notes)


def test_taxable_value_not_derived_from_lines_without_values():
    lines = [ExtractedLineItem(description="Consulting")]
    inv = normalize(with_totals(taxable_value=v(None)).model_copy(update={"line_items": lines}))
    assert inv.taxable_value is None
    assert len(inv.lines) == 1


def test_printed_taxable_value_wins_over_lines():
    inv = normalize(with_totals(taxable_value=v("79,999.00")))
    assert inv.taxable_value == Decimal("79999.00")
    assert inv.confidence["taxable_value"] == CONFIDENCE["high"]


def test_money_is_rounded_half_up_to_paise():
    inv = normalize(with_totals(igst=v("14400.005"), round_off=v("-0.125")))
    assert inv.igst == Decimal("14400.01")
    assert inv.round_off == Decimal("-0.13")


@pytest.mark.parametrize("digits", [16, 27, 40])
def test_implausibly_large_amounts_are_unreadable(digits):
    huge = "1" * digits
    lines = [ExtractedLineItem(description="x", quantity=huge, rate=huge, taxable_value=huge)]
    extraction = with_totals(grand_total=v(huge), cess=v(huge)).model_copy(
        update={"line_items": lines}
    )
    inv = normalize(extraction)
    assert inv.grand_total is None and inv.cess == 0
    line = inv.lines[0]
    assert line.quantity is None and line.rate is None and line.taxable_value is None
    for key in ("grand_total", "cess", "lines.0.quantity", "lines.0.rate", "lines.0.taxable_value"):
        assert inv.confidence[key] == 0.0, key
    assert any(huge in note and "grand total" in note for note in inv.notes)


def test_largest_plausible_amount_is_kept():
    inv = normalize(with_totals(grand_total=v("99,99,99,99,99,99,999.99")))
    assert inv.grand_total == Decimal("999999999999999.99")


def test_tally_printed_credit_note():
    extraction = with_totals(
        taxable_value=v("(-)1,000.00"),
        cgst=v("-"),
        sgst=v("Nil"),
        igst=v("(-)180.00"),
        round_off=v("(-)0.40"),
        grand_total=v("(-)1,180.40"),
    ).model_copy(update={"document_type": "credit_note", "line_items": []})
    inv = normalize(extraction)
    assert inv.taxable_value == Decimal("-1000.00")
    assert (inv.cgst, inv.sgst, inv.igst) == (0, 0, Decimal("-180.00"))
    assert inv.round_off == Decimal("-0.40")
    assert inv.grand_total == Decimal("-1180.40")
    for key in ("taxable_value", "cgst", "sgst", "igst", "round_off", "grand_total"):
        assert inv.confidence[key] == CONFIDENCE["high"], key
    assert inv.notes == ["Stamp partly covers the buyer address."]


def test_leading_zero_grouped_line_numbers_are_unreadable():
    item = ExtractedLineItem(
        description="Wire", quantity="0,500", rate="0,750", taxable_value="375"
    )
    inv = normalize(make_extraction(line_items=[item]))
    assert inv.lines[0].quantity is None and inv.lines[0].rate is None
    assert inv.confidence["lines.0.quantity"] == inv.confidence["lines.0.rate"] == 0.0


def test_lines_kept_when_they_have_a_description_or_an_amount():
    lines = [
        ExtractedLineItem(description="", taxable_value="250"),
        ExtractedLineItem(description="Batch no. B-17"),
        ExtractedLineItem(description="", quantity=None, rate=" ", taxable_value=None),
    ]
    inv = normalize(make_extraction(line_items=lines))
    assert [line.description for line in inv.lines] == ["", "Batch no. B-17"]
    assert inv.lines[0].taxable_value == Decimal("250.00")


def test_invalid_gstin_takes_state_from_printed_name():
    wrong_check = SELLER_GSTIN[:14] + ("0" if SELLER_GSTIN[14] != "0" else "1")
    seller = make_extraction().seller.model_copy(
        update={"gstin": v(wrong_check), "state": "Karnataka"}
    )
    inv = normalize(make_extraction(seller=seller))
    assert inv.seller.gstin == wrong_check
    assert not inv.seller.gstin_valid
    assert inv.seller.state_code == "29"


def test_valid_gstin_state_wins_over_printed_name():
    buyer = make_extraction().buyer.model_copy(update={"state": "Gujarat"})
    inv = normalize(make_extraction(buyer=buyer))
    assert (inv.buyer.state_code, inv.buyer.state) == ("27", "Maharashtra")


def test_gstin_with_unicode_dashes_is_valid():
    gstin = f"{SELLER_GSTIN[:2]}–{SELLER_GSTIN[2:7]}—{SELLER_GSTIN[7:11]}−{SELLER_GSTIN[11:]}​"
    seller = make_extraction().seller.model_copy(update={"gstin": v(gstin), "state": None})
    inv = normalize(make_extraction(seller=seller))
    assert inv.seller.gstin == SELLER_GSTIN
    assert inv.seller.gstin_valid
    assert inv.seller.state_code == "29"


@pytest.mark.parametrize("gstin", ["27AAPFU०९३९F1ZV", "27AAPFU０９３９F1ZV"])
def test_gstin_with_non_ascii_digits_is_flagged_not_fatal(gstin):
    seller = ExtractedParty(name=v("A"), gstin=v(gstin), state="Maharashtra")
    inv = normalize(make_extraction(seller=seller))
    assert not inv.seller.gstin_valid
    assert inv.seller.state_code == "27"


def test_legacy_andhra_pradesh_code_resolves_to_current_code():
    seller = ExtractedParty(
        name=v("Vizag Steels"), gstin=v(with_check("37AABCV1234K1Z")), state="Andhra Pradesh"
    )
    buyer = ExtractedParty(name=v("Walk-in Customer"), gstin=v(None), state="28-Andhra Pradesh")
    extraction = with_totals(cgst=v("90"), sgst=v("90"), igst=v(None)).model_copy(
        update={"seller": seller, "buyer": buyer, "place_of_supply": "28-Andhra Pradesh"}
    )
    inv = normalize(extraction)
    assert inv.seller.state_code == inv.place_of_supply_code == "37"
    assert (inv.buyer.state_code, inv.buyer.state) == ("37", "Andhra Pradesh")


def test_legacy_daman_and_diu_code_resolves_to_merged_ut():
    inv = normalize(make_extraction(place_of_supply="Daman & Diu (25)"))
    assert inv.place_of_supply_code == "26"


@pytest.mark.parametrize(
    "printed",
    [
        "URP",
        "N/A",
        "NA",
        "Unregistered",
        " - ",
        "NIL",
        "URD",
        "U.R.D.",
        "Unregistered Dealer",
        "Unregistered Person",
        "Not Registered",
        "Consumer",
    ],
)
def test_placeholder_gstin_is_treated_as_absent(printed):
    buyer = ExtractedParty(name=v("Walk-in Customer"), gstin=v(printed), state="NCT of Delhi")
    inv = normalize(make_extraction(buyer=buyer))
    assert inv.buyer.gstin is None
    assert not inv.buyer.gstin_valid
    assert (inv.buyer.state_code, inv.buyer.state) == ("07", "Delhi")


def test_unknown_place_of_supply_is_noted():
    inv = normalize(make_extraction(place_of_supply="Narnia"))
    assert inv.place_of_supply_code is None
    assert any('"Narnia"' in note for note in inv.notes)


def test_reverse_charge_is_copied():
    assert normalize(make_extraction(reverse_charge=True)).reverse_charge is True
    assert normalize(make_extraction(reverse_charge=False)).reverse_charge is False


def test_intrastate_invoice():
    extraction = with_totals(
        cgst=v("7,200.03"), sgst=v("7,200.03"), igst=v(None), round_off=v(None)
    ).model_copy(update={"place_of_supply": "Karnataka"})
    inv = normalize(extraction)
    assert inv.place_of_supply_code == inv.seller.state_code == "29"
    assert (inv.cgst, inv.sgst, inv.igst) == (Decimal("7200.03"), Decimal("7200.03"), 0)
    assert inv.round_off == 0
    assert inv.total_tax == Decimal("14400.06")
