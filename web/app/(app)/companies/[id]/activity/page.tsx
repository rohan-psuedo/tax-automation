"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type ReactNode, useRef, useState } from "react";
import { ActivityList, SegmentedControl } from "@/components/activity";
import { Button, ErrorNote } from "@/components/ui";
import { activityApi } from "@/lib/api";

type FilterKey = "all" | "documents" | "entries" | "ledgers" | "company";

type Filter = {
  key: FilterKey;
  label: string;
  query: { entity_type?: string; action_prefix?: string };
  empty: (base: string) => ReactNode;
};

const LINK = "text-ledger underline-offset-2 hover:underline";

// The feed takes one entity_type or one action_prefix per request, so each filter is one of them.
// - Ledgers uses the "ledger" prefix: it catches ledger.created and ledger.create_failed (new
//   ledgers made in Tally while posting) and ledgers.synced, which is recorded against the company.
// - Company uses the "company" prefix rather than entity_type "company", so ledger syncs stay
//   under Ledgers and this filter shows only company.created and company.updated (details and
//   posting rules). Team events (user.created, user.login, ...) are office-wide and recorded
//   without a company, so a company's feed never has any; they are not offered here, and the
//   label says "Company" rather than promising team activity it can't show.
const FILTERS: Filter[] = [
  {
    key: "all",
    label: "All",
    query: {},
    empty: (base) => (
      <>
        Nothing has happened in this company yet.{" "}
        <Link href={base} className={LINK}>
          Upload invoices
        </Link>{" "}
        and every step, from reading to posting in Tally, is recorded here.
      </>
    ),
  },
  {
    key: "documents",
    label: "Documents",
    query: { entity_type: "document" },
    empty: (base) => (
      <>
        No documents yet.{" "}
        <Link href={base} className={LINK}>
          Upload invoices
        </Link>{" "}
        to see uploads, reads and deletions here.
      </>
    ),
  },
  {
    key: "entries",
    label: "Entries",
    query: { entity_type: "voucher" },
    empty: (base) => (
      <>
        No entries yet. An entry is made each time the AI reads an invoice, so{" "}
        <Link href={base} className={LINK}>
          upload invoices
        </Link>{" "}
        to start.
      </>
    ),
  },
  {
    key: "ledgers",
    label: "Ledgers",
    query: { action_prefix: "ledger" },
    empty: (base) => (
      <>
        No ledger activity yet. Sync ledgers from Tally on the{" "}
        <Link href={`${base}/ledgers`} className={LINK}>
          Ledgers &amp; rules
        </Link>{" "}
        tab.
      </>
    ),
  },
  {
    key: "company",
    label: "Company",
    query: { action_prefix: "company" },
    empty: (base) => (
      <>
        No changes to this company yet. Posting rules are on the{" "}
        <Link href={`${base}/ledgers`} className={LINK}>
          Ledgers &amp; rules
        </Link>{" "}
        tab.
      </>
    ),
  },
];

export default function ActivityPage() {
  const { id } = useParams<{ id: string }>();
  const companyId = Number(id);
  const base = `/companies/${companyId}`;
  const [filterKey, setFilterKey] = useState<FilterKey>("all");
  const filter = FILTERS.find((f) => f.key === filterKey) ?? FILTERS[0];
  // Read out to screen reader users after "Show older", since new rows appear silently.
  const [announcement, setAnnouncement] = useState("");
  // Set while older events load. If that was the last page, the button disappears, so focus
  // moves to the note that replaces it instead of falling back to the top of the page.
  const focusEndNote = useRef(false);
  const currentFilter = useRef(filterKey);

  const activity = useInfiniteQuery({
    queryKey: ["activity", companyId, filter.key],
    queryFn: ({ pageParam }) =>
      activityApi.company(companyId, { ...filter.query, before_id: pageParam }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => page.next_before_id ?? undefined,
    // A feed should show what just happened, so refetch whenever the tab is opened.
    staleTime: 0,
  });
  const items = activity.data?.pages.flatMap((page) => page.items) ?? [];

  const changeFilter = (key: FilterKey) => {
    currentFilter.current = key;
    focusEndNote.current = false;
    setAnnouncement("");
    setFilterKey(key);
  };

  const showOlder = async () => {
    // aria-disabled rather than disabled while loading, so the button keeps keyboard focus.
    if (activity.isFetchingNextPage) return;
    const key = filter.key;
    const shown = items.length;
    focusEndNote.current = true;
    const result = await activity.fetchNextPage();
    if (result.isError || result.hasNextPage) focusEndNote.current = false;
    if (result.isError || currentFilter.current !== key) return;
    const total = result.data?.pages.reduce((sum, page) => sum + page.items.length, 0) ?? shown;
    const added = total - shown;
    setAnnouncement(
      `Loaded ${added} older ${added === 1 ? "event" : "events"}. Showing ${total} in all.`,
    );
  };

  const endNoteRef = (node: HTMLParagraphElement | null) => {
    if (node && focusEndNote.current) {
      focusEndNote.current = false;
      node.focus();
    }
  };

  return (
    <div className="pt-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Activity</h2>
          <p className="mt-0.5 text-xs text-ink-soft">
            What your team and the system did in this company&apos;s books, newest first.
          </p>
        </div>
        <SegmentedControl
          label="Show activity for"
          options={FILTERS}
          value={filter.key}
          onChange={changeFilter}
        />
      </div>

      <div className="mt-5">
        {activity.isRefetchError && (
          <div className="mb-4">
            <ErrorNote>
              Couldn&apos;t refresh the activity, so newer events may be missing:{" "}
              {activity.error?.message}
            </ErrorNote>
          </div>
        )}
        {activity.isPending ? (
          <p role="status" className="text-sm text-ink-soft">
            Loading activity…
          </p>
        ) : !activity.data ? (
          <div className="space-y-3">
            <ErrorNote>Couldn&apos;t load the activity: {activity.error?.message}</ErrorNote>
            <Button variant="secondary" onClick={() => activity.refetch()}>
              Try again
            </Button>
          </div>
        ) : items.length === 0 ? (
          <p className="max-w-lg text-sm leading-relaxed text-ink-soft">{filter.empty(base)}</p>
        ) : (
          <>
            {/* Headings are relative to when the feed was fetched, which keeps render pure. */}
            <ActivityList
              items={items}
              companyId={companyId}
              now={new Date(activity.dataUpdatedAt)}
            />
            {activity.isFetchNextPageError && (
              <div className="mt-4">
                <ErrorNote>Couldn&apos;t load older activity: {activity.error?.message}</ErrorNote>
              </div>
            )}
            {activity.hasNextPage ? (
              <Button
                variant="secondary"
                className="mt-4 aria-disabled:cursor-not-allowed aria-disabled:text-ink-soft"
                onClick={showOlder}
                aria-disabled={activity.isFetchingNextPage}
              >
                {activity.isFetchingNextPage ? "Loading…" : "Show older"}
              </Button>
            ) : (
              activity.data.pages.length > 1 && (
                <p ref={endNoteRef} tabIndex={-1} className="mt-4 text-sm text-ink-soft">
                  That&apos;s everything. There&apos;s no older activity.
                </p>
              )
            )}
          </>
        )}
      </div>
      <p role="status" className="sr-only">
        {announcement}
      </p>
    </div>
  );
}
