import xml.etree.ElementTree as ET
from datetime import date

import pytest

from app.connectors.base import ConnectorError
from app.connectors.tally import xml_builder, xml_parser
from app.schemas.canonical import ProposedLedger
from tests.factories import purchase_tx


def _parse(payload: bytes) -> ET.Element:
    return ET.fromstring(payload)


class TestVoucherXml:
    def test_header_and_company(self):
        root = _parse(xml_builder.voucher_request("Demo Co", purchase_tx()))
        assert root.findtext("HEADER/TALLYREQUEST") == "Import Data"
        assert root.findtext(".//REPORTNAME") == "Vouchers"
        assert root.findtext(".//SVCURRENTCOMPANY") == "Demo Co"

    def test_voucher_fields(self):
        tx = purchase_tx()
        v = _parse(xml_builder.voucher_request("Demo Co", tx)).find(".//VOUCHER")
        assert v is not None
        assert v.get("VCHTYPE") == "Purchase"
        assert v.get("ACTION") == "Create"
        assert v.get("REMOTEID") == str(tx.id)
        assert v.findtext("DATE") == "20260930"
        assert v.findtext("REFERENCE") == "SE/2026/0042"
        assert v.findtext("REFERENCEDATE") == "20260928"
        assert v.findtext("PARTYLEDGERNAME") == "Sharma Electronics"

    def test_sign_convention_and_party_first(self):
        v = _parse(xml_builder.voucher_request("Demo Co", purchase_tx())).find(".//VOUCHER")
        lines = v.findall("ALLLEDGERENTRIES.LIST")
        got = [
            (
                line.findtext("LEDGERNAME"),
                line.findtext("ISDEEMEDPOSITIVE"),
                line.findtext("AMOUNT"),
            )
            for line in lines
        ]
        assert got == [
            ("Sharma Electronics", "No", "11800.00"),  # credit: positive
            ("Purchase", "Yes", "-10000.00"),  # debit: negative
            ("Input CGST", "Yes", "-900.00"),
            ("Input SGST", "Yes", "-900.00"),
        ]
        assert sum(float(a) for _, _, a in got) == 0

    def test_bill_allocation_on_party_line(self):
        v = _parse(xml_builder.voucher_request("Demo Co", purchase_tx())).find(".//VOUCHER")
        party = v.findall("ALLLEDGERENTRIES.LIST")[0]
        bill = party.find("BILLALLOCATIONS.LIST")
        assert bill is not None
        assert bill.findtext("NAME") == "SE/2026/0042"
        assert bill.findtext("BILLTYPE") == "New Ref"
        assert bill.findtext("AMOUNT") == "11800.00"
        assert v.findall("ALLLEDGERENTRIES.LIST")[1].find("BILLALLOCATIONS.LIST") is None

    def test_special_characters_are_escaped(self):
        tx = purchase_tx()
        tx.narration = 'Parts <A&B> "urgent"'
        v = _parse(xml_builder.voucher_request("R & D Labs", tx))
        assert v.findtext(".//NARRATION") == 'Parts <A&B> "urgent"'
        assert v.findtext(".//SVCURRENTCOMPANY") == "R & D Labs"


def test_ledger_create_xml():
    led = ProposedLedger(
        name="Mehta Steel",
        parent_group="Sundry Creditors",
        gstin="24AAACM1234B1ZC",
        gst_registration_type="Regular",
        state="Gujarat",
        address_lines=["Plot 4, GIDC", "Vapi"],
        bill_wise=True,
    )
    root = _parse(xml_builder.create_ledgers_request("Demo Co", [led], date(2024, 4, 1)))
    assert root.findtext(".//REPORTNAME") == "All Masters"
    el = root.find(".//LEDGER")
    assert el.get("NAME") == "Mehta Steel" and el.get("ACTION") == "Create"
    assert el.findtext("PARENT") == "Sundry Creditors"
    assert el.findtext("PARTYGSTIN") == "24AAACM1234B1ZC"
    assert el.findtext("ISBILLWISEON") == "Yes"
    assert [a.text for a in el.findall("ADDRESS.LIST/ADDRESS")] == ["Plot 4, GIDC", "Vapi"]
    assert el.findtext("LEDGSTREGDETAILS.LIST/GSTIN") == "24AAACM1234B1ZC"


def test_export_request_contains_collection():
    root = _parse(xml_builder.ledgers_request("Demo Co"))
    assert root.findtext("HEADER/TYPE") == "Collection"
    assert root.findtext(".//COLLECTION/TYPE") == "Ledger"
    assert "PARENT" in root.findtext(".//COLLECTION/FETCH")


class TestParser:
    def test_ledgers_with_control_chars_and_aliases(self):
        raw = (
            b"<ENVELOPE><BODY><DATA><COLLECTION>"
            b'<LEDGER NAME="Sharma Electronics"><GUID>g-1</GUID><PARENT>&#4; Sundry Creditors</PARENT>'
            b"<PARTYGSTIN>29abcde1234f1zw</PARTYGSTIN><LEDSTATENAME>Karnataka</LEDSTATENAME>"
            b'<LANGUAGENAME.LIST><NAME.LIST TYPE="String"><NAME>Sharma Electronics</NAME>'
            b"<NAME>Sharma Elec</NAME></NAME.LIST></LANGUAGENAME.LIST></LEDGER>"
            b'<LEDGER NAME="Cash"><PARENT>Cash-in-Hand</PARENT></LEDGER>'
            b"</COLLECTION></DATA></BODY></ENVELOPE>"
        )
        ledgers = xml_parser.parse_ledgers(raw)
        assert [lg.name for lg in ledgers] == ["Sharma Electronics", "Cash"]
        sharma = ledgers[0]
        assert sharma.parent == "Sundry Creditors"
        assert sharma.gstin == "29ABCDE1234F1ZW"
        assert sharma.aliases == ["Sharma Elec"]
        assert sharma.external_id == "g-1"

    def test_newer_gst_details_block(self):
        raw = (
            b'<ENVELOPE><LEDGER NAME="V"><PARENT>Sundry Creditors</PARENT>'
            b"<LEDGSTREGDETAILS.LIST><GSTIN>27AAAAA0000A1Z5</GSTIN></LEDGSTREGDETAILS.LIST>"
            b"</LEDGER></ENVELOPE>"
        )
        assert xml_parser.parse_ledgers(raw)[0].gstin == "27AAAAA0000A1Z5"

    def test_utf16_response(self):
        raw = '<ENVELOPE><GROUP NAME="Duties &amp; Taxes"><PARENT>Current Liabilities</PARENT></GROUP></ENVELOPE>'
        groups = xml_parser.parse_groups(raw.encode("utf-16"))
        assert groups[0].name == "Duties & Taxes"

    def test_export_error_raises(self):
        raw = (
            b"<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER><BODY><DATA>"
            b"<LINEERROR>Could not set 'SVCurrentCompany' to 'Nope'</LINEERROR></DATA></BODY></ENVELOPE>"
        )
        with pytest.raises(ConnectorError, match="SVCurrentCompany"):
            xml_parser.parse_ledgers(raw)

    def test_import_result_success(self):
        raw = b"<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><LASTVCHID>77</LASTVCHID><ERRORS>0</ERRORS></RESPONSE>"
        result = xml_parser.parse_import_result(raw)
        assert result.success and result.created == 1 and result.last_voucher_id == "77"

    def test_import_result_line_error(self):
        raw = (
            b"<RESPONSE><LINEERROR>Ledger 'Foo' does not exist</LINEERROR>"
            b"<CREATED>0</CREATED><ERRORS>1</ERRORS></RESPONSE>"
        )
        result = xml_parser.parse_import_result(raw)
        assert not result.success
        assert result.messages == ["Ledger 'Foo' does not exist"]

    def test_garbage_raises(self):
        with pytest.raises(ConnectorError):
            xml_parser.parse(b"not xml at all <")
