"use client";

import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { type FormEvent, useDeferredValue, useId, useMemo, useState } from "react";
import { Button, ErrorNote, Ident, Input, formatDateTime } from "@/components/ui";
import { api, type Company, type User } from "@/lib/api";

const OK_NOTE = "border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm";

export default function LedgersPage() {
  const { id } = useParams<{ id: string }>();
  const companyId = Number(id);
  const company = useQuery({
    queryKey: ["company", companyId],
    queryFn: () => api.company(companyId),
  });
  if (!company.data) return null;
  return (
    <>
      <Policies company={company.data} />
      <Ledgers companyId={companyId} />
    </>
  );
}

/**
 * After a posting rule changes, the server moves this company's open entries between "ready"
 * and "needs review", so the lists and counts shown on other tabs are out of date.
 */
function refreshEntries(queryClient: QueryClient, companyId: number) {
  for (const key of ["documents", "document-counts", "vouchers", "activity"]) {
    queryClient.invalidateQueries({ queryKey: [key, companyId] });
  }
  // A single document and its entry are cached by document id, which this page doesn't know.
  queryClient.invalidateQueries({ queryKey: ["document"] });
  queryClient.invalidateQueries({ queryKey: ["voucher"] });
}

type Toggle = "always_review" | "auto_post" | "auto_create_ledgers";

function Policies({ company }: { company: Company }) {
  const queryClient = useQueryClient();
  const me = queryClient.getQueryData<User>(["me"]);
  const isAdmin = me?.role === "admin";

  const update = useMutation({
    mutationFn: (changes: Partial<Company>) => api.updateCompany(company.id, changes),
    onSuccess: (updated) => {
      queryClient.setQueryData(["company", company.id], updated);
      refreshEntries(queryClient, company.id);
    },
  });

  const toggle = (key: Toggle, label: string, help: string, note?: string) => (
    <label className="flex items-start gap-3 py-3">
      <input
        type="checkbox"
        className="mt-0.5 size-4 accent-[var(--ledger)]"
        checked={company[key]}
        // Not disabled while saving, which would drop keyboard focus; a change made meanwhile
        // is ignored instead.
        disabled={!isAdmin}
        aria-busy={update.isPending || undefined}
        onChange={(e) => {
          if (!update.isPending) update.mutate({ [key]: e.target.checked });
        }}
      />
      <span>
        <span className="block text-sm">{label}</span>
        <span className="block text-xs text-ink-soft">{help}</span>
        {note && (
          <span className="mt-1 block border-l-2 border-amber pl-2 text-xs text-ink">{note}</span>
        )}
      </span>
    </label>
  );

  return (
    <section aria-labelledby="policies-title" className="mt-6">
      <h2 id="policies-title" className="text-sm font-semibold">
        Posting rules
      </h2>
      <div className="mt-2 divide-y divide-rule border-y border-rule">
        {toggle(
          "always_review",
          "Review every entry before posting",
          "Recommended while your team gets used to the system.",
        )}
        <ReviewLimit company={company} isAdmin={isAdmin} />
        {toggle(
          "auto_post",
          "Post clean entries to Tally automatically",
          "Entries with no issues go to Tally without anyone clicking Post. Has no effect while “Review every entry before posting” is on.",
          company.always_review && company.auto_post
            ? "Not in effect now, because every entry is reviewed first."
            : undefined,
        )}
        {toggle(
          "auto_create_ledgers",
          "Create new party ledgers automatically",
          "When off, a reviewer approves each new ledger before it reaches Tally.",
        )}
      </div>
      {!isAdmin && (
        <p className="mt-2 text-xs text-ink-soft">Only an administrator can change these.</p>
      )}
      {update.error && <ErrorNote>{update.error.message}</ErrorNote>}
    </section>
  );
}

/** Text for the amount box: "100000.00" -> "100000", no limit -> "". */
function amountText(value: string | null) {
  return value === null ? "" : value.replace(/\.00$/, "");
}

function sameAmount(a: string | null, b: string | null) {
  return a === null || b === null ? a === b : Number(a) === Number(b);
}

/** Reads the box the way the API stores it: rupees, up to 12 digits and 2 decimals. */
function parseAmount(raw: string): { value: string | null } | { error: string } {
  const v = raw.replace(/[\s,₹]/g, "");
  if (v === "") return { value: null };
  if (v.startsWith("-"))
    return { error: "The amount can't be negative. Leave it empty for no limit." };
  if (!/^\d+(\.\d+)?$/.test(v)) {
    return { error: "Enter an amount in rupees, like 50000, or leave it empty for no limit." };
  }
  if (!/^\d+(\.\d{1,2})?$/.test(v))
    return { error: "Use at most two digits after the decimal point." };
  if (v.split(".")[0].replace(/^0+(?=\d)/, "").length > 12) {
    return { error: "That amount is too large. Leave it empty for no limit." };
  }
  return { value: v };
}

/** "100000.00" -> "₹1,00,000"; paise are shown only when there are any. */
function rupees(value: string) {
  const n = Number(value);
  const digits = Number.isInteger(n) ? 0 : 2;
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: 2 })}`;
}

function ReviewLimit({ company, isAdmin }: { company: Company; isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const helpId = useId();
  const saveId = useId();
  const saved = company.review_above_amount;
  const [draft, setDraft] = useState(amountText(saved));
  const [baseline, setBaseline] = useState(saved);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // The saved limit changed (saved here, or by someone else): show it.
  if (baseline !== saved) {
    setBaseline(saved);
    setDraft(amountText(saved));
    setProblem(null);
  }

  const update = useMutation({
    mutationFn: (value: string | null) =>
      api.updateCompany(company.id, { review_above_amount: value }),
    onSuccess: (updated) => {
      // The Save button goes away once saved: keep keyboard focus on the box instead.
      if (document.activeElement?.id === saveId) document.getElementById(inputId)?.focus();
      queryClient.setQueryData(["company", company.id], updated);
      refreshEntries(queryClient, company.id);
      setNotice(
        updated.review_above_amount === null
          ? "Saved. There is no limit now."
          : `Saved. Entries above ${rupees(updated.review_above_amount)} now wait for a reviewer.`,
      );
    },
  });

  const parsed = parseAmount(draft);
  const dirty = "error" in parsed || !sameAmount(parsed.value, saved);

  function commit() {
    if (!isAdmin) return;
    if ("error" in parsed) {
      setProblem(parsed.error);
      return;
    }
    setProblem(null);
    // A blur followed by a click on Save would otherwise send the same change twice.
    if (!dirty || update.isPending) return;
    update.mutate(parsed.value);
  }

  const label = "Always review entries above";
  const help = (
    <span id={helpId} className="block text-xs text-ink-soft">
      Entries with a total above this amount wait for a reviewer, even when nothing looks wrong.
      {isAdmin && " Leave it empty for no limit."}
    </span>
  );

  if (!isAdmin) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-3 pl-7">
        <div className="min-w-0 flex-1 basis-64">
          <span className="block text-sm">{label}</span>
          {help}
        </div>
        <span className="text-sm font-medium">{saved === null ? "No limit" : rupees(saved)}</span>
      </div>
    );
  }

  return (
    <div className="py-3 pl-7">
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          commit();
        }}
        className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2"
      >
        <div className="min-w-0 flex-1 basis-64">
          <label htmlFor={inputId} className="block text-sm">
            {label}
          </label>
          {help}
        </div>
        <div className="flex items-center gap-2">
          <div className="relative w-40">
            <span
              aria-hidden
              className="pointer-events-none absolute inset-y-0 left-2.5 flex items-center text-sm text-ink-soft"
            >
              ₹
            </span>
            <Input
              id={inputId}
              inputMode="decimal"
              autoComplete="off"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setProblem(null);
                setNotice(null);
              }}
              onBlur={commit}
              placeholder="No limit"
              aria-describedby={helpId}
              aria-invalid={problem ? true : undefined}
              // Full ink-soft rather than the faded default, so "No limit" is readable.
              className="pl-6 text-right placeholder:text-ink-soft!"
            />
          </div>
          {dirty && (
            <Button
              id={saveId}
              type="submit"
              variant="secondary"
              aria-disabled={update.isPending || undefined}
            >
              {update.isPending ? "Saving…" : "Save"}
            </Button>
          )}
        </div>
      </form>
      {/* Always in the page, so screen readers announce the save. */}
      <div role="status">{notice && <p className={`mt-2 ${OK_NOTE}`}>{notice}</p>}</div>
      {problem && (
        <div className="mt-2">
          <ErrorNote>{problem}</ErrorNote>
        </div>
      )}
      {update.error && (
        <div className="mt-2">
          <ErrorNote>{update.error.message}</ErrorNote>
        </div>
      )}
    </div>
  );
}

function Ledgers({ companyId }: { companyId: number }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState("");
  const q = useDeferredValue(search.trim());

  const ledgers = useQuery({
    queryKey: ["ledgers", companyId, q, group],
    queryFn: () => api.ledgers(companyId, q || undefined, group || undefined),
    placeholderData: (prev) => prev,
  });
  const groups = useQuery({
    queryKey: ["groups", companyId],
    queryFn: () => api.groups(companyId),
  });

  const sync = useMutation({
    mutationFn: () => api.syncLedgers(companyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ledgers", companyId] });
      queryClient.invalidateQueries({ queryKey: ["groups", companyId] });
    },
  });

  const lastSynced = useMemo(() => {
    const times = ledgers.data?.map((l) => l.synced_at) ?? [];
    return times.length ? times.reduce((a, b) => (a > b ? a : b)) : null;
  }, [ledgers.data]);

  const neverSynced = !q && !group && ledgers.data?.length === 0;

  return (
    <section aria-labelledby="ledgers-title" className="mt-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="ledgers-title" className="text-sm font-semibold">
            Ledgers
          </h2>
          <p className="mt-0.5 text-xs text-ink-soft">
            {lastSynced
              ? `Copied from Tally ${formatDateTime(lastSynced)}. Used to match invoices to ledgers.`
              : "Copied from Tally and used to match invoices to ledgers."}
          </p>
        </div>
        <Button variant="secondary" onClick={() => sync.mutate()} disabled={sync.isPending}>
          {sync.isPending ? "Syncing…" : "Sync ledgers from Tally"}
        </Button>
      </div>

      {sync.error && (
        <div className="mt-3">
          <ErrorNote>{sync.error.message}</ErrorNote>
        </div>
      )}
      {sync.data && (
        <p role="status" className="mt-3 border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm">
          Synced {sync.data.ledgers} ledgers in {sync.data.groups} groups: {sync.data.added} new,{" "}
          {sync.data.removed} removed.
        </p>
      )}

      {neverSynced ? (
        <div className="mt-8 max-w-md">
          <p className="font-medium">No ledgers yet</p>
          <p className="mt-1 text-sm leading-relaxed text-ink-soft">
            Sync ledgers from Tally to load this company&apos;s parties, expense heads and tax
            ledgers.
          </p>
        </div>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap gap-2">
            <Input
              type="search"
              placeholder="Search by name or GSTIN"
              aria-label="Search ledgers"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="max-w-xs"
            />
            <select
              aria-label="Filter by group"
              value={group}
              onChange={(e) => setGroup(e.target.value)}
              className="h-9 rounded-[3px] border border-rule-strong bg-sheet px-2 text-sm focus:border-ledger focus:outline-none"
            >
              <option value="">All groups</option>
              {groups.data?.map((g) => (
                <option key={g.name} value={g.name}>
                  {g.name}
                </option>
              ))}
            </select>
          </div>

          <table className="mt-3 w-full text-sm">
            <thead>
              <tr className="border-b border-rule text-left text-xs text-ink-soft">
                <th className="py-2 pr-4 font-medium">Ledger</th>
                <th className="py-2 pr-4 font-medium">Group</th>
                <th className="py-2 pr-4 font-medium">GSTIN</th>
                <th className="py-2 font-medium">State</th>
              </tr>
            </thead>
            <tbody>
              {ledgers.data?.map((l) => (
                <tr key={l.id} className="border-b border-rule align-top">
                  <td className="py-2 pr-4">
                    {l.name}
                    {l.aliases.length > 0 && (
                      <span className="block text-xs text-ink-soft">
                        Also known as {l.aliases.join(", ")}
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-4 text-ink-soft">{l.parent ?? "—"}</td>
                  <td className="py-2 pr-4">{l.gstin ? <Ident>{l.gstin}</Ident> : "—"}</td>
                  <td className="py-2 text-ink-soft">{l.state ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {ledgers.data?.length === 0 && (
            <p className="mt-4 text-sm text-ink-soft">No ledgers match this search.</p>
          )}
          {ledgers.data && ledgers.data.length > 0 && (
            <p className="mt-3 text-xs text-ink-soft">{ledgers.data.length} ledgers</p>
          )}
        </>
      )}
    </section>
  );
}
