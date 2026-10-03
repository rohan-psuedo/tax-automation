"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type DragEvent, useRef, useState } from "react";
import { StatusBadge, formatBytes, isInProgress, kindLabel } from "@/components/documents";
import { Button, ErrorNote, formatDateTime } from "@/components/ui";
import { VoucherBadge } from "@/components/voucher-panel";
import {
  type DocumentSummary,
  MAX_UPLOAD_MB,
  type UploadResult,
  type User,
  type VoucherStatus,
  documentsApi,
  vouchersApi,
} from "@/lib/api";

const ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,.docx,.xlsx,.csv";

type Filter = { key: string; label: string; match: (d: DocumentSummary) => boolean };

const voucherIs =
  (...statuses: VoucherStatus[]) =>
  (d: DocumentSummary) =>
    d.voucher_status !== null && statuses.includes(d.voucher_status);

const busy = (d: DocumentSummary) =>
  isInProgress(d) || voucherIs("pending", "extracting", "posting")(d);

const FILTERS: Filter[] = [
  { key: "all", label: "All", match: () => true },
  { key: "progress", label: "In progress", match: busy },
  { key: "review", label: "Needs review", match: voucherIs("needs_review", "post_failed") },
  { key: "ready", label: "Ready to post", match: voucherIs("ready") },
  { key: "posted", label: "In Tally", match: voucherIs("posted") },
  { key: "failed", label: "Couldn't read", match: (d) => d.status === "failed" },
  { key: "duplicate", label: "Duplicates", match: (d) => d.status === "duplicate" },
];

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
});

export default function DocumentsPage() {
  const { id } = useParams<{ id: string }>();
  const companyId = Number(id);
  const queryClient = useQueryClient();
  const me = queryClient.getQueryData<User>(["me"]);
  const canPost = me?.role === "admin" || me?.role === "reviewer";
  const [filterKey, setFilterKey] = useState("all");
  const filter = FILTERS.find((f) => f.key === filterKey) ?? FILTERS[0];

  const docs = useQuery({
    queryKey: ["documents", companyId],
    queryFn: () => documentsApi.list(companyId),
    // Keep polling while files are being read or vouchers are being prepared or posted.
    refetchInterval: (query) => (query.state.data?.some(busy) ? 2000 : false),
  });
  const all = docs.data ?? [];
  const shown = all.filter(filter.match);
  const readyCount = all.filter(voucherIs("ready")).length;

  const postReady = useMutation({
    mutationFn: () => vouchersApi.postReady(companyId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["documents", companyId] }),
  });

  return (
    <div className="pt-6">
      <UploadZone companyId={companyId} />

      <div className="mt-8 flex flex-wrap items-center gap-1">
        <div className="flex flex-wrap gap-1" role="tablist" aria-label="Filter documents">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              role="tab"
              aria-selected={f.key === filter.key}
              onClick={() => setFilterKey(f.key)}
              className="rounded-[3px] px-2.5 py-1.5 text-sm text-ink-soft hover:bg-sheet hover:text-ink aria-selected:bg-sheet aria-selected:font-medium aria-selected:text-ink aria-selected:shadow-[inset_0_0_0_1px_var(--rule-strong)]"
            >
              {f.label}
              {docs.data && (
                <span className="ml-1.5 text-ink-soft">{all.filter(f.match).length}</span>
              )}
            </button>
          ))}
        </div>
        {canPost && readyCount > 0 && (
          <Button
            className="ml-auto"
            onClick={() => postReady.mutate()}
            disabled={postReady.isPending}
          >
            {postReady.isPending
              ? "Posting…"
              : `Post ${readyCount} ready ${readyCount === 1 ? "entry" : "entries"} to Tally`}
          </Button>
        )}
      </div>

      {postReady.data && (
        <div role="status" className="mt-3 space-y-2 text-sm">
          {postReady.data.posted.length > 0 && (
            <p className="border-l-2 border-ledger bg-ledger-tint px-3 py-2">
              Posted {postReady.data.posted.length} to Tally.
            </p>
          )}
          {postReady.data.failed.length > 0 && (
            <ErrorNote>
              {postReady.data.failed.length} couldn&apos;t be posted. Open them to see why.
            </ErrorNote>
          )}
        </div>
      )}
      {postReady.error && (
        <div className="mt-3">
          <ErrorNote>{postReady.error.message}</ErrorNote>
        </div>
      )}

      {docs.error && (
        <div className="mt-4">
          <ErrorNote>{docs.error.message}</ErrorNote>
        </div>
      )}

      {docs.data && shown.length === 0 && (
        <p className="mt-6 text-sm text-ink-soft">
          {filter.key === "all"
            ? "No documents yet. Upload this company's invoices, bills or bank statements above."
            : "Nothing here."}
        </p>
      )}

      {shown.length > 0 && (
        <table className="mt-3 w-full table-fixed text-sm">
          <colgroup>
            <col />
            <col className="w-40 lg:w-56" />
            <col className="w-32" />
            <col className="w-40 lg:w-56" />
            <col className="w-36" />
          </colgroup>
          <thead>
            <tr className="border-b border-rule text-left text-xs text-ink-soft">
              <th className="py-2 pr-4 font-medium">File</th>
              <th className="py-2 pr-4 font-medium">Party</th>
              <th className="py-2 pr-4 text-right font-medium">Amount</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 font-medium">Uploaded</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((d) => (
              <tr key={d.id} className="border-b border-rule align-top hover:bg-sheet">
                <td className="py-2.5 pr-4">
                  <Link
                    href={`/companies/${companyId}/documents/${d.id}`}
                    className="block truncate font-medium text-ink hover:text-ledger hover:underline"
                    title={d.original_filename}
                  >
                    {d.original_filename}
                  </Link>
                  <span className="text-xs text-ink-soft">
                    {kindLabel(d)}, {formatBytes(d.size_bytes)}
                  </span>
                </td>
                <td className="py-2.5 pr-4">
                  <span className="block truncate" title={d.party_name ?? undefined}>
                    {d.party_name ?? <span className="text-ink-soft">—</span>}
                  </span>
                  {d.invoice_number && (
                    <span className="block truncate text-xs text-ink-soft">{d.invoice_number}</span>
                  )}
                </td>
                <td className="py-2.5 pr-4 text-right">
                  {d.grand_total !== null ? (
                    INR.format(d.grand_total)
                  ) : (
                    <span className="text-ink-soft">—</span>
                  )}
                </td>
                <td className="py-2.5 pr-4">
                  {d.status === "parsed" && d.voucher_status ? (
                    <VoucherBadge status={d.voucher_status} />
                  ) : (
                    <StatusBadge status={d.status} />
                  )}
                  {d.status === "failed" && d.error && (
                    <span className="mt-1 block text-xs text-red-ink">{d.error}</span>
                  )}
                  {d.status === "parsed" && !d.voucher_status && d.kind === "sheet" && (
                    <span className="mt-1 block text-xs text-ink-soft">
                      Spreadsheet import comes with bank statement support.
                    </span>
                  )}
                </td>
                <td className="whitespace-nowrap py-2.5 text-ink-soft">
                  {formatDateTime(d.created_at)}
                  {d.uploader_name && <span className="block text-xs">{d.uploader_name}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function UploadZone({ companyId }: { companyId: number }) {
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function upload(fileList: FileList | null) {
    const files = Array.from(fileList ?? []);
    if (!files.length || progress !== null) return;
    setError(null);
    setResult(null);
    setProgress(0);
    try {
      setResult(await documentsApi.upload(companyId, files, setProgress));
      queryClient.invalidateQueries({ queryKey: ["documents", companyId] });
      queryClient.invalidateQueries({ queryKey: ["document-counts", companyId] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setProgress(null);
      if (input.current) input.current.value = "";
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    upload(e.dataTransfer.files);
  }

  const added = result?.documents.filter((d) => d.status !== "duplicate").length ?? 0;
  const duplicates = result?.documents.filter((d) => d.status === "duplicate") ?? [];

  return (
    <section aria-label="Upload documents">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`flex flex-col items-start gap-3 border border-dashed px-5 py-5 sm:flex-row sm:items-center sm:justify-between ${
          dragging ? "border-ledger bg-ledger-tint" : "border-rule-strong bg-sheet"
        }`}
      >
        <div>
          <p className="font-medium">
            {progress !== null
              ? `Uploading… ${Math.round(progress * 100)}%`
              : "Drop invoices, bills or bank statements here"}
          </p>
          <p className="mt-0.5 text-xs text-ink-soft">
            PDF, JPG, PNG, Word, Excel or CSV, up to {MAX_UPLOAD_MB} MB each.
          </p>
        </div>
        <Button
          variant="secondary"
          onClick={() => input.current?.click()}
          disabled={progress !== null}
        >
          Choose files
        </Button>
        <input
          ref={input}
          type="file"
          multiple
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => upload(e.target.files)}
        />
      </div>
      {progress !== null && (
        <div className="h-1 bg-rule" aria-hidden>
          <div
            className="h-1 bg-ledger transition-[width]"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      )}

      {error && (
        <div className="mt-3">
          <ErrorNote>{error}</ErrorNote>
        </div>
      )}
      {result && (
        <div role="status" className="mt-3 space-y-2 text-sm">
          {added > 0 && (
            <p className="border-l-2 border-ledger bg-ledger-tint px-3 py-2">
              Added {added} {added === 1 ? "file" : "files"}. They&apos;re being read now.
            </p>
          )}
          {duplicates.length > 0 && (
            <p className="border-l-2 border-rule-strong bg-sheet px-3 py-2">
              Already uploaded, so not read again:{" "}
              {duplicates.map((d) => d.original_filename).join(", ")}
            </p>
          )}
          {result.rejected.length > 0 && (
            <div className="border-l-2 border-red-ink bg-red-tint px-3 py-2 text-red-ink">
              <p className="font-medium">
                {result.rejected.length === 1
                  ? "1 file wasn't added"
                  : `${result.rejected.length} files weren't added`}
              </p>
              <ul className="mt-1 space-y-0.5">
                {result.rejected.map((r, i) => (
                  <li key={i}>
                    {r.filename}: {r.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
