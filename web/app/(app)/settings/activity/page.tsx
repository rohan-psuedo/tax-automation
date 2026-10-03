"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ActivityList, SegmentedControl } from "@/components/activity";
import { Button, ErrorNote } from "@/components/ui";
import { activityApi } from "@/lib/api";

type FilterKey = "all" | "team" | "settings" | "backups";

const FILTERS: {
  key: FilterKey;
  label: string;
  query: { entity_type?: string; action_prefix?: string };
}[] = [
  { key: "all", label: "All", query: {} },
  { key: "team", label: "Team and sign-ins", query: { entity_type: "user" } },
  { key: "settings", label: "Settings", query: { action_prefix: "settings" } },
  { key: "backups", label: "Backups", query: { action_prefix: "backup" } },
];

// Office events have no company and so never link to a document; ActivityList only uses the
// company id to build document links.
const NO_COMPANY = 0;

export default function OfficeActivityPage() {
  const [filterKey, setFilterKey] = useState<FilterKey>("all");
  const filter = FILTERS.find((f) => f.key === filterKey) ?? FILTERS[0];
  const [announcement, setAnnouncement] = useState("");

  const activity = useInfiniteQuery({
    queryKey: ["office-activity", filter.key],
    queryFn: ({ pageParam }) => activityApi.office({ ...filter.query, before_id: pageParam }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => page.next_before_id ?? undefined,
    staleTime: 0,
  });
  const items = activity.data?.pages.flatMap((page) => page.items) ?? [];

  const showOlder = async () => {
    if (activity.isFetchingNextPage) return;
    const shown = items.length;
    const result = await activity.fetchNextPage();
    if (result.isError) return;
    const total = result.data?.pages.reduce((sum, page) => sum + page.items.length, 0) ?? shown;
    setAnnouncement(`Loaded ${total - shown} older events. Showing ${total} in all.`);
  };

  return (
    <div className="pt-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Office activity</h2>
          <p className="mt-0.5 text-xs text-ink-soft">
            Sign-ins, team changes, settings and backups, newest first. Each company&apos;s own work
            is on its Activity tab.
          </p>
        </div>
        <SegmentedControl
          label="Show"
          options={FILTERS}
          value={filter.key}
          onChange={(key) => {
            setAnnouncement("");
            setFilterKey(key);
          }}
        />
      </div>

      <div className="mt-5">
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
          <p className="text-sm text-ink-soft">Nothing recorded here yet.</p>
        ) : (
          <>
            <ActivityList
              items={items}
              companyId={NO_COMPANY}
              now={new Date(activity.dataUpdatedAt)}
            />
            {activity.hasNextPage && (
              <div className="mt-6">
                <Button
                  variant="secondary"
                  onClick={showOlder}
                  aria-disabled={activity.isFetchingNextPage}
                >
                  {activity.isFetchingNextPage ? "Loading…" : "Show older"}
                </Button>
              </div>
            )}
          </>
        )}
        <p role="status" className="sr-only">
          {announcement}
        </p>
      </div>
    </div>
  );
}
