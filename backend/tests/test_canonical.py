from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.canonical import (
    CanonicalTransaction,
    Entry,
    LedgerRef,
    ProposedLedger,
    Side,
    VoucherKind,
)
from tests.factories import purchase_tx


def test_balanced_transaction_is_accepted():
    tx = purchase_tx()
    assert tx.total(Side.DR) == tx.total(Side.CR) == Decimal("11800.00")
    assert tx.party_entry is not None and tx.party_entry.ledger.name == "Sharma Electronics"


def test_unbalanced_transaction_is_rejected():
    with pytest.raises(ValidationError, match="does not balance"):
        CanonicalTransaction(
            voucher_kind=VoucherKind.JOURNAL,
            date=date(2026, 4, 1),
            entries=[
                Entry(ledger=LedgerRef(name="A"), side=Side.DR, amount=Decimal("100")),
                Entry(ledger=LedgerRef(name="B"), side=Side.CR, amount=Decimal("99.99")),
            ],
        )


def test_amounts_are_rounded_to_paise():
    entry = Entry(ledger=LedgerRef(name="A"), side=Side.DR, amount=Decimal("10.005"))
    assert entry.amount == Decimal("10.01")


def test_non_positive_amount_rejected():
    with pytest.raises(ValidationError):
        Entry(ledger=LedgerRef(name="A"), side=Side.DR, amount=Decimal("0"))


def test_proposed_ledger_name_must_match_ref():
    with pytest.raises(ValidationError, match="must equal"):
        LedgerRef(name="X", proposed=ProposedLedger(name="Y", parent_group="Sundry Creditors"))


def test_ledgers_to_create_deduplicates():
    proposed = ProposedLedger(name="New Vendor", parent_group="Sundry Creditors")
    tx = purchase_tx(party="New Vendor", proposed=proposed)
    assert [p.name for p in tx.ledgers_to_create()] == ["New Vendor"]
