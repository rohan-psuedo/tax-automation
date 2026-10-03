"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { api } from "@/lib/api";

const TABS = [
  { href: "/settings", label: "Tally and AI" },
  { href: "/settings/team", label: "Team" },
  { href: "/settings/backups", label: "Backups" },
  { href: "/settings/activity", label: "Office activity" },
];

// Typed with ReactNode rather than LayoutProps<"/settings">: the generated route types only
// learn about this folder on the next `next dev` or `next build`.
export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  // Subscribed rather than read once, so an administrator who gives up their own admin role
  // on the Team tab sees this area close straight away.
  const me = useQuery({ queryKey: ["me"], queryFn: api.me }).data;

  return (
    <div className="mx-auto max-w-5xl px-6 pt-7">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-ink-soft">
          These apply to the whole office: every company and every person.
        </p>
      </header>

      {me?.role === "admin" ? (
        <>
          <nav aria-label="Settings sections" className="mt-5 flex gap-6 border-b border-rule">
            {TABS.map((tab) => (
              <Link
                key={tab.href}
                href={tab.href}
                aria-current={pathname === tab.href ? "page" : undefined}
                className="-mb-px border-b-2 border-transparent pb-2.5 text-sm text-ink-soft hover:text-ink aria-[current=page]:border-ledger aria-[current=page]:font-medium aria-[current=page]:text-ink"
              >
                {tab.label}
              </Link>
            ))}
          </nav>
          <div className="pb-12">{children}</div>
        </>
      ) : (
        <div className="mt-6 max-w-md border-l-2 border-rule-strong bg-sheet px-3 py-2 text-sm">
          <p>Only an administrator can change settings.</p>
          <p className="mt-1 text-ink-soft">
            To change your own password, open{" "}
            <Link href="/account" className="text-ledger underline-offset-2 hover:underline">
              Your account
            </Link>
            .
          </p>
        </div>
      )}
    </div>
  );
}
