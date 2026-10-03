import Link from "next/link";
import type { ActivityItem } from "@/lib/api";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Local calendar day, e.g. "2026-09-28", for grouping events by the day they happened. */
function dayKey(date: Date) {
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${mm}-${dd}`;
}

/** "Today", "Yesterday", or a date like "28 Sep 2026". Built by hand because the en-IN locale
 * writes September as "Sept" in some browsers. */
export function dayHeading(date: Date, now: Date) {
  const key = dayKey(date);
  if (key === dayKey(now)) return "Today";
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (key === dayKey(yesterday)) return "Yesterday";
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
}

export function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}

/** Actions that record something going wrong: parse_failed, post_failed, create_failed, ... */
export const isFailure = (action: string) => /fail/.test(action);

type DayGroup = { key: string; date: Date; items: ActivityItem[] };

/** Splits a newest-first feed into consecutive days, keeping the order. */
function groupByDay(items: ActivityItem[]): DayGroup[] {
  const groups: DayGroup[] = [];
  for (const item of items) {
    const date = new Date(item.created_at);
    const key = dayKey(date);
    const last = groups.at(-1);
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, date, items: [item] });
  }
  return groups;
}

export function SegmentedControl<K extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly { key: K; label: string }[];
  value: K;
  onChange: (key: K) => void;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="inline-flex max-w-full flex-wrap gap-0.5 rounded-[3px] border border-rule-strong bg-sheet p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.key}
          type="button"
          aria-pressed={option.key === value}
          onClick={() => onChange(option.key)}
          className="whitespace-nowrap rounded-[2px] px-2.5 py-1 text-sm text-ink-soft hover:bg-paper hover:text-ink aria-pressed:bg-ledger-tint aria-pressed:font-medium aria-pressed:text-ledger"
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** A newest-first feed under day headings. `now` decides which day is "Today". */
export function ActivityList({
  items,
  companyId,
  now,
}: {
  items: ActivityItem[];
  companyId: number;
  now: Date;
}) {
  return (
    <div className="space-y-6">
      {/* Plain divs, not named sections: a named section is a landmark, and one landmark per
          day would bury the page's real ones. The h3 headings give the structure. */}
      {groupByDay(items).map((group) => (
        <div key={group.key}>
          <h3 className="border-b border-rule-strong pb-1.5 text-sm font-semibold">
            {dayHeading(group.date, now)}
          </h3>
          <ol>
            {group.items.map((item) => (
              <ActivityRow key={item.id} item={item} companyId={companyId} />
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}

function ActivityRow({ item, companyId }: { item: ActivityItem; companyId: number }) {
  // Once a document is deleted the feed sends document_id null for all of its events, the
  // deletion included, so none of them links to a page that no longer exists. The action check
  // is a fallback in case a deletion event ever arrives with an id.
  const showLink = item.document_id !== null && item.action !== "document.deleted";
  const summaryId = `activity-${item.id}`;
  return (
    <li className="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-x-3 gap-y-0.5 border-b border-rule py-2 text-sm sm:grid-cols-[4.75rem_minmax(0,1fr)_auto]">
      <time dateTime={item.created_at} className="text-ink-soft">
        {formatTime(item.created_at)}
      </time>
      <p id={summaryId} className="break-words">
        {item.summary}
        {isFailure(item.action) && (
          <span className="ml-2 inline-block rounded-[3px] bg-red-tint px-1.5 py-0.5 align-[1px] text-xs font-medium text-red-ink">
            Failed
          </span>
        )}
      </p>
      {showLink && (
        <Link
          href={`/companies/${companyId}/documents/${item.document_id}`}
          aria-describedby={summaryId}
          className="col-start-2 justify-self-start whitespace-nowrap text-ledger underline-offset-2 hover:underline sm:col-start-3"
        >
          Open document
        </Link>
      )}
    </li>
  );
}
