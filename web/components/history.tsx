"use client";

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";
import { Button, ErrorNote, Ident, formatDateTime } from "@/components/ui";
import { type FieldChange, type HistoryItem, type PostingRecord, activityApi } from "@/lib/api";

/** The life of one voucher, oldest first: reads, edits, postings and the Tally exchange. */
export function VoucherHistoryTimeline({
  voucherId,
  updatedAt,
}: {
  voucherId: number;
  /** The voucher's updated_at. Every save, post or reject changes it, and so refetches. */
  updatedAt: string;
}) {
  const history = useQuery({
    queryKey: ["voucher-history", voucherId, updatedAt],
    queryFn: () => activityApi.voucherHistory(voucherId),
    // Keep showing the old timeline while the new one loads, but only for the same voucher.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === voucherId ? previous : undefined,
  });

  if (history.isPending) {
    return (
      <p role="status" className="text-sm text-ink-soft">
        Loading history…
      </p>
    );
  }
  if (!history.data) {
    return (
      <div className="space-y-3">
        <ErrorNote>Couldn&apos;t load the history: {history.error?.message}</ErrorNote>
        <Button variant="secondary" onClick={() => history.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const items = history.data.items;
  if (items.length === 0) {
    return (
      <p className="text-sm text-ink-soft">
        Nothing recorded yet. Reads, edits and postings of this entry will be listed here.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {history.isRefetchError && (
        <ErrorNote>
          Couldn&apos;t refresh the history, so recent changes may be missing:{" "}
          {history.error?.message}
        </ErrorNote>
      )}
      <ol aria-label="History, oldest first" className="text-sm">
        {items.map((item, i) => (
          <TimelineItem key={`${item.at}-${i}`} item={item} last={i === items.length - 1} />
        ))}
      </ol>
    </div>
  );
}

function TimelineItem({ item, last }: { item: HistoryItem; last: boolean }) {
  const failed = /fail/.test(item.action) || item.posting?.success === false;
  const posted = item.posting?.success === true;
  const marker = failed
    ? "border-red-ink bg-red-ink"
    : posted
      ? "border-ledger bg-ledger"
      : "border-rule-strong bg-sheet";

  return (
    <li className="relative pb-5 pl-6 last:pb-0">
      {!last && (
        <span
          aria-hidden
          className="absolute -bottom-2.5 left-[4.5px] top-2.5 w-px bg-rule-strong"
        />
      )}
      <span
        aria-hidden
        className={`absolute left-0 top-[5px] size-2.5 rounded-full border-2 ${marker}`}
      />
      <p className="flex flex-wrap gap-x-2 text-xs text-ink-soft">
        <time dateTime={item.at}>{formatDateTime(item.at)}</time>
        <span>{item.actor_name ?? "The system"}</span>
      </p>
      <p className={`mt-0.5 break-words ${failed ? "text-red-ink" : ""}`}>{item.summary}</p>
      {item.changes.length > 0 && <Changes changes={item.changes} />}
      {item.posting && <TallyExchange record={item.posting} />}
    </li>
  );
}

function Changes({ changes }: { changes: FieldChange[] }) {
  return (
    <table className="mt-2 w-full table-fixed text-xs">
      <caption className="sr-only">Fields changed</caption>
      <thead>
        <tr className="border-b border-rule text-left text-ink-soft">
          <th scope="col" className="w-[34%] py-1 pr-2 font-medium">
            Field
          </th>
          <th scope="col" className="py-1 pr-2 font-medium">
            Before
          </th>
          <th scope="col" className="py-1 font-medium">
            After
          </th>
        </tr>
      </thead>
      <tbody>
        {changes.map((change) => (
          <tr key={change.field} className="border-b border-rule align-top">
            <th scope="row" className="break-words py-1 pr-2 text-left font-normal text-ink-soft">
              {change.label}
            </th>
            <td className="break-words py-1 pr-2">
              <Value value={change.before} />
            </td>
            <td className="break-words py-1">
              <Value value={change.after} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Plain text for a changed value, or null when it was empty. Numbers are shown as given. */
function displayValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value.trim() === "" ? null : value;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  const parts = Array.isArray(value)
    ? value.map(displayValue)
    : typeof value === "object"
      ? Object.entries(value).map(([key, v]) => {
          const shown = displayValue(v);
          return shown === null ? null : `${key}: ${shown}`;
        })
      : [String(value)];
  const kept = parts.filter((p): p is string => p !== null);
  return kept.length ? kept.join(", ") : null;
}

function Value({ value }: { value: unknown }) {
  const shown = displayValue(value);
  return shown === null ? <span className="text-ink-soft">empty</span> : <>{shown}</>;
}

function TallyExchange({ record }: { record: PostingRecord }) {
  return (
    <details className="mt-2 border border-rule bg-sheet">
      <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
        Tally request and response
        <span
          className={`ml-2 inline-block rounded-[3px] px-1.5 py-0.5 text-xs font-medium ${
            record.success ? "bg-ledger-tint text-ledger" : "bg-red-tint text-red-ink"
          }`}
        >
          {record.success ? "Accepted" : "Not accepted"}
        </span>
      </summary>
      <div className="space-y-3 border-t border-rule px-3 py-3">
        <p className="break-words text-xs text-ink-soft">
          {record.kind === "ledger" ? "New ledger" : "Voucher"} <Ident>{record.reference}</Ident>,
          sent {formatDateTime(record.created_at)}
        </p>
        {record.success ? (
          <p className="border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-xs">
            Tally accepted it.
          </p>
        ) : (
          <p className="break-words border-l-2 border-red-ink bg-red-tint px-3 py-2 text-xs text-red-ink">
            {record.error ? `Tally didn't accept it: ${record.error}` : "Tally didn't accept it."}
          </p>
        )}
        <XmlBlock label="Request sent to Tally" xml={record.request_payload} />
        {record.response_payload ? (
          <XmlBlock label="Response from Tally" xml={record.response_payload} />
        ) : (
          <p className="text-xs text-ink-soft">
            No response came back. Tally couldn&apos;t be reached.
          </p>
        )}
      </div>
    </details>
  );
}

function XmlBlock({ label, xml }: { label: string; xml: string }) {
  const labelId = useId();
  return (
    <div>
      <p id={labelId} className="mb-1 text-xs font-medium">
        {label}
      </p>
      {/* Focusable so it can be scrolled with the keyboard. A plain <pre> can't carry a name,
          so it is a region named by the caption above. */}
      <pre
        role="region"
        aria-labelledby={labelId}
        tabIndex={0}
        className="max-h-72 overflow-auto whitespace-pre border border-rule bg-paper px-3 py-2 font-mono text-xs leading-relaxed text-ink"
      >
        {prettyXml(xml)}
      </pre>
    </div>
  );
}

/** Indents XML that arrives on a single line, for reading only. Anything already broken into
 * lines, or that isn't XML, is shown exactly as stored. */
export function prettyXml(xml: string): string {
  const text = xml.trim();
  if (!text.startsWith("<") || text.includes("\n")) return xml;
  let depth = 0;
  return text
    .replace(/>\s*</g, ">\n<")
    .split("\n")
    .map((line) => {
      if (line.startsWith("</")) depth = Math.max(0, depth - 1);
      const indented = "  ".repeat(depth) + line;
      // A lone opening tag, like <ENVELOPE> or <LEDGER NAME="x">, opens a level.
      if (/^<[^!?/][^>]*>$/.test(line) && !line.endsWith("/>")) depth += 1;
      return indented;
    })
    .join("\n");
}
