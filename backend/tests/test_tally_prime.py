"""TallyPrime 3.0+ behaviour: dated GST and address lists, UTF-16 requests, how Tally
reports rejections, Educational mode, and the checks made before anything is posted."""

import xml.etree.ElementTree as ET
from datetime import date

import httpx
from fastapi.testclient import TestClient

from app.accounting.engine import build_voucher
from app.connectors.tally import TallyConnector, xml_builder, xml_parser
from app.connectors.tally.client import wire_body
from app.connectors.tally.connector import explain
from app.devtools.mock_tally import DEMO_COMPANY, MockTally, httpx_transport
from app.schemas.canonical import GstDetails, ProposedLedger
from app.schemas.invoice import Party
from tests.factories import purchase_tx
from tests.test_accounting import COMPANY_GSTIN, GUPTA_GSTIN, SHARMA_GSTIN, ctx, purchase, sale

MEHTA = ProposedLedger(
    name="Mehta Steel",
    parent_group="Sundry Creditors",
    gstin="24AAACM1234B1ZC",
    gst_registration_type="Regular",
    state="Gujarat",
    address_lines=["Plot 4, GIDC", "Vapi"],
    pincode="396195",
    bill_wise=True,
)
SINCE = date(2024, 4, 1)


def _ledger_xml(*ledgers: ProposedLedger) -> ET.Element:
    root = ET.fromstring(xml_builder.create_ledgers_request("Demo Co", list(ledgers), SINCE))
    found = root.find(".//LEDGER")
    assert found is not None
    return found


def _voucher_xml(tx) -> ET.Element:
    found = ET.fromstring(xml_builder.voucher_request("Demo Co", tx)).find(".//VOUCHER")
    assert found is not None
    return found


def _ledgers_raw(*ledger_xml: str) -> bytes:
    body = "".join(ledger_xml)
    return (
        f"<ENVELOPE><BODY><DATA><COLLECTION>{body}</COLLECTION></DATA></BODY></ENVELOPE>".encode()
    )


# -- ledger requests ---------------------------------------------------------------------


class TestLedgerXml:
    def test_gst_details_go_in_the_dated_list(self):
        reg = _ledger_xml(MEHTA).find("LEDGSTREGDETAILS.LIST")
        assert reg is not None
        assert reg.findtext("APPLICABLEFROM") == "20240401"
        assert reg.findtext("GSTREGISTRATIONTYPE") == "Regular"
        assert reg.findtext("STATE") == "Gujarat"
        assert reg.findtext("GSTIN") == "24AAACM1234B1ZC"

    def test_address_goes_in_the_dated_mailing_list(self):
        mailing = _ledger_xml(MEHTA).find("LEDMAILINGDETAILS.LIST")
        assert mailing is not None
        assert mailing.findtext("APPLICABLEFROM") == "20240401"
        assert mailing.findtext("MAILINGNAME") == "Mehta Steel"
        assert [a.text for a in mailing.findall("ADDRESS.LIST/ADDRESS")] == [
            "Plot 4, GIDC",
            "Vapi",
        ]
        assert mailing.findtext("STATE") == "Gujarat"
        assert mailing.findtext("COUNTRY") == "India"
        assert mailing.findtext("PINCODE") == "396195"

    def test_old_flat_fields_are_still_sent_for_older_releases(self):
        el = _ledger_xml(MEHTA)
        assert el.findtext("PARTYGSTIN") == "24AAACM1234B1ZC"
        assert el.findtext("LEDSTATENAME") == "Gujarat"
        assert el.findtext("GSTREGISTRATIONTYPE") == "Regular"
        assert [a.text for a in el.findall("ADDRESS.LIST/ADDRESS")] == ["Plot 4, GIDC", "Vapi"]

    def test_unregistered_party_uses_the_merged_type_in_the_list(self):
        led = MEHTA.model_copy(update={"gstin": None, "gst_registration_type": "Unregistered"})
        el = _ledger_xml(led)
        reg = el.find("LEDGSTREGDETAILS.LIST")
        assert reg is not None
        assert reg.findtext("GSTREGISTRATIONTYPE") == "Unregistered/Consumer"
        assert reg.find("GSTIN") is None
        assert el.findtext("GSTREGISTRATIONTYPE") == "Unregistered"  # old releases' spelling
        assert el.find("PARTYGSTIN") is None

    def test_a_ledger_without_party_details_gets_no_lists(self):
        el = _ledger_xml(ProposedLedger(name="Freight", parent_group="Direct Expenses"))
        assert el.find("LEDGSTREGDETAILS.LIST") is None
        assert el.find("LEDMAILINGDETAILS.LIST") is None

    def test_fetches_ask_for_the_dated_lists_and_books_date(self):
        def fetch(payload: bytes) -> list[str]:
            text = ET.fromstring(payload).findtext(".//COLLECTION/FETCH") or ""
            return [f.strip() for f in text.split(",")]

        ledger_fields = fetch(xml_builder.ledgers_request("Demo Co"))
        assert {"LEDGSTREGDETAILS.LIST", "LEDMAILINGDETAILS.LIST"} <= set(ledger_fields)
        assert {"PARTYGSTIN", "LEDSTATENAME"} <= set(ledger_fields)  # fallbacks
        assert "BOOKSFROM" in fetch(xml_builder.companies_request())


# -- voucher requests --------------------------------------------------------------------


class TestVoucherGst:
    def test_registered_party(self):
        tx = purchase_tx()
        tx.gst = GstDetails(
            party_gstin="29ABCDE1234F1ZW",
            party_registration_type="Regular",
            party_state="Karnataka",
            place_of_supply="Karnataka",
            company_gstin="29AAACD1234A1ZD",
        )
        v = _voucher_xml(tx)
        assert v.findtext("CMPGSTIN") == "29AAACD1234A1ZD"
        assert v.findtext("PARTYGSTIN") == "29ABCDE1234F1ZW"
        assert v.findtext("GSTREGISTRATIONTYPE") == "Regular"
        assert v.findtext("STATENAME") == "Karnataka"
        assert v.findtext("COUNTRYOFRESIDENCE") == "India"
        assert v.findtext("PLACEOFSUPPLY") == "Karnataka"

    def test_unregistered_party_leaves_the_type_to_the_ledger(self):
        tx = purchase_tx()
        tx.gst = GstDetails(party_registration_type="Unregistered", party_state="Gujarat")
        v = _voucher_xml(tx)
        assert v.find("PARTYGSTIN") is None and v.find("GSTREGISTRATIONTYPE") is None
        assert v.findtext("STATENAME") == "Gujarat"

    def test_no_gst_details_no_tags(self):
        v = _voucher_xml(purchase_tx())
        for tag in ("CMPGSTIN", "PARTYGSTIN", "STATENAME", "PLACEOFSUPPLY"):
            assert v.find(tag) is None


# -- reading ledgers and companies back --------------------------------------------------


def _reg(since: str | None, gstin: str | None, state: str = "Karnataka") -> str:
    parts = [f'<APPLICABLEFROM TYPE="Date">{since}</APPLICABLEFROM>' if since else ""]
    parts.append("<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>")
    parts.append(f"<STATE>{state}</STATE>")
    parts.append(f"<GSTIN>{gstin}</GSTIN>" if gstin else "")
    return f"<LEDGSTREGDETAILS.LIST>{''.join(parts)}</LEDGSTREGDETAILS.LIST>"


class TestReadingLedgers:
    RAW = _ledgers_raw(
        '<LEDGER NAME="V"><PARENT>Sundry Creditors</PARENT><PARTYGSTIN></PARTYGSTIN>',
        _reg("20170701", None),
        _reg("20240701", "29ABCDE1234F1ZW"),
        _reg("20990401", "29ABCDE1234F2ZV"),
        "</LEDGER>",
    )

    def test_uses_the_registration_in_force(self):
        assert xml_parser.parse_ledgers(self.RAW, on=date(2025, 1, 1))[0].gstin == (
            "29ABCDE1234F1ZW"
        )

    def test_an_earlier_date_finds_the_earlier_entry(self):
        # In force in 2020: the first entry, which has no GSTIN and no flat one to fall back on.
        assert xml_parser.parse_ledgers(self.RAW, on=date(2020, 1, 1))[0].gstin is None

    def test_empty_placeholder_falls_back_to_the_flat_fields(self):
        raw = _ledgers_raw(
            '<LEDGER NAME="Old Party"><PARTYGSTIN>27pqrsx6789k1zs</PARTYGSTIN>',
            "<LEDSTATENAME>Maharashtra</LEDSTATENAME>",
            "<LEDGSTREGDETAILS.LIST>     </LEDGSTREGDETAILS.LIST>",
            "<LEDMAILINGDETAILS.LIST>     </LEDMAILINGDETAILS.LIST></LEDGER>",
        )
        [ledger] = xml_parser.parse_ledgers(raw)
        assert ledger.gstin == "27PQRSX6789K1ZS" and ledger.state == "Maharashtra"

    def test_only_future_entries_use_the_earliest(self):
        raw = _ledgers_raw('<LEDGER NAME="V">', _reg("20990401", "29ABCDE1234F1ZW"), "</LEDGER>")
        assert xml_parser.parse_ledgers(raw, on=date(2025, 1, 1))[0].gstin == "29ABCDE1234F1ZW"

    def test_state_comes_from_the_mailing_list_first(self):
        raw = _ledgers_raw(
            '<LEDGER NAME="V"><LEDSTATENAME>Kerala</LEDSTATENAME>',
            "<LEDMAILINGDETAILS.LIST><APPLICABLEFROM>20240401</APPLICABLEFROM>",
            "<STATE>Goa</STATE></LEDMAILINGDETAILS.LIST></LEDGER>",
        )
        assert xml_parser.parse_ledgers(raw)[0].state == "Goa"

    def test_company_books_date(self):
        raw = (
            b'<ENVELOPE><COMPANY NAME="Demo"><BOOKSFROM TYPE="Date">20230401</BOOKSFROM>'
            b"</COMPANY></ENVELOPE>"
        )
        assert xml_parser.parse_companies(raw)[0].books_from == date(2023, 4, 1)

    def test_tally_date_formats(self):
        assert xml_parser.parse_date("20250401") == date(2025, 4, 1)
        assert xml_parser.parse_date("1-Apr-2025") == date(2025, 4, 1)
        assert xml_parser.parse_date("") is None and xml_parser.parse_date("soon") is None


# -- import responses --------------------------------------------------------------------


class TestImportResponses:
    def test_unknown_request(self):
        result = xml_parser.parse_import_result(
            b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"
        )
        assert result.unknown_request and not result.success
        [message] = explain(result)
        assert "did not understand the request" in message
        assert "Unknown Request, cannot be processed" in message

    def test_exceptions_without_a_line_error(self):
        result = xml_parser.parse_import_result(
            b"<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS>"
            b"<EXCEPTIONS>1</EXCEPTIONS></RESPONSE>"
        )
        assert not result.success and not result.unknown_request
        [message] = explain(result)
        assert message.startswith("Tally rejected it without giving a reason.")
        assert "Educational mode" in message

    def test_misleading_date_error_gets_the_educational_mode_hint(self):
        result = xml_parser.parse_import_result(
            b"<RESPONSE><LINEERROR>Voucher date is missing for: 'Sales' voucher</LINEERROR>"
            b"<CREATED>0</CREATED><ERRORS>0</ERRORS><EXCEPTIONS>1</EXCEPTIONS></RESPONSE>"
        )
        [message] = explain(result)
        assert message.startswith("Voucher date is missing for: 'Sales' voucher.")
        assert "1st, 2nd or 31st" in message

    def test_nothing_created_and_no_reason(self):
        result = xml_parser.parse_import_result(b"<RESPONSE><CREATED>0</CREATED></RESPONSE>")
        assert explain(result) == ["Tally did not create anything and gave no reason."]

    def test_success_has_no_explanation(self):
        result = xml_parser.parse_import_result(b"<RESPONSE><ALTERED>1</ALTERED></RESPONSE>")
        assert result.success and explain(result) == []


# -- encoding ----------------------------------------------------------------------------


def test_requests_go_as_utf16_with_charset():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<RESPONSE><CREATED>1</CREATED></RESPONSE>")

    connector = TallyConnector("http://tally.test:9000", transport=httpx.MockTransport(handler))
    result = connector.post_transaction("Demo Co", purchase_tx())
    assert result.success
    [request] = seen
    assert request.headers["content-type"] == "text/xml; charset=utf-16"
    assert request.content.startswith(b"\xff\xfe")
    assert request.content.decode("utf-16").startswith("<ENVELOPE>")
    assert result.request_payload.startswith("<ENVELOPE>")  # stored readable


def test_wire_body_round_trips_any_script():
    xml = "<NAME>शर्मा ट्रेडर्स</NAME>".encode()
    assert wire_body(xml).decode("utf-16") == "<NAME>शर्मा ट्रेडर्स</NAME>"


def test_names_outside_latin_script_survive(connector: TallyConnector, mock_tally: MockTally):
    name = "शर्मा ट्रेडर्स"
    led = ProposedLedger(name=name, parent_group="Sundry Creditors", state="Karnataka")
    assert connector.create_ledger(DEMO_COMPANY, led).success
    assert name in [lg.name for lg in connector.fetch_ledgers(DEMO_COMPANY)]
    # What the old UTF-8 requests got back.
    utf8_reply = mock_tally.handle(xml_builder.ledgers_request(DEMO_COMPANY))
    assert name.encode() not in utf8_reply and b"????" in utf8_reply


def test_a_utf16_body_without_charset_is_an_unknown_request(mock_tally: MockTally):
    reply = mock_tally.handle(wire_body(xml_builder.companies_request()), "text/xml")
    assert b"Unknown Request" in reply


# -- the connector against the mock ------------------------------------------------------


def test_new_ledgers_apply_from_the_first_day_of_the_books(
    connector: TallyConnector, mock_tally: MockTally
):
    assert connector.create_ledger(DEMO_COMPANY, MEHTA).success
    made = mock_tally.companies[DEMO_COMPANY]["ledgers"]["Mehta Steel"]
    assert made["gst_details"] == [
        {"since": "20240401", "type": "Regular", "state": "Gujarat", "gstin": "24AAACM1234B1ZC"}
    ]
    assert made["mailing"][0]["since"] == "20240401"
    [synced] = [lg for lg in connector.fetch_ledgers(DEMO_COMPANY) if lg.name == "Mehta Steel"]
    assert synced.gstin == "24AAACM1234B1ZC" and synced.state == "Gujarat"


def test_books_older_than_gst_use_the_gst_start(mock_tally: MockTally):
    mock_tally.add_company("Old Books Ltd", state="Gujarat", books_from="20100401")
    old = TallyConnector("http://tally.test:9000", transport=httpx_transport(mock_tally))
    assert old.create_ledger("Old Books Ltd", MEHTA).success
    made = mock_tally.companies["Old Books Ltd"]["ledgers"]["Mehta Steel"]
    assert made["gst_details"][0]["since"] == "20170701"


def test_ledgers_made_in_tallyprime_and_erp9_both_sync(connector: TallyConnector):
    ledgers = {lg.name: lg for lg in connector.fetch_ledgers(DEMO_COMPANY)}
    # Sharma's GSTIN is only in the dated list, Gupta's only in the flat field.
    assert ledgers["Sharma Electronics"].gstin == "29ABCDE1234F1ZW"
    assert ledgers["Sharma Electronics"].state == "Karnataka"
    assert ledgers["Gupta Retail"].gstin == "27PQRSX6789K1ZS"
    assert ledgers["Gupta Retail"].state == "Maharashtra"


def test_posting_the_same_voucher_again_replaces_it(
    connector: TallyConnector, mock_tally: MockTally
):
    tx = purchase_tx()
    first = connector.post_transaction(DEMO_COMPANY, tx)
    again = connector.post_transaction(DEMO_COMPANY, tx)
    assert (first.created, first.altered) == (1, 0)
    assert again.success and (again.created, again.altered) == (0, 1)
    assert len(mock_tally.companies[DEMO_COMPANY]["vouchers"]) == 1


def test_educational_mode_dates():
    mock = MockTally(educational=True)
    connector = TallyConnector("http://tally.test:9000", transport=httpx_transport(mock))
    refused = connector.post_transaction(DEMO_COMPANY, purchase_tx())  # dated the 30th
    assert not refused.success
    [message] = refused.errors
    assert "Voucher date is missing" in message and "Educational mode" in message
    tx = purchase_tx().model_copy(update={"date": date(2026, 10, 31)})
    assert connector.post_transaction(DEMO_COMPANY, tx).success


# -- checks before posting ---------------------------------------------------------------


def _sync(client: TestClient, company_id: int) -> None:
    assert client.post(f"/api/companies/{company_id}/ledgers/sync").status_code == 200


def test_nothing_is_posted_while_the_company_is_closed_in_tally(
    admin_client: TestClient, company_id: int, mock_tally: MockTally
):
    _sync(admin_client, company_id)
    books = mock_tally.companies.pop(DEMO_COMPANY)
    mock_tally.add_company("Another Client Pvt Ltd", state="Karnataka")  # the active one

    tx = purchase_tx().model_dump(mode="json")
    resp = admin_client.post(f"/api/companies/{company_id}/vouchers", json=tx)
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        f"'{DEMO_COMPANY}' is not open in Tally. Open it in TallyPrime and try again."
    )
    assert mock_tally.companies["Another Client Pvt Ltd"]["vouchers"] == []
    assert books["vouchers"] == []


def test_a_new_ledger_may_not_take_a_name_tally_uses(
    admin_client: TestClient, company_id: int, mock_tally: MockTally
):
    _sync(admin_client, company_id)
    ledgers = mock_tally.companies[DEMO_COMPANY]["ledgers"]
    before = {k: dict(v) for k, v in ledgers.items()}
    # An alias of Sharma Electronics, spelled in other case.
    clash = ProposedLedger(name="SHARMA ELEC", parent_group="Sundry Debtors")
    tx = purchase_tx(party="SHARMA ELEC", proposed=clash).model_dump(mode="json")
    resp = admin_client.post(f"/api/companies/{company_id}/vouchers", json=tx)
    assert resp.status_code == 409
    assert "Tally already has a ledger named 'SHARMA ELEC'" in resp.json()["detail"]

    # The same check guards creating a single ledger.
    body = ProposedLedger(name="gupta retail", parent_group="Sundry Creditors")
    resp = admin_client.post(
        f"/api/companies/{company_id}/ledgers", json=body.model_dump(mode="json")
    )
    assert resp.status_code == 409
    assert "Tally already has a ledger named 'gupta retail'" in resp.json()["detail"]
    assert ledgers == before and mock_tally.companies[DEMO_COMPANY]["vouchers"] == []


# -- what the accounting engine puts on the voucher --------------------------------------


def test_purchase_carries_the_supplier_gst_details():
    tx = build_voucher(purchase(), ctx()).transaction
    assert tx is not None and tx.gst == GstDetails(
        party_gstin=SHARMA_GSTIN,
        party_registration_type="Regular",
        party_state="Karnataka",
        place_of_supply="Karnataka",
        company_gstin=COMPANY_GSTIN,
    )


def test_sale_place_of_supply_is_the_buyer_state():
    tx = build_voucher(sale(), ctx()).transaction
    assert tx is not None and tx.gst is not None
    assert tx.gst.party_gstin == GUPTA_GSTIN and tx.gst.place_of_supply == "Maharashtra"


def test_printed_place_of_supply_wins():
    tx = build_voucher(sale(place_of_supply_code="07"), ctx()).transaction
    assert tx is not None and tx.gst is not None and tx.gst.place_of_supply == "Delhi"


def test_unregistered_supplier():
    local = Party(name="Ravi Hardware", state_code="29", state="Karnataka")
    result = build_voucher(purchase(seller=local), ctx())
    tx = result.transaction
    assert tx is not None and tx.gst is not None, [p.message for p in result.problems]
    assert tx.gst.party_gstin is None
    assert tx.gst.party_registration_type == "Unregistered"
    assert tx.gst.party_state == "Karnataka"
