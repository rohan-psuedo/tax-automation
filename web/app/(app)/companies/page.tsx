"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { Button, ErrorNote, Field, Ident, Input } from "@/components/ui";
import { api, type User } from "@/lib/api";

export default function CompaniesPage() {
  const queryClient = useQueryClient();
  const me = queryClient.getQueryData<User>(["me"]);
  const companies = useQuery({ queryKey: ["companies"], queryFn: api.companies });
  const [adding, setAdding] = useState(false);

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="flex items-end justify-between gap-4 border-b border-rule pb-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Companies</h1>
          <p className="mt-1 text-sm text-ink-soft">
            Each company here is linked to one company in Tally.
          </p>
        </div>
        {me?.role === "admin" && !adding && (
          <Button onClick={() => setAdding(true)}>Add company</Button>
        )}
      </header>

      {adding && <AddCompany onDone={() => setAdding(false)} />}

      {companies.error && (
        <div className="mt-6">
          <ErrorNote>{companies.error.message}</ErrorNote>
        </div>
      )}

      {companies.data && companies.data.length === 0 && !adding && (
        <div className="mt-10 max-w-md">
          <p className="font-medium">No companies yet</p>
          <p className="mt-1 text-sm leading-relaxed text-ink-soft">
            Add the first client company you keep books for. You will pick it from the companies
            open in Tally, so make sure Tally is running.
          </p>
        </div>
      )}

      {companies.data && companies.data.length > 0 && (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="border-b border-rule text-left text-xs text-ink-soft">
              <th className="py-2.5 pr-4 font-medium">Company</th>
              <th className="py-2.5 pr-4 font-medium">Name in Tally</th>
              <th className="py-2.5 pr-4 font-medium">GSTIN</th>
              <th className="py-2.5 font-medium">State</th>
            </tr>
          </thead>
          <tbody>
            {companies.data.map((c) => (
              <tr key={c.id} className="border-b border-rule hover:bg-sheet">
                <td className="py-3 pr-4">
                  <Link
                    href={`/companies/${c.id}`}
                    className="font-medium text-ink hover:text-ledger hover:underline"
                  >
                    {c.name}
                  </Link>
                </td>
                <td className="py-3 pr-4 text-ink-soft">{c.external_company_name}</td>
                <td className="py-3 pr-4">{c.gstin ? <Ident>{c.gstin}</Ident> : "—"}</td>
                <td className="py-3 text-ink-soft">{c.state ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AddCompany({ onDone }: { onDone: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const tallyCompanies = useQuery({ queryKey: ["tally-companies"], queryFn: api.tallyCompanies });

  const [tallyName, setTallyName] = useState("");
  const [name, setName] = useState("");
  const [gstin, setGstin] = useState("");
  const [state, setState] = useState("");

  function pickTallyCompany(value: string) {
    setTallyName(value);
    const picked = tallyCompanies.data?.find((c) => c.name === value);
    if (picked) {
      setName((current) => current || picked.name);
      setGstin(picked.gstin ?? "");
      setState(picked.state ?? "");
    }
  }

  const create = useMutation({
    mutationFn: () =>
      api.createCompany({
        name: name.trim(),
        external_company_name: tallyName.trim(),
        gstin: gstin.trim().toUpperCase() || null,
        state: state.trim() || null,
      }),
    onSuccess: (company) => {
      queryClient.invalidateQueries({ queryKey: ["companies"] });
      onDone();
      router.push(`/companies/${company.id}`);
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }

  const tallyDown = Boolean(tallyCompanies.error);

  return (
    <form
      onSubmit={onSubmit}
      className="mt-6 border border-rule bg-sheet p-5"
      aria-labelledby="add-company-title"
    >
      <h2 id="add-company-title" className="font-semibold">
        Add a company
      </h2>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <Field
          label="Company in Tally"
          hint={
            tallyDown
              ? "Tally isn't reachable, so type the company name exactly as it appears in Tally."
              : "Only companies currently open in Tally are listed."
          }
        >
          {tallyDown ? (
            <Input value={tallyName} onChange={(e) => setTallyName(e.target.value)} required />
          ) : (
            <select
              value={tallyName}
              onChange={(e) => pickTallyCompany(e.target.value)}
              required
              className="h-9 w-full rounded-[3px] border border-rule-strong bg-sheet px-2 text-sm focus:border-ledger focus:outline-none"
            >
              <option value="" disabled>
                {tallyCompanies.isPending ? "Loading from Tally…" : "Choose a company"}
              </option>
              {tallyCompanies.data?.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Display name" hint="How your team refers to this client.">
          <Input value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Field label="GSTIN" hint="Used to tell purchases from sales on invoices.">
          <Input
            value={gstin}
            onChange={(e) => setGstin(e.target.value)}
            maxLength={15}
            pattern="[0-9]{2}[0-9A-Za-z]{13}"
            className="font-mono uppercase"
          />
        </Field>
        <Field label="State">
          <Input value={state} onChange={(e) => setState(e.target.value)} />
        </Field>
      </div>
      {create.error && (
        <div className="mt-4">
          <ErrorNote>{create.error.message}</ErrorNote>
        </div>
      )}
      <div className="mt-5 flex gap-2">
        <Button type="submit" disabled={create.isPending}>
          Save company
        </Button>
        <Button type="button" variant="quiet" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
