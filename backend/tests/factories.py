from datetime import date
from decimal import Decimal

from app.schemas.canonical import (
    CanonicalTransaction,
    Entry,
    LedgerRef,
    ProposedLedger,
    Side,
    VoucherKind,
)


def purchase_tx(
    party: str = "Sharma Electronics",
    proposed: ProposedLedger | None = None,
    invoice_no: str = "SE/2026/0042",
) -> CanonicalTransaction:
    """Intra-state purchase: 10,000 taxable + 9% CGST + 9% SGST = 11,800."""
    return CanonicalTransaction(
        voucher_kind=VoucherKind.PURCHASE,
        date=date(2026, 9, 30),
        reference_no=invoice_no,
        reference_date=date(2026, 9, 28),
        narration="Purchase of networking equipment",
        entries=[
            Entry(ledger=LedgerRef(name="Purchase"), side=Side.DR, amount=Decimal("10000")),
            Entry(ledger=LedgerRef(name="Input CGST"), side=Side.DR, amount=Decimal("900")),
            Entry(ledger=LedgerRef(name="Input SGST"), side=Side.DR, amount=Decimal("900")),
            Entry(
                ledger=LedgerRef(name=party, proposed=proposed),
                side=Side.CR,
                amount=Decimal("11800"),
                is_party=True,
            ),
        ],
    )
