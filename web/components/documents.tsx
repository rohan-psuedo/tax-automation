import type { DocumentStatus, DocumentSummary } from "@/lib/api";

export const STATUS_LABEL: Record<DocumentStatus, string> = {
  uploaded: "Queued",
  parsing: "Reading",
  parsed: "Read",
  failed: "Couldn't read",
  duplicate: "Duplicate",
};

const STATUS_STYLE: Record<DocumentStatus, string> = {
  uploaded: "bg-rule/60 text-ink-soft",
  parsing: "bg-amber-tint text-amber",
  parsed: "bg-ledger-tint text-ledger",
  failed: "bg-red-tint text-red-ink",
  duplicate: "bg-rule/60 text-ink-soft",
};

export function StatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <span
      className={`inline-block rounded-[3px] px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLE[status]}`}
    >
      {STATUS_LABEL[status]}
    </span>
  );
}

export const isInProgress = (d: Pick<DocumentSummary, "status">) =>
  d.status === "uploaded" || d.status === "parsing";

const KIND_LABEL: Record<DocumentSummary["kind"], string> = {
  pdf: "PDF",
  image: "Image",
  docx: "Word",
  sheet: "Spreadsheet",
};

export function kindLabel(doc: Pick<DocumentSummary, "kind" | "mime_type">) {
  return doc.mime_type === "text/csv" ? "CSV" : KIND_LABEL[doc.kind];
}

export function formatBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
