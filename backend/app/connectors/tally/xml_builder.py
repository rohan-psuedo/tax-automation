"""Builds Tally XML request envelopes (TallyPrime / Tally.ERP 9 HTTP-XML interface).

Sign convention for ledger entries in Tally vouchers:
  debit  -> ISDEEMEDPOSITIVE=Yes, AMOUNT negative
  credit -> ISDEEMEDPOSITIVE=No,  AMOUNT positive
"""

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from app.schemas.canonical import (
    CanonicalTransaction,
    Entry,
    GstDetails,
    ProposedLedger,
    Side,
    VoucherKind,
)

VOUCHER_TYPE_NAMES: dict[VoucherKind, str] = {
    VoucherKind.PURCHASE: "Purchase",
    VoucherKind.SALES: "Sales",
    VoucherKind.PAYMENT: "Payment",
    VoucherKind.RECEIPT: "Receipt",
    VoucherKind.JOURNAL: "Journal",
    VoucherKind.CONTRA: "Contra",
    VoucherKind.CREDIT_NOTE: "Credit Note",
    VoucherKind.DEBIT_NOTE: "Debit Note",
}

COMPANY_FETCH = ["NAME", "GUID", "STATENAME", "GSTREGISTRATIONNUMBER", "BOOKSFROM"]
LEDGER_FETCH = [
    "NAME",
    "PARENT",
    "GUID",
    # TallyPrime 3.0+ keeps a party's GST registration and address in dated lists, returned
    # only when named here. The flat fields hold them in older releases, and in ledgers that
    # were imported the old way.
    "LEDGSTREGDETAILS.LIST",
    "LEDMAILINGDETAILS.LIST",
    "PARTYGSTIN",
    "LEDSTATENAME",
    "LANGUAGENAME",
]
GROUP_FETCH = ["NAME", "PARENT"]


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = text
    return el


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


def tally_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def tally_amount(side: Side, amount: Decimal) -> str:
    return f"-{amount:.2f}" if side == Side.DR else f"{amount:.2f}"


def _serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def export_collection_request(
    collection_id: str, object_type: str, fetch: list[str], company: str | None = None
) -> bytes:
    env = ET.Element("ENVELOPE")
    header = _sub(env, "HEADER")
    _sub(header, "VERSION", "1")
    _sub(header, "TALLYREQUEST", "Export")
    _sub(header, "TYPE", "Collection")
    _sub(header, "ID", collection_id)

    desc = _sub(_sub(env, "BODY"), "DESC")
    static = _sub(desc, "STATICVARIABLES")
    _sub(static, "SVEXPORTFORMAT", "$$SysName:XML")
    if company:
        _sub(static, "SVCURRENTCOMPANY", company)

    tdl_msg = _sub(_sub(desc, "TDL"), "TDLMESSAGE")
    coll = _sub(tdl_msg, "COLLECTION", NAME=collection_id, ISMODIFY="No")
    _sub(coll, "TYPE", object_type)
    _sub(coll, "FETCH", ", ".join(fetch))
    return _serialize(env)


def companies_request() -> bytes:
    return export_collection_request("TACompanyList", "Company", COMPANY_FETCH)


def ledgers_request(company: str) -> bytes:
    return export_collection_request("TALedgerList", "Ledger", LEDGER_FETCH, company)


def groups_request(company: str) -> bytes:
    return export_collection_request("TAGroupList", "Group", GROUP_FETCH, company)


def _import_envelope(company: str, report_name: str) -> tuple[ET.Element, ET.Element]:
    env = ET.Element("ENVELOPE")
    _sub(_sub(env, "HEADER"), "TALLYREQUEST", "Import Data")
    import_data = _sub(_sub(env, "BODY"), "IMPORTDATA")
    req_desc = _sub(import_data, "REQUESTDESC")
    _sub(req_desc, "REPORTNAME", report_name)
    _sub(_sub(req_desc, "STATICVARIABLES"), "SVCURRENTCOMPANY", company)
    message = _sub(_sub(import_data, "REQUESTDATA"), "TALLYMESSAGE", **{"xmlns:UDF": "TallyUDF"})
    return env, message


def registration_type(value: str | None) -> str | None:
    """TallyPrime 3.0+ has one type for unregistered parties and consumers."""
    return "Unregistered/Consumer" if value in {"Unregistered", "Consumer"} else value


def _address(parent: ET.Element, lines: list[str]) -> None:
    if lines:
        addr = _sub(parent, "ADDRESS.LIST", TYPE="String")
        for line in lines:
            _sub(addr, "ADDRESS", line)


def create_ledgers_request(
    company: str, ledgers: list[ProposedLedger], applicable_from: date
) -> bytes:
    """Ledgers to create. Their GST registration and address apply from applicable_from,
    which must be on or before the first voucher that uses them: Tally applies the details
    dated on or before each voucher's date."""
    env, message = _import_envelope(company, "All Masters")
    since = tally_date(applicable_from)
    for led in ledgers:
        el = _sub(message, "LEDGER", NAME=led.name, ACTION="Create")
        _sub(_sub(el, "NAME.LIST", TYPE="String"), "NAME", led.name)
        _sub(el, "PARENT", led.parent_group)
        _sub(el, "ISBILLWISEON", _yes_no(led.bill_wise))
        # TallyPrime 3.0+ reads the address and GST details from these dated lists; the flat
        # tags further down only fill the old fields. Tally shows a GSTIN only when the state
        # and country are given too.
        if led.address_lines or led.state or led.pincode:
            mailing = _sub(el, "LEDMAILINGDETAILS.LIST")
            _sub(mailing, "APPLICABLEFROM", since)
            _sub(mailing, "MAILINGNAME", led.name)
            _address(mailing, led.address_lines)
            if led.state:
                _sub(mailing, "STATE", led.state)
            _sub(mailing, "COUNTRY", led.country)
            if led.pincode:
                _sub(mailing, "PINCODE", led.pincode)
        if led.gstin or led.gst_registration_type:
            reg = _sub(el, "LEDGSTREGDETAILS.LIST")
            _sub(reg, "APPLICABLEFROM", since)
            if led.gst_registration_type:
                _sub(reg, "GSTREGISTRATIONTYPE", registration_type(led.gst_registration_type))
            if led.state:
                _sub(reg, "STATE", led.state)
            if led.gstin:
                _sub(reg, "GSTIN", led.gstin)
        # The same details for Tally.ERP 9 and TallyPrime before 3.0.
        _address(el, led.address_lines)
        if led.state:
            _sub(el, "LEDSTATENAME", led.state)
        _sub(el, "COUNTRYNAME", led.country)
        if led.pincode:
            _sub(el, "PINCODE", led.pincode)
        if led.gst_registration_type:
            _sub(el, "GSTREGISTRATIONTYPE", led.gst_registration_type)
        if led.gstin:
            _sub(el, "PARTYGSTIN", led.gstin)
    return _serialize(env)


def _ledger_entry(parent: ET.Element, entry: Entry, default_bill_ref: str | None) -> None:
    le = _sub(parent, "ALLLEDGERENTRIES.LIST")
    _sub(le, "LEDGERNAME", entry.ledger.name)
    _sub(le, "ISDEEMEDPOSITIVE", _yes_no(entry.side == Side.DR))
    _sub(le, "ISPARTYLEDGER", _yes_no(entry.is_party))
    amount = tally_amount(entry.side, entry.amount)
    _sub(le, "AMOUNT", amount)
    bill_ref = entry.bill_ref or (default_bill_ref if entry.is_party else None)
    if bill_ref:
        bill = _sub(le, "BILLALLOCATIONS.LIST")
        _sub(bill, "NAME", bill_ref)
        _sub(bill, "BILLTYPE", "New Ref")
        _sub(bill, "AMOUNT", amount)


def _gst_details(v: ET.Element, gst: GstDetails) -> None:
    """The party's GST particulars. Without them Tally can file the voucher under
    "Uncertain Transactions" in its GST returns instead of the right table."""
    if gst.company_gstin:
        _sub(v, "CMPGSTIN", gst.company_gstin)
    if gst.party_gstin:
        _sub(v, "PARTYGSTIN", gst.party_gstin)
        # Only "Regular" is spelled the same in every Tally release; for anyone else Tally
        # takes the type from the party ledger.
        if gst.party_registration_type == "Regular":
            _sub(v, "GSTREGISTRATIONTYPE", "Regular")
    if gst.party_state:
        _sub(v, "STATENAME", gst.party_state)
        _sub(v, "COUNTRYOFRESIDENCE", "India")
    if gst.place_of_supply:
        _sub(v, "PLACEOFSUPPLY", gst.place_of_supply)


def voucher_request(company: str, tx: CanonicalTransaction) -> bytes:
    env, message = _import_envelope(company, "Vouchers")
    vch_type = VOUCHER_TYPE_NAMES[tx.voucher_kind]
    v = _sub(
        message,
        "VOUCHER",
        REMOTEID=str(tx.id),
        VCHTYPE=vch_type,
        ACTION="Create",
        OBJVIEW="Accounting Voucher View",
    )
    _sub(v, "DATE", tally_date(tx.date))
    _sub(v, "EFFECTIVEDATE", tally_date(tx.date))
    _sub(v, "VOUCHERTYPENAME", vch_type)
    if tx.voucher_number:
        _sub(v, "VOUCHERNUMBER", tx.voucher_number)
    if tx.reference_no:
        _sub(v, "REFERENCE", tx.reference_no)
    if tx.reference_date:
        _sub(v, "REFERENCEDATE", tally_date(tx.reference_date))
    party = tx.party_entry
    if party:
        _sub(v, "PARTYLEDGERNAME", party.ledger.name)
    if tx.gst:
        _gst_details(v, tx.gst)
    if tx.narration:
        _sub(v, "NARRATION", tx.narration)
    _sub(v, "PERSISTEDVIEW", "Accounting Voucher View")
    _sub(v, "ISINVOICE", "No")

    # Tally convention: party line first, then the rest in their given order.
    ordered = sorted(tx.entries, key=lambda e: not e.is_party)
    for entry in ordered:
        _ledger_entry(v, entry, tx.reference_no)
    return _serialize(env)
