"""Measures how accurately the AI service reads invoices (phase 3 target: 95% of key fields).

The documents are read by the AI service and model chosen in Settings, so a run shows how
well that choice does.

Two sources of truth:

  --from-posted   Entries your team reviewed and posted to Tally. A person checked every field
                  of these, so they are the most realistic answer key. Each original document
                  is read again by the AI service and compared field by field, and the voucher
                  its reading would produce is compared with the voucher that was posted.
  --folder DIR    Invoice files with a matching "<name>.expected.json" next to each, holding
                  the correct values (same field names as the review screen; omit what you
                  don't want checked). Example:
                  {"invoice_number": "SE/2026/0042", "invoice_date": "2026-09-28",
                   "seller": {"name": "Sharma Electronics", "gstin": "29ABCDE1234F1ZW"},
                   "taxable_value": "10000.00", "cgst": "900", "sgst": "900",
                   "grand_total": "11800"}

Every document costs one request to the AI service, so nothing is sent without --yes; without
it the run only lists what would be read and, when the service's prices are known, a rough
estimate of the cost.

    uv run python -m app.extraction.evaluate --from-posted --limit 40
    uv run python -m app.extraction.evaluate --from-posted --limit 40 --yes
"""

import argparse
import json
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.accounting import AccountingResult, build_voucher
from app.accounting.matching import name_similarity
from app.extraction import services
from app.extraction.services import Service
from app.ingestion import filetypes, storage
from app.models import Company, Document, Voucher, VoucherStatus
from app.normalization.normalize import normalize
from app.schemas.invoice import NormalizedInvoice

TARGET = 0.95
# Tokens of a typical 1-2 page invoice (the pages, the instructions, the answer and some
# thinking), priced at the model's rates for the rough estimate shown before a run.
TYPICAL_INPUT_TOKENS = 4_000
TYPICAL_OUTPUT_TOKENS = 1_000
NAME_MATCH = 90  # a party name counts as read correctly at this similarity (0-100)

TEXT_FIELDS = ("invoice_number",)
DATE_FIELDS = ("invoice_date",)
GSTIN_FIELDS = ("seller.gstin", "buyer.gstin")
NAME_FIELDS = ("seller.name", "buyer.name")
AMOUNT_FIELDS = ("taxable_value", "cgst", "sgst", "igst", "cess", "grand_total")
KEY_FIELDS = (
    "document_type",
    *TEXT_FIELDS,
    *DATE_FIELDS,
    *NAME_FIELDS,
    *GSTIN_FIELDS,
    *AMOUNT_FIELDS,
)


@dataclass
class Case:
    """One document to read, with the values it should produce."""

    label: str
    kind: str
    file_path: Path
    expected: dict[str, Any]  # NormalizedInvoice-shaped (JSON); missing fields are not checked
    company_name: str
    company_gstin: str | None
    text: str | None = None
    page_images: list[Path] = field(default_factory=list)
    posted_entries: list[tuple[str, str, str]] | None = None  # (ledger, side, amount) as posted
    company: Company | None = None


@dataclass
class CaseResult:
    label: str
    fields: dict[str, bool]  # field -> read correctly (only fields the answer key has)
    mismatches: list[str]
    voucher_match: bool | None = None  # None when there is no posted voucher to compare
    cost_usd: float = 0.0
    tokens: int = 0  # input + output; with cost_usd 0, the service did not report the cost
    seconds: float = 0.0
    error: str | None = None


# -- comparing ----------------------------------------------------------------------------


def _get(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _text(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def field_correct(path: str, expected: Any, got: Any) -> bool:
    """Whether `got` (from the AI service's reading) matches the expected value for `path`."""
    if path in AMOUNT_FIELDS:
        exp, actual = _decimal(expected), _decimal(got)
        # An absent tax is 0 on the review screen, so 0 and missing are the same value.
        if path not in ("taxable_value", "grand_total"):
            exp, actual = exp or Decimal("0.00"), actual or Decimal("0.00")
        return exp == actual
    if path in NAME_FIELDS:
        if not expected and not got:
            return True
        return name_similarity(str(expected or ""), str(got or "")) >= NAME_MATCH
    if path in DATE_FIELDS:
        return str(expected or "") == str(got or "")
    return _text(expected) == _text(got)  # numbers, GSTINs, document type


def compare(expected: dict[str, Any], got: NormalizedInvoice) -> tuple[dict[str, bool], list[str]]:
    actual = got.model_dump(mode="json")
    fields: dict[str, bool] = {}
    mismatches: list[str] = []
    for path in KEY_FIELDS:
        exp = _get(expected, path)
        if exp is None:
            continue  # the answer key doesn't say, so this field isn't scored
        ok = field_correct(path, exp, _get(actual, path))
        fields[path] = ok
        if not ok:
            mismatches.append(f"{path}: expected {exp!r}, read {_get(actual, path)!r}")
    return fields, mismatches


def _entries(result: AccountingResult) -> list[tuple[str, str, str]] | None:
    if result.transaction is None:
        return None
    return sorted(
        (e.ledger.name, e.side.value, f"{e.amount:.2f}") for e in result.transaction.entries
    )


# -- building the cases -------------------------------------------------------------------


def cases_from_posted(db, *, company_id: int | None = None, limit: int = 50) -> list[Case]:
    """Posted entries, newest first: reviewed by a person, so their values are the answer."""
    stmt = (
        select(Voucher, Document, Company)
        .join(Document, Document.id == Voucher.document_id)
        .join(Company, Company.id == Voucher.company_id)
        .where(Voucher.status == VoucherStatus.POSTED)
        .order_by(Voucher.id.desc())
        .limit(limit)
    )
    if company_id is not None:
        stmt = stmt.where(Voucher.company_id == company_id)
    cases = []
    for voucher, doc, company in db.execute(stmt).all():
        path = storage.absolute(doc.stored_path)
        if not path.exists():
            continue
        tx = (voucher.accounting or {}).get("transaction") or {}
        posted = sorted(
            (e["ledger"]["name"], e["side"], f"{Decimal(str(e['amount'])):.2f}")
            for e in tx.get("entries", [])
        )
        cases.append(
            Case(
                label=f"#{doc.id} {doc.original_filename}",
                kind=doc.kind,
                file_path=path,
                expected=voucher.invoice or {},
                company_name=company.external_company_name,
                company_gstin=company.gstin,
                text=doc.text,
                page_images=[
                    storage.page_image(doc.stored_path, p["number"])
                    for p in (doc.parsed or {}).get("pages", [])
                ],
                posted_entries=posted or None,
                company=company,
            )
        )
    return cases


def cases_from_folder(folder: Path, company_name: str, company_gstin: str | None) -> list[Case]:
    from app.parsing import parse_file

    cases = []
    pages_root = Path(tempfile.mkdtemp(prefix="evaluate-pages-"))
    for expected_file in sorted(folder.glob("*.expected.json")):
        stem = expected_file.name.removesuffix(".expected.json")
        matches = [p for p in folder.glob(f"{stem}.*") if not p.name.endswith(".expected.json")]
        if not matches:
            print(f"Skipped {expected_file.name}: no invoice file named {stem}.*", file=sys.stderr)
            continue
        path = matches[0]
        ftype = filetypes.detect(path.name, path.read_bytes())
        parsed = parse_file(path, ftype.kind, pages_root / stem)
        cases.append(
            Case(
                label=path.name,
                kind=ftype.kind,
                file_path=path,
                expected=json.loads(expected_file.read_text(encoding="utf-8")),
                company_name=company_name,
                company_gstin=company_gstin,
                text=parsed.text or None,
                page_images=[pages_root / stem / f"page-{p.number}.png" for p in parsed.pages],
            )
        )
    return cases


# -- running --------------------------------------------------------------------------------


Extractor = Callable[..., Any]


def run_case(
    case: Case, extract: Extractor, context_for: Callable[[Case], Any] | None
) -> CaseResult:
    started = time.monotonic()
    try:
        outcome = extract(
            kind=case.kind,
            file_path=case.file_path,
            page_images=case.page_images,
            text=case.text,
            company_name=case.company_name,
            company_gstin=case.company_gstin,
        )
    except Exception as exc:  # one unreadable document must not stop the evaluation
        message = getattr(exc, "message", None) or str(exc)
        return CaseResult(case.label, {}, [f"not read: {message}"], error=message)
    invoice = normalize(outcome.extraction)
    fields, mismatches = compare(case.expected, invoice)
    voucher_match = None
    if case.posted_entries is not None and context_for is not None:
        result = build_voucher(invoice, context_for(case), voucher_id=uuid.uuid4())
        voucher_match = _entries(result) == case.posted_entries
        if not voucher_match:
            mismatches.append("voucher: this reading would not give the voucher that was posted")
    return CaseResult(
        case.label,
        fields,
        mismatches,
        voucher_match=voucher_match,
        cost_usd=outcome.cost_usd,
        tokens=(outcome.input_tokens or 0) + (outcome.output_tokens or 0),
        seconds=time.monotonic() - started,
    )


@dataclass
class Report:
    results: list[CaseResult]
    target: float = TARGET
    service: str = "the AI service"  # who read the documents, as said mid-sentence

    @property
    def scored(self) -> list[bool]:
        return [ok for r in self.results for ok in r.fields.values()]

    @property
    def accuracy(self) -> float | None:
        return sum(self.scored) / len(self.scored) if self.scored else None

    def per_field(self) -> dict[str, tuple[int, int]]:
        counts: dict[str, tuple[int, int]] = {}
        for r in self.results:
            for path, ok in r.fields.items():
                right, total = counts.get(path, (0, 0))
                counts[path] = (right + ok, total + 1)
        return counts

    def as_dict(self) -> dict[str, Any]:
        vouchers = [r.voucher_match for r in self.results if r.voucher_match is not None]
        return {
            "date": date.today().isoformat(),
            "service": self.service,
            "documents": len(self.results),
            "unreadable": sum(r.error is not None for r in self.results),
            "key_field_accuracy": self.accuracy,
            "target": self.target,
            "passed": self.accuracy is not None and self.accuracy >= self.target,
            "per_field": {k: {"right": a, "of": b} for k, (a, b) in self.per_field().items()},
            "vouchers_identical": f"{sum(vouchers)}/{len(vouchers)}" if vouchers else None,
            "total_cost_usd": round(sum(r.cost_usd for r in self.results), 4),
            "total_tokens": sum(r.tokens for r in self.results),
            "documents_without_cost": len(self._unpriced()),
            "documents_with_mistakes": [
                {"document": r.label, "mistakes": r.mismatches}
                for r in self.results
                if r.mismatches
            ],
        }

    def text(self) -> str:
        d = self.as_dict()
        lines = [
            f"Documents read by {self.service}: {d['documents']} "
            f"({d['unreadable']} could not be read)"
        ]
        if self.accuracy is None:
            lines.append("No fields were scored.")
        else:
            verdict = "meets" if d["passed"] else "is below"
            lines.append(
                f"Key fields read correctly: {self.accuracy:.1%}, which {verdict} the "
                f"{self.target:.0%} target."
            )
        if d["vouchers_identical"]:
            lines.append(f"Vouchers identical to what was posted: {d['vouchers_identical']}")
        lines.append(self._cost_line())
        lines.append("")
        lines.append("Per field:")
        for path, (right, total) in sorted(self.per_field().items()):
            lines.append(f"  {path:<15} {right}/{total}  ({right / total:.0%})")
        for item in d["documents_with_mistakes"]:
            lines.append("")
            lines.append(item["document"])
            lines.extend(f"  - {m}" for m in item["mistakes"])
        return "\n".join(lines)

    def _unpriced(self) -> list[CaseResult]:
        """Documents that used tokens but came back with no cost: the service did not report
        it (most OpenAI-compatible ones) or its prices for the model are not known here."""
        return [r for r in self.results if r.tokens > 0 and not r.cost_usd]

    def _cost_line(self) -> str:
        # $0.00 for a run that used tokens would read as free, which it may not have been.
        total = sum(r.cost_usd for r in self.results)
        unpriced = len(self._unpriced())
        if not unpriced:
            return f"Cost: ${total:.2f} in all"
        if total > 0:
            documents = f"{unpriced} document{'' if unpriced == 1 else 's'}"
            return (
                f"Cost: ${total:.2f} in all, not counting {documents} whose cost was not "
                f"reported by {self.service}"
            )
        tokens = sum(r.tokens for r in self.results)
        return f"Cost: not reported by {self.service} ({tokens:,} tokens in all)"


def evaluate(
    cases: Iterable[Case], extract: Extractor, context_for=None, *, service: str = "the AI service"
) -> Report:
    """service: who reads the documents, as said mid-sentence ("Gemini", "the AI service")."""
    return Report([run_case(case, extract, context_for) for case in cases], service=service)


def estimated_cost_per_document(service: Service, model: str) -> float | None:
    """Rough cost of reading one 1-2 page invoice, or None when the service's prices for the
    model are not known here (OpenRouter, Groq, a local server, a model not in the tables)."""
    match service.adapter:
        case "anthropic":
            from app.extraction.extractor import cost_usd

            usd = cost_usd(model, TYPICAL_INPUT_TOKENS, TYPICAL_OUTPUT_TOKENS)
        case "gemini":
            from app.extraction.adapters import gemini

            usd = float(gemini._cost(model, TYPICAL_INPUT_TOKENS, TYPICAL_OUTPUT_TOKENS, 0))
        case "openai":
            from app.extraction.adapters import openai_api

            usd = float(openai_api.cost(model, TYPICAL_INPUT_TOKENS, TYPICAL_OUTPUT_TOKENS))
        case _:
            return None
    return usd or None  # the adapters price a model they don't know at 0


def main(argv: list[str] | None = None) -> int:
    from app.services import app_settings

    # The service the worker would use, so the run measures what the office really gets.
    settings = app_settings.current()
    service = services.get(settings.ai_provider) or services.DEFAULT
    model = settings.service_settings(service.id).model

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-posted", action="store_true", help="use reviewed, posted entries")
    source.add_argument("--folder", type=Path, help="invoices with <name>.expected.json files")
    parser.add_argument("--company", type=int, help="only this company (with --from-posted)")
    parser.add_argument("--company-name", default="", help="your company name (with --folder)")
    parser.add_argument("--company-gstin", default=None, help="your GSTIN (with --folder)")
    parser.add_argument("--limit", type=int, default=50, help="at most this many documents")
    parser.add_argument("--out", type=Path, help="also write the report as JSON to this file")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=f"really send the documents to {service.in_sentence}",
    )
    args = parser.parse_args(argv)

    from app.db import SessionLocal
    from app.extraction.extractor import extract_invoice
    from app.services.vouchers import accounting_context

    with SessionLocal() as db:
        if args.from_posted:
            cases = cases_from_posted(db, company_id=args.company, limit=args.limit)
        else:
            cases = cases_from_folder(args.folder, args.company_name, args.company_gstin)
            cases = cases[: args.limit]
        if not cases:
            print("Nothing to evaluate: no posted entries (or no *.expected.json files) found.")
            return 1
        reader = f"{service.in_sentence} ({model})" if model else service.in_sentence
        per_document = estimated_cost_per_document(service, model)
        if per_document is None:
            print(
                f"{len(cases)} document(s) to read with {reader}. The cost is not known "
                f"beforehand: {service.in_sentence}'s prices for this model are not known here."
            )
        else:
            estimate = len(cases) * per_document
            print(f"{len(cases)} document(s) to read with {reader}, roughly ${estimate:.2f}.")
        if not args.yes:
            for case in cases:
                print(f"  {case.label}")
            print("Nothing was sent. Run again with --yes to read them.")
            return 0

        def context_for(case: Case):
            return accounting_context(db, case.company) if case.company else None

        report = evaluate(
            cases,
            extract_invoice,
            context_for if args.from_posted else None,
            service=service.in_sentence,
        )
    print(report.text())
    if args.out:
        args.out.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(f"\nReport written to {args.out}")
    return 0 if report.as_dict()["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
