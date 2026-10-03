"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { type KeyboardEvent, useId, useState } from "react";
import { StatusBadge, formatBytes, isInProgress, kindLabel } from "@/components/documents";
import { VoucherHistoryTimeline } from "@/components/history";
import { Button, ErrorNote, Ident, formatDateTime } from "@/components/ui";
import { VoucherPanel, voucherInProgress } from "@/components/voucher-panel";
import {
  ApiError,
  type DocumentDetail,
  type ParsedSheet,
  documentsApi,
  type User,
  vouchersApi,
} from "@/lib/api";

type Panel = "voucher" | "history" | "file";

const PANELS: readonly (readonly [Panel, string])[] = [
  ["voucher", "Voucher"],
  ["history", "History"],
  ["file", "File details"],
];

export default function DocumentPage() {
  const { id, docId } = useParams<{ id: string; docId: string }>();
  const companyId = Number(id);
  const documentId = Number(docId);
  const router = useRouter();
  const queryClient = useQueryClient();
  const me = queryClient.getQueryData<User>(["me"]);

  const doc = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => documentsApi.get(documentId),
    refetchInterval: (query) => (query.state.data && isInProgress(query.state.data) ? 1500 : false),
  });

  const voucher = useQuery({
    queryKey: ["voucher", documentId],
    queryFn: () => vouchersApi.forDocument(documentId),
    enabled: doc.data?.status === "parsed",
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
    refetchInterval: (query) =>
      query.state.data && voucherInProgress(query.state.data) ? 2000 : false,
  });
  const [tab, setTab] = useState<Panel>("voucher");
  // The history is fetched only once someone opens its tab, then kept up to date.
  const [historyOpened, setHistoryOpened] = useState(false);
  const idBase = useId();
  const tabId = (panel: Panel) => `${idBase}-tab-${panel}`;
  const panelId = (panel: Panel) => `${idBase}-panel-${panel}`;

  const selectTab = (panel: Panel) => {
    setTab(panel);
    if (panel === "history") setHistoryOpened(true);
  };
  // Arrow keys, Home and End move between tabs, as screen reader users expect of a tab list.
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = PANELS.length - 1;
    const next =
      event.key === "ArrowRight"
        ? (index + 1) % PANELS.length
        : event.key === "ArrowLeft"
          ? (index + last) % PANELS.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (next === null) return;
    event.preventDefault();
    selectTab(PANELS[next][0]);
    const tabs = event.currentTarget.parentElement?.querySelectorAll<HTMLElement>('[role="tab"]');
    tabs?.[next]?.focus();
  };

  const refreshLists = () => {
    queryClient.invalidateQueries({ queryKey: ["documents", companyId] });
    queryClient.invalidateQueries({ queryKey: ["document-counts", companyId] });
  };
  const retry = useMutation({
    mutationFn: () => documentsApi.retry(documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["document", documentId] });
      refreshLists();
    },
  });
  const remove = useMutation({
    mutationFn: () => documentsApi.remove(documentId),
    onSuccess: () => {
      refreshLists();
      router.replace(`/companies/${companyId}`);
    },
  });

  const back = (
    <Link
      href={`/companies/${companyId}`}
      className="text-sm text-ink-soft hover:text-ink hover:underline"
    >
      Back to documents
    </Link>
  );

  if (doc.error) {
    return (
      <div className="space-y-4 pt-6">
        {back}
        <ErrorNote>{doc.error.message}</ErrorNote>
      </div>
    );
  }
  if (!doc.data) return null;
  const d = doc.data;
  const canDelete =
    me &&
    (me.role !== "preparer" || d.uploaded_by === me.id) &&
    voucher.data?.status !== "posted" &&
    voucher.data?.status !== "posting";

  const fileDetails = (
    <div className="space-y-5">
      <div>
        <h2 className="break-words font-semibold leading-snug">{d.original_filename}</h2>
        <div className="mt-2">
          <StatusBadge status={d.status} />
        </div>
      </div>

      {d.status === "failed" && d.error && <ErrorNote>{d.error}</ErrorNote>}
      {d.status === "duplicate" && d.duplicate_of_id && (
        <p className="text-sm text-ink-soft">
          This exact file was already uploaded.{" "}
          <Link
            href={`/companies/${companyId}/documents/${d.duplicate_of_id}`}
            className="text-ledger underline-offset-2 hover:underline"
          >
            Open the original
          </Link>
        </p>
      )}
      {(d.parsed.warnings ?? []).map((w) => (
        <p key={w} className="border-l-2 border-amber bg-amber-tint px-3 py-2 text-sm text-amber">
          {w}
        </p>
      ))}

      <dl className="divide-y divide-rule border-y border-rule text-sm">
        <Fact label="Type">
          {kindLabel(d)}, {formatBytes(d.size_bytes)}
        </Fact>
        {d.page_count > 0 && <Fact label="Pages">{d.page_count}</Fact>}
        {d.status === "parsed" && d.kind === "pdf" && (
          <Fact label="Text layer">
            {d.has_text_layer ? "Yes" : "No. It's a scan, so the page images will be read."}
          </Fact>
        )}
        <Fact label="Uploaded">
          {formatDateTime(d.created_at)}
          {d.uploader_name && ` by ${d.uploader_name}`}
        </Fact>
        <Fact label="Fingerprint">
          <span title={d.sha256}>
            <Ident>{d.sha256.slice(0, 16)}</Ident>
          </span>
        </Fact>
      </dl>

      <div className="flex flex-wrap gap-2">
        <a
          href={documentsApi.fileUrl(d.id)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-9 items-center rounded-[3px] border border-rule-strong bg-sheet px-3.5 text-sm font-medium hover:border-ink-soft"
        >
          Open original
        </a>
        {d.status === "failed" && (
          <Button onClick={() => retry.mutate()} disabled={retry.isPending}>
            Try reading again
          </Button>
        )}
        {canDelete && d.status !== "parsing" && (
          <Button
            variant="quiet"
            className="text-red-ink hover:bg-red-tint hover:text-red-ink"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(`Delete "${d.original_filename}"? This can't be undone.`)) {
                remove.mutate();
              }
            }}
          >
            Delete
          </Button>
        )}
      </div>
      {(retry.error || remove.error) && (
        <ErrorNote>{(retry.error ?? remove.error)?.message}</ErrorNote>
      )}
    </div>
  );

  return (
    <div className="pt-5">
      {back}
      <div
        className={`mt-3 grid gap-8 ${
          voucher.data
            ? "xl:grid-cols-[minmax(0,1fr)_480px] lg:grid-cols-[minmax(0,1fr)_420px]"
            : "lg:grid-cols-[minmax(0,1fr)_300px]"
        }`}
      >
        <div className="min-w-0 lg:order-1">
          <Content doc={d} />
        </div>

        <aside className="lg:order-2">
          {voucher.data ? (
            <>
              <div
                role="tablist"
                aria-label="Panels"
                className="mb-4 flex gap-5 border-b border-rule"
              >
                {PANELS.map(([key, label], index) => (
                  <button
                    key={key}
                    type="button"
                    role="tab"
                    id={tabId(key)}
                    aria-selected={tab === key}
                    aria-controls={panelId(key)}
                    tabIndex={tab === key ? 0 : -1}
                    onClick={() => selectTab(key)}
                    onKeyDown={(event) => onTabKeyDown(event, index)}
                    className="-mb-px border-b-2 border-transparent pb-2 text-sm text-ink-soft hover:text-ink aria-selected:border-ledger aria-selected:font-medium aria-selected:text-ink"
                  >
                    {label}
                  </button>
                ))}
              </div>
              {/* All panels stay mounted and only the chosen one is shown, so switching tabs
                  keeps the unsaved edits held inside the voucher panel. */}
              <div
                role="tabpanel"
                id={panelId("voucher")}
                aria-labelledby={tabId("voucher")}
                hidden={tab !== "voucher"}
              >
                <VoucherPanel voucher={voucher.data} user={me} />
              </div>
              <div
                role="tabpanel"
                id={panelId("history")}
                aria-labelledby={tabId("history")}
                hidden={tab !== "history"}
              >
                {historyOpened && (
                  <VoucherHistoryTimeline
                    voucherId={voucher.data.id}
                    updatedAt={voucher.data.updated_at}
                  />
                )}
              </div>
              <div
                role="tabpanel"
                id={panelId("file")}
                aria-labelledby={tabId("file")}
                hidden={tab !== "file"}
                className="lg:sticky lg:top-6"
              >
                {fileDetails}
              </div>
            </>
          ) : (
            <div className="lg:sticky lg:top-6">{fileDetails}</div>
          )}
        </aside>
      </div>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_1fr] gap-3 py-2">
      <dt className="text-ink-soft">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Content({ doc }: { doc: DocumentDetail }) {
  if (isInProgress(doc)) {
    return (
      <div className="grid min-h-64 place-items-center border border-rule bg-sheet text-sm text-ink-soft">
        {doc.status === "uploaded" ? "Waiting to be read…" : "Reading the file…"}
      </div>
    );
  }
  if (doc.status === "failed") {
    return (
      <div className="grid min-h-64 place-items-center border border-rule bg-sheet px-6 text-center text-sm text-ink-soft">
        Nothing to show, because the file couldn&apos;t be read. Open the original to check it.
      </div>
    );
  }

  const pages = doc.parsed.pages ?? [];
  const sheets = doc.parsed.sheets ?? [];
  return (
    <div className="space-y-6">
      {pages.map((p) => (
        <figure key={p.number}>
          {/* eslint-disable-next-line @next/next/no-img-element -- authenticated API image */}
          <img
            src={documentsApi.pageUrl(doc.id, p.number)}
            alt={`Page ${p.number} of ${doc.original_filename}`}
            width={p.width}
            height={p.height}
            loading="lazy"
            className="h-auto w-full border border-rule bg-white shadow-[0_1px_0_var(--rule)]"
          />
          {pages.length > 1 && (
            <figcaption className="mt-1.5 text-center text-xs text-ink-soft">
              Page {p.number} of {doc.page_count}
            </figcaption>
          )}
        </figure>
      ))}

      {sheets.map((s) => (
        <SheetTable key={s.name} sheet={s} />
      ))}

      {doc.kind === "docx" && doc.text && <TextBlock text={doc.text} open />}
      {doc.kind !== "docx" && doc.text && <TextBlock text={doc.text} />}
    </div>
  );
}

function SheetTable({ sheet }: { sheet: ParsedSheet }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-semibold">{sheet.name}</h3>
      <div className="overflow-x-auto border border-rule bg-sheet">
        <table className="w-full text-sm">
          <tbody>
            {sheet.rows.map((row, i) => (
              <tr key={i} className={i === 0 ? "bg-paper font-medium" : "border-t border-rule"}>
                {row.map((cell, j) => (
                  <td key={j} className="whitespace-nowrap px-3 py-1.5">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sheet.total_rows > sheet.rows.length && (
        <p className="mt-1.5 text-xs text-ink-soft">
          Showing {sheet.rows.length} of {sheet.total_rows} rows.
        </p>
      )}
    </section>
  );
}

function TextBlock({ text, open = false }: { text: string; open?: boolean }) {
  return (
    <details open={open} className="border border-rule bg-sheet">
      <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium">
        Text found in this file
      </summary>
      <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap border-t border-rule px-4 py-3 font-sans text-sm leading-relaxed text-ink">
        {text}
      </pre>
    </details>
  );
}
