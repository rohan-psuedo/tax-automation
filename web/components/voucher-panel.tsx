"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useMemo, useState } from "react";
import { Button, ErrorNote, formatDateTime } from "@/components/ui";
import {
  ApiError,
  type Invoice,
  type InvoiceParty,
  type Issue,
  type Ledger,
  type LedgerChoices,
  type User,
  type Voucher,
  type VoucherStatus,
  api,
  vouchersApi,
} from "@/lib/api";

export const VOUCHER_STATUS_LABEL: Record<VoucherStatus, string> = {
  pending: "Waiting for AI",
  extracting: "AI is reading",
  needs_review: "Needs review",
  ready: "Ready to post",
  posting: "Posting…",
  posted: "In Tally",
  post_failed: "Posting failed",
  rejected: "Rejected",
};

const VOUCHER_STATUS_STYLE: Record<VoucherStatus, string> = {
  pending: "bg-rule/60 text-ink-soft",
  extracting: "bg-amber-tint text-amber",
  needs_review: "bg-amber-tint text-amber",
  ready: "bg-ledger-tint text-ledger",
  posting: "bg-amber-tint text-amber",
  posted: "bg-ledger text-white",
  post_failed: "bg-red-tint text-red-ink",
  rejected: "bg-rule/60 text-ink-soft",
};

export function VoucherBadge({ status }: { status: VoucherStatus }) {
  return (
    <span
      className={`inline-block rounded-[3px] px-1.5 py-0.5 text-xs font-medium ${VOUCHER_STATUS_STYLE[status]}`}
    >
      {VOUCHER_STATUS_LABEL[status]}
    </span>
  );
}

export const voucherInProgress = (v: Pick<Voucher, "status">) =>
  v.status === "pending" || v.status === "extracting" || v.status === "posting";

const EDITABLE: VoucherStatus[] = ["needs_review", "ready", "post_failed"];

const KIND_LABEL: Record<string, string> = {
  purchase: "Purchase",
  sales: "Sales",
  credit_note: "Credit Note",
  debit_note: "Debit Note",
  payment: "Payment",
  receipt: "Receipt",
  journal: "Journal",
  contra: "Contra",
};

const DOC_TYPES: [string, string][] = [
  ["tax_invoice", "Tax invoice"],
  ["bill_of_supply", "Bill of supply"],
  ["credit_note", "Credit note"],
  ["debit_note", "Debit note"],
  ["receipt", "Receipt"],
  ["proforma", "Proforma"],
  ["other", "Other"],
];

const PARTY_GROUPS = ["Sundry Creditors", "Sundry Debtors"];
// Ledgers an invoice's items can go to, by direction (as the accounting engine ranks them).
const ITEM_GROUPS: Record<"purchase" | "sales", string[]> = {
  purchase: ["Purchase Accounts", "Direct Expenses", "Indirect Expenses", "Fixed Assets"],
  sales: ["Sales Accounts", "Direct Incomes", "Indirect Incomes"],
};
// Word start, as in the engine: "Rounding Off" is a round-off ledger, "Ground Rent" is not.
const ROUND_OFF = /\bround/i;

const CREATE_NEW = "__create__";
const AUTO = "";

const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function money(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  return Number.isFinite(n) ? inr.format(n) : String(value);
}

/** Text inputs hold strings; the API wants null for empty optional decimals. */
function decimalOrNull(raw: string | null) {
  const v = (raw ?? "").replace(/,/g, "").trim();
  return v === "" ? null : v;
}

export function VoucherPanel({ voucher, user }: { voucher: Voucher; user: User | undefined }) {
  const queryClient = useQueryClient();
  const [invoice, setInvoice] = useState<Invoice>(voucher.invoice);
  const [choices, setChoices] = useState<LedgerChoices>(voucher.choices);
  const [baseline, setBaseline] = useState(voucher.updated_at);

  // Server copy changed (e.g. AI finished, or another reviewer saved): take it.
  if (baseline !== voucher.updated_at) {
    setBaseline(voucher.updated_at);
    setInvoice(voucher.invoice);
    setChoices(voucher.choices);
  }

  const dirty =
    JSON.stringify(invoice) !== JSON.stringify(voucher.invoice) ||
    JSON.stringify(choices) !== JSON.stringify(voucher.choices);
  const editable = EDITABLE.includes(voucher.status);
  const canPost = user?.role === "admin" || user?.role === "reviewer";

  const ledgers = useQuery({
    queryKey: ["ledgers", voucher.company_id, "", ""],
    queryFn: () => api.ledgers(voucher.company_id),
  });

  const onSaved = (v: Voucher) => {
    queryClient.setQueryData(["voucher", voucher.document_id], v);
    queryClient.invalidateQueries({ queryKey: ["documents", voucher.company_id] });
    queryClient.invalidateQueries({ queryKey: ["vouchers", voucher.company_id] });
  };

  const payload = (): [Invoice, LedgerChoices] => [
    {
      ...invoice,
      taxable_value: decimalOrNull(invoice.taxable_value),
      grand_total: decimalOrNull(invoice.grand_total),
      cgst: decimalOrNull(invoice.cgst) ?? "0",
      sgst: decimalOrNull(invoice.sgst) ?? "0",
      igst: decimalOrNull(invoice.igst) ?? "0",
      cess: decimalOrNull(invoice.cess) ?? "0",
      round_off: decimalOrNull(invoice.round_off) ?? "0",
      invoice_number: invoice.invoice_number?.trim() || null,
      invoice_date: invoice.invoice_date || null,
    },
    choices,
  ];

  const save = useMutation({
    mutationFn: () => vouchersApi.update(voucher.id, ...payload()),
    onSuccess: onSaved,
  });
  const postIt = useMutation({
    mutationFn: async () => {
      if (dirty) await vouchersApi.update(voucher.id, ...payload());
      return vouchersApi.post(voucher.id);
    },
    onSuccess: onSaved,
    onError: () => queryClient.invalidateQueries({ queryKey: ["voucher", voucher.document_id] }),
  });
  const reject = useMutation({
    mutationFn: () => vouchersApi.reject(voucher.id),
    onSuccess: onSaved,
  });
  const reopen = useMutation({
    mutationFn: () => vouchersApi.reopen(voucher.id),
    onSuccess: onSaved,
  });
  const again = useMutation({
    mutationFn: () => vouchersApi.extractAgain(voucher.id),
    onSuccess: onSaved,
  });
  const actionError = [save, postIt, reject, reopen, again].find((m) => m.error)?.error;

  const accounting = voucher.accounting;
  const direction = choices.direction ?? accounting?.direction ?? "purchase";
  const errors = voucher.issues.filter((i) => i.severity === "error");
  const warnings = voucher.issues.filter((i) => i.severity === "warning");
  const fieldIssue = (field: string) => voucher.issues.find((i) => i.field === field);

  const setField = <K extends keyof Invoice>(key: K, value: Invoice[K]) =>
    setInvoice((cur) => ({ ...cur, [key]: value }));
  const setParty = (side: "seller" | "buyer", patch: Partial<InvoiceParty>) =>
    setInvoice((cur) => ({ ...cur, [side]: { ...cur[side], ...patch } }));

  if (voucher.status === "pending" || voucher.status === "extracting") {
    return (
      <div className="space-y-3">
        <VoucherBadge status={voucher.status} />
        <p className="text-sm text-ink-soft">
          The AI is reading this document. The voucher appears here when it&apos;s done, usually
          within a minute.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <VoucherBadge status={voucher.status} />
        {accounting && (
          <span className="text-sm text-ink-soft">
            {KIND_LABEL[accounting.voucher_kind] ?? accounting.voucher_kind} voucher
          </span>
        )}
        {voucher.source === "ai" && voucher.confidence > 0 && (
          <span className="ml-auto text-xs text-ink-soft">
            Read by AI, {Math.round(voucher.confidence * 100)}% sure
          </span>
        )}
      </div>

      {voucher.status === "posted" && (
        <p className="border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm">
          Posted to Tally{voucher.posted_at ? ` on ${formatDateTime(voucher.posted_at)}` : ""}.
          {voucher.external_id && ` Tally voucher ID ${voucher.external_id}.`}
        </p>
      )}
      {voucher.status === "post_failed" && voucher.post_error && (
        <ErrorNote>Tally didn&apos;t accept it: {voucher.post_error}</ErrorNote>
      )}
      {voucher.extraction_error && editable && (
        <p className="border-l-2 border-amber bg-amber-tint px-3 py-2 text-sm text-amber">
          {voucher.extraction_error} Enter the details below to post it anyway.
        </p>
      )}

      {voucher.issues.length > 0 && voucher.status !== "posted" && (
        <ul className="space-y-1.5 text-sm" aria-label="Issues">
          {[...errors, ...warnings].map((issue, i) => (
            <li
              key={`${issue.code}-${i}`}
              className={`border-l-2 px-3 py-1.5 ${
                issue.severity === "error"
                  ? "border-red-ink bg-red-tint text-red-ink"
                  : "border-amber bg-amber-tint text-amber"
              }`}
            >
              {issue.message}
            </li>
          ))}
        </ul>
      )}

      <fieldset disabled={!editable} className="space-y-5 disabled:opacity-90">
        <Section title="Invoice">
          <div className="grid grid-cols-2 gap-3">
            <Labeled
              label="Number"
              issue={fieldIssue("invoice_number")}
              confidence={invoice.confidence.invoice_number}
            >
              <TextInput
                value={invoice.invoice_number ?? ""}
                onChange={(v) => setField("invoice_number", v)}
              />
            </Labeled>
            <Labeled
              label="Date"
              issue={fieldIssue("invoice_date")}
              confidence={invoice.confidence.invoice_date}
            >
              <TextInput
                type="date"
                value={invoice.invoice_date ?? ""}
                onChange={(v) => setField("invoice_date", v || null)}
              />
            </Labeled>
            <Labeled label="Document type" issue={fieldIssue("document_type")}>
              <select
                value={invoice.document_type}
                onChange={(e) => setField("document_type", e.target.value)}
                className={SELECT}
              >
                {DOC_TYPES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </Labeled>
            <Labeled label="Entry for your books" hint={accounting?.direction_reason}>
              <select
                value={choices.direction ?? AUTO}
                onChange={(e) =>
                  setChoices((c) => ({
                    ...c,
                    direction: (e.target.value || null) as LedgerChoices["direction"],
                  }))
                }
                className={SELECT}
              >
                <option value={AUTO}>
                  Automatic ({accounting?.direction === "sales" ? "sales" : "purchase"})
                </option>
                <option value="purchase">Purchase (we bought)</option>
                <option value="sales">Sales (we sold)</option>
              </select>
            </Labeled>
          </div>
        </Section>

        <Section title="Parties">
          <div className="grid grid-cols-2 gap-3">
            {(["seller", "buyer"] as const).map((side) => (
              <div key={side} className="space-y-2">
                <Labeled
                  label={side === "seller" ? "Seller" : "Buyer"}
                  issue={fieldIssue(`${side}.name`)}
                  confidence={invoice.confidence[`${side}.name`]}
                >
                  <TextInput
                    value={invoice[side].name ?? ""}
                    onChange={(v) => setParty(side, { name: v || null })}
                  />
                </Labeled>
                <Labeled
                  label="GSTIN"
                  issue={fieldIssue(`${side}.gstin`)}
                  confidence={invoice.confidence[`${side}.gstin`]}
                >
                  <TextInput
                    mono
                    value={invoice[side].gstin ?? ""}
                    maxLength={15}
                    onChange={(v) =>
                      setParty(side, { gstin: v.replace(/\s/g, "").toUpperCase() || null })
                    }
                  />
                </Labeled>
              </div>
            ))}
          </div>
        </Section>

        <Section title="Amounts">
          <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3">
            {(
              [
                ["taxable_value", "Taxable value"],
                ["cgst", "CGST"],
                ["sgst", "SGST / UTGST"],
                ["igst", "IGST"],
                ["cess", "Cess"],
                ["round_off", "Round off"],
              ] as const
            ).map(([key, label]) => (
              <Labeled
                key={key}
                label={label}
                issue={fieldIssue(key)}
                confidence={invoice.confidence[key]}
              >
                <TextInput numeric value={invoice[key] ?? ""} onChange={(v) => setField(key, v)} />
              </Labeled>
            ))}
            <Labeled
              label="Total"
              issue={fieldIssue("grand_total")}
              confidence={invoice.confidence.grand_total}
            >
              <TextInput
                numeric
                strong
                value={invoice.grand_total ?? ""}
                onChange={(v) => setField("grand_total", v)}
              />
            </Labeled>
          </div>
        </Section>

        <Section title="Ledgers in Tally">
          <LedgerPickers
            voucher={voucher}
            choices={choices}
            setChoices={setChoices}
            ledgers={ledgers.data ?? []}
            direction={direction}
          />
        </Section>
      </fieldset>

      <Section title="Voucher">
        {dirty ? (
          <p className="text-sm text-ink-soft">Save your changes to see the updated voucher.</p>
        ) : accounting?.transaction ? (
          <EntriesTable voucher={voucher} />
        ) : (
          <p className="text-sm text-ink-soft">
            The voucher can&apos;t be built yet. Fix the issues above.
          </p>
        )}
      </Section>

      {actionError && (
        <ErrorNote>
          {actionError instanceof ApiError ? actionError.message : "Something went wrong."}
        </ErrorNote>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-rule pt-4">
        {editable && canPost && (
          <Button
            onClick={() => postIt.mutate()}
            disabled={postIt.isPending || (!dirty && errors.length > 0)}
            title={!dirty && errors.length > 0 ? "Fix the issues marked in red first" : undefined}
          >
            {postIt.isPending ? "Posting…" : "Post to Tally"}
          </Button>
        )}
        {editable && dirty && (
          <Button variant="secondary" onClick={() => save.mutate()} disabled={save.isPending}>
            Save changes
          </Button>
        )}
        {editable && dirty && (
          <Button
            variant="quiet"
            onClick={() => {
              setInvoice(voucher.invoice);
              setChoices(voucher.choices);
            }}
          >
            Undo changes
          </Button>
        )}
        <span className="ml-auto flex gap-1">
          {(editable || voucher.status === "rejected") && (
            <Button variant="quiet" onClick={() => again.mutate()} disabled={again.isPending}>
              Read again with AI
            </Button>
          )}
          {editable && canPost && (
            <Button
              variant="quiet"
              className="text-red-ink hover:bg-red-tint hover:text-red-ink"
              onClick={() => reject.mutate()}
              disabled={reject.isPending}
            >
              Reject
            </Button>
          )}
          {voucher.status === "rejected" && canPost && (
            <Button variant="secondary" onClick={() => reopen.mutate()} disabled={reopen.isPending}>
              Reopen
            </Button>
          )}
        </span>
      </div>
      {editable && !canPost && (
        <p className="text-xs text-ink-soft">A reviewer or administrator posts entries to Tally.</p>
      )}
    </div>
  );
}

function LedgerPickers({
  voucher,
  choices,
  setChoices,
  ledgers,
  direction,
}: {
  voucher: Voucher;
  choices: LedgerChoices;
  setChoices: (fn: (c: LedgerChoices) => LedgerChoices) => void;
  ledgers: Ledger[];
  direction: "purchase" | "sales";
}) {
  const accounting = voucher.accounting;
  const proposed = accounting?.proposed_party ?? null;
  const partyOptions = useMemo(
    () => ledgers.filter((l) => l.parent && PARTY_GROUPS.includes(l.parent)).map((l) => l.name),
    [ledgers],
  );
  const itemOptions = useMemo(
    () =>
      ledgers
        .filter((l) => l.parent && ITEM_GROUPS[direction].includes(l.parent))
        .filter((l) => !ROUND_OFF.test(l.name))
        .map((l) => l.name),
    [ledgers, direction],
  );
  const partyMatch = accounting?.party;
  const itemMatch = accounting?.item;

  const partyValue = choices.create_party_ledger ? CREATE_NEW : (choices.party_ledger ?? AUTO);
  const autoParty = partyMatch?.ledger
    ? `${partyMatch.ledger} (${METHOD_LABEL[partyMatch.method] ?? partyMatch.method})`
    : proposed
      ? "No match found"
      : "None";

  return (
    <div className="grid gap-3">
      <Labeled label={direction === "sales" ? "Customer ledger" : "Supplier ledger"}>
        <select
          value={partyValue}
          onChange={(e) => {
            const v = e.target.value;
            setChoices((c) => ({
              ...c,
              create_party_ledger: v === CREATE_NEW,
              party_ledger: v === CREATE_NEW || v === AUTO ? null : v,
            }));
          }}
          className={SELECT}
        >
          <option value={AUTO}>Automatic: {autoParty}</option>
          {proposed && (
            <option value={CREATE_NEW}>
              Create new: {proposed.name} under {proposed.parent_group}
            </option>
          )}
          {uniq([...(partyMatch?.candidates ?? []), ...partyOptions]).map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </Labeled>
      <Labeled label={direction === "sales" ? "Sales ledger" : "Purchase or expense ledger"}>
        <select
          value={choices.item_ledger ?? AUTO}
          onChange={(e) => setChoices((c) => ({ ...c, item_ledger: e.target.value || null }))}
          className={SELECT}
        >
          <option value={AUTO}>
            Automatic: {itemMatch?.ledger ?? "none found"}
            {itemMatch?.method === "learned" ? " (used last time)" : ""}
          </option>
          {uniq([...(itemMatch?.candidates ?? []), ...itemOptions]).map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </Labeled>
    </div>
  );
}

const METHOD_LABEL: Record<string, string> = {
  gstin: "matched by GSTIN",
  exact: "same name",
  alias: "matched by alias",
  fuzzy: "similar name",
  learned: "used last time",
  choice: "your choice",
  default: "default",
};

function EntriesTable({ voucher }: { voucher: Voucher }) {
  const entries = voucher.accounting?.transaction?.entries ?? [];
  const total = (side: "dr" | "cr") =>
    entries.filter((e) => e.side === side).reduce((sum, e) => sum + Number(e.amount), 0);
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-rule text-left text-xs text-ink-soft">
          <th className="py-1.5 pr-2 font-medium">Ledger</th>
          <th className="w-28 py-1.5 pr-2 text-right font-medium">Debit</th>
          <th className="w-28 py-1.5 text-right font-medium">Credit</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((e, i) => (
          <tr key={i} className="border-b border-rule">
            <td className="py-1.5 pr-2">
              {e.ledger.name}
              {e.ledger.proposed && (
                <span className="ml-1.5 rounded-[3px] bg-amber-tint px-1 text-xs text-amber">
                  new
                </span>
              )}
            </td>
            <td className="py-1.5 pr-2 text-right">{e.side === "dr" ? money(e.amount) : ""}</td>
            <td className="py-1.5 text-right">{e.side === "cr" ? money(e.amount) : ""}</td>
          </tr>
        ))}
        <tr className="font-medium">
          <td className="py-1.5 pr-2">Total</td>
          <td className="py-1.5 pr-2 text-right">{money(total("dr"))}</td>
          <td className="py-1.5 text-right">{money(total("cr"))}</td>
        </tr>
      </tbody>
    </table>
  );
}

const SELECT =
  "h-9 w-full rounded-[3px] border border-rule-strong bg-sheet px-2 text-sm focus:border-ledger focus:outline-none disabled:bg-paper";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Labeled({
  label,
  hint,
  issue,
  confidence,
  children,
}: {
  label: string;
  hint?: string;
  issue?: Issue;
  confidence?: number;
  children: ReactNode;
}) {
  const unsure = confidence !== undefined && confidence < 0.6;
  return (
    <label className="block min-w-0">
      <span className="mb-1 flex items-center gap-1.5 text-xs text-ink-soft">
        {label}
        {unsure && !issue && (
          <span className="text-amber" title="The AI wasn't sure about this value">
            check
          </span>
        )}
      </span>
      <span
        className={
          issue
            ? issue.severity === "error"
              ? "block rounded-[3px] ring-1 ring-red-ink"
              : "block rounded-[3px] ring-1 ring-amber"
            : "block"
        }
      >
        {children}
      </span>
      {hint && <span className="mt-1 block text-xs text-ink-soft">{hint}</span>}
    </label>
  );
}

function TextInput({
  value,
  onChange,
  type = "text",
  numeric = false,
  mono = false,
  strong = false,
  maxLength,
}: {
  value: string;
  onChange: (v: string) => void;
  type?: string;
  numeric?: boolean;
  mono?: boolean;
  strong?: boolean;
  maxLength?: number;
}) {
  return (
    <input
      type={type}
      value={value}
      maxLength={maxLength}
      inputMode={numeric ? "decimal" : undefined}
      onChange={(e) => onChange(e.target.value)}
      className={`h-9 w-full rounded-[3px] border border-rule-strong bg-sheet px-2 text-sm focus:border-ledger focus:outline-none disabled:bg-paper ${
        numeric ? "text-right tabular-nums" : ""
      } ${mono ? "font-mono uppercase" : ""} ${strong ? "font-semibold" : ""}`}
    />
  );
}

function uniq(items: string[]) {
  return [...new Set(items)];
}
