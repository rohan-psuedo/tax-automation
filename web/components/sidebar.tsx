"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import { api, type User } from "@/lib/api";

const ITEM_LINK =
  "block rounded-[3px] px-3 py-1.5 text-sm text-ink-soft hover:bg-paper hover:text-ink aria-[current=page]:bg-ledger-tint aria-[current=page]:font-medium aria-[current=page]:text-ledger";

/**
 * The main navigation. From the md breakpoint up it is a fixed column; below it, a slim rail
 * with a Menu button opens the same links as a drawer, so every page stays reachable when the
 * window is narrow (for example snapped next to Tally).
 */
export function Sidebar() {
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const me = queryClient.getQueryData<User>(["me"]);
  const companies = useQuery({ queryKey: ["companies"], queryFn: api.companies });
  const isUnder = (base: string) => pathname === base || pathname.startsWith(`${base}/`);

  const navId = useId();
  const nav = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const firstLink = useRef<HTMLAnchorElement>(null);
  const [open, setOpen] = useState(false);
  const [openedOn, setOpenedOn] = useState(pathname);

  // Following a link in the drawer closes it.
  if (openedOn !== pathname) {
    setOpenedOn(pathname);
    setOpen(false);
  }

  // An open drawer takes focus, and closes on a click or tap anywhere outside it.
  useEffect(() => {
    if (!open) return;
    firstLink.current?.focus();
    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (!nav.current?.contains(target) && !menuButton.current?.contains(target)) {
        setOpen(false);
      }
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  function close(returnFocus: boolean) {
    setOpen(false);
    if (returnFocus) menuButton.current?.focus();
  }

  return (
    <>
      <div className="w-14 shrink-0 border-r border-rule bg-sheet pt-3 md:hidden">
        <button
          ref={menuButton}
          type="button"
          aria-expanded={open}
          aria-controls={navId}
          onClick={() => setOpen(!open)}
          className="mx-auto block rounded-[3px] px-2 py-1.5 text-xs font-medium text-ink hover:bg-paper aria-expanded:bg-ledger-tint aria-expanded:text-ledger"
        >
          Menu
        </button>
      </div>

      <nav
        ref={nav}
        id={navId}
        aria-label="Main"
        onKeyDown={(e) => {
          if (open && e.key === "Escape") close(true);
        }}
        onClick={(e) => {
          // Also covers a link to the page already shown, where the path does not change.
          if (open && (e.target as HTMLElement).closest("a")) close(false);
        }}
        onBlur={(e) => {
          // Tabbing out of the drawer closes it; clicks outside are handled above.
          const to = e.relatedTarget as Node | null;
          if (open && to && !e.currentTarget.contains(to) && to !== menuButton.current) {
            close(false);
          }
        }}
        className={`${
          open ? "fixed bottom-8 left-14 top-0 z-30 flex shadow-lg" : "hidden"
        } w-60 shrink-0 flex-col border-r border-rule bg-sheet md:static md:z-auto md:flex md:shadow-none`}
      >
        <Link
          ref={firstLink}
          href="/companies"
          className="px-5 pb-4 pt-5 text-sm font-semibold text-ledger"
        >
          Tax Automaton
        </Link>

        <Link
          href="/companies"
          aria-current={pathname === "/companies" ? "page" : undefined}
          className="mx-2 rounded-[3px] px-3 py-2 text-sm font-medium text-ink hover:bg-paper aria-[current=page]:bg-ledger-tint aria-[current=page]:text-ledger"
        >
          All companies
        </Link>

        <div className="mt-5 px-5 pb-2 text-xs text-ink-soft">Books you keep</div>
        <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
          {companies.data?.map((c) => {
            const href = `/companies/${c.id}`;
            return (
              <li key={c.id}>
                <Link
                  href={href}
                  aria-current={isUnder(href) ? "page" : undefined}
                  className={`${ITEM_LINK} truncate`}
                >
                  {c.name}
                </Link>
              </li>
            );
          })}
          {companies.data?.length === 0 && (
            <li className="px-3 py-1.5 text-sm text-ink-soft">No companies yet.</li>
          )}
        </ul>

        <ul className="space-y-0.5 border-t border-rule px-2 py-3">
          {me?.role === "admin" && (
            <li>
              <Link
                href="/settings"
                aria-current={isUnder("/settings") ? "page" : undefined}
                className={ITEM_LINK}
              >
                Settings
              </Link>
            </li>
          )}
          <li>
            <Link
              href="/account"
              aria-current={isUnder("/account") ? "page" : undefined}
              className={ITEM_LINK}
            >
              Your account
            </Link>
          </li>
        </ul>
      </nav>
    </>
  );
}
