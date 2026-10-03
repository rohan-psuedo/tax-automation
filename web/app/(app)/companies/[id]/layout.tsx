"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { ErrorNote, Ident } from "@/components/ui";
import { api } from "@/lib/api";

export default function CompanyLayout({ children }: LayoutProps<"/companies/[id]">) {
  const { id } = useParams<{ id: string }>();
  const pathname = usePathname();
  const companyId = Number(id);
  const company = useQuery({
    queryKey: ["company", companyId],
    queryFn: () => api.company(companyId),
  });

  if (company.error) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-8">
        <ErrorNote>{company.error.message}</ErrorNote>
      </div>
    );
  }
  if (!company.data) return null;

  const base = `/companies/${companyId}`;
  const tabs = [
    {
      href: base,
      label: "Documents",
      active: pathname === base || pathname.startsWith(`${base}/documents`),
    },
    {
      href: `${base}/ledgers`,
      label: "Ledgers & rules",
      active: pathname.startsWith(`${base}/ledgers`),
    },
    {
      href: `${base}/activity`,
      label: "Activity",
      active: pathname.startsWith(`${base}/activity`),
    },
  ];

  return (
    <div className="mx-auto max-w-6xl px-6 pt-7">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">{company.data.name}</h1>
        <p className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-ink-soft">
          <span>Tally: {company.data.external_company_name}</span>
          {company.data.gstin && (
            <span>
              GSTIN <Ident>{company.data.gstin}</Ident>
            </span>
          )}
          {company.data.state && <span>{company.data.state}</span>}
        </p>
      </header>
      <nav aria-label="Company sections" className="mt-5 flex gap-6 border-b border-rule">
        {tabs.map((tab) => (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={tab.active ? "page" : undefined}
            className="-mb-px border-b-2 border-transparent pb-2.5 text-sm text-ink-soft hover:text-ink aria-[current=page]:border-ledger aria-[current=page]:font-medium aria-[current=page]:text-ink"
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      <div className="pb-12">{children}</div>
    </div>
  );
}
