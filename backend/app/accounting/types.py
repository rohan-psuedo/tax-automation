"""Inputs and outputs of the accounting engine. No database or connector access here:
the engine is a pure function of (invoice, context, choices) so it can be re-run every
time a reviewer edits a field."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from app.schemas.canonical import CanonicalTransaction, ProposedLedger, VoucherKind

Direction = Literal["purchase", "sales"]
MatchMethod = Literal["choice", "gstin", "learned", "exact", "alias", "fuzzy", "default", "none"]


@dataclass(frozen=True)
class LedgerInfo:
    """A ledger master as cached from the accounting system."""

    name: str
    parent: str | None = None  # group, e.g. "Sundry Creditors", "Duties & Taxes"
    gstin: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountingContext:
    company_name: str
    company_gstin: str | None
    company_state_code: str | None  # 2-digit GST state code
    ledgers: tuple[LedgerInfo, ...]
    # Learned mappings: party ledger name -> item ledger name used last time (from approvals).
    learned_item_ledgers: dict[str, str] = field(default_factory=dict)


class LedgerChoices(BaseModel):
    """What a reviewer picked on the review screen. None means 'let the engine decide'."""

    direction: Direction | None = None
    party_ledger: str | None = None  # an existing ledger name
    create_party_ledger: bool = False  # accept the engine's ProposedLedger for the party
    item_ledger: str | None = None  # purchase/sales/expense ledger for all lines


class LedgerMatch(BaseModel):
    ledger: str | None  # matched ledger name, or None
    method: MatchMethod
    score: float  # 0..1 confidence in the match
    candidates: list[str] = []  # other plausible ledgers, best first (for the picker)


class AccountingProblem(BaseModel):
    """Why a voucher could not be built. The code lets validation drop a problem it has
    already reported in its own words (e.g. a missing date), so each root cause shows once."""

    code: str
    message: str
    field: str | None = None  # invoice field or picker it concerns, e.g. "grand_total"


class AccountingResult(BaseModel):
    direction: Direction
    direction_reason: str
    voucher_kind: VoucherKind
    party: LedgerMatch
    proposed_party: ProposedLedger | None = None  # set when no reliable party match
    item: LedgerMatch
    tax_ledgers: dict[str, str] = {}  # "cgst"/"sgst"/"igst"/"cess"/"round_off" -> ledger name
    transaction: CanonicalTransaction | None = None  # None when a balanced voucher is impossible
    problems: list[AccountingProblem] = []  # why the transaction could not be built
