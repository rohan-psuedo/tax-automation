"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatBytes } from "@/components/documents";
import { Button, ErrorNote, Ident } from "@/components/ui";
import { type Backup, backupsApi } from "@/lib/api";

const REASON_LABEL: Record<Backup["reason"], string> = {
  scheduled: "Automatic",
  manual: "Manual",
  before_migration: "Before an update",
};

/** Backups are kept for weeks, so unlike formatDateTime this shows the year. */
function formatWhen(iso: string) {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function BackupsPage() {
  const queryClient = useQueryClient();
  const backups = useQuery({ queryKey: ["backups"], queryFn: backupsApi.list });
  const create = useMutation({
    mutationFn: backupsApi.create,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
  });

  return (
    <div className="pt-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-xl">
          <h2 className="text-sm font-semibold">Database backups</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-soft">
            The database holds your companies, entries, ledgers, team and history. The app backs it
            up automatically about once a day and keeps the most recent backups on the computer the
            app runs on. Download one now and then to keep a copy somewhere else.
          </p>
        </div>
        <Button onClick={() => create.mutate()} disabled={create.isPending}>
          {create.isPending ? "Backing up…" : "Back up now"}
        </Button>
      </div>

      {create.error && (
        <div className="mt-4">
          <ErrorNote>{create.error.message}</ErrorNote>
        </div>
      )}
      {create.data && (
        <p role="status" className="mt-4 border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm">
          Backup made {formatWhen(create.data.created_at)}, {formatBytes(create.data.size_bytes)}.
        </p>
      )}

      {backups.error && (
        <div className="mt-6">
          <ErrorNote>{backups.error.message}</ErrorNote>
        </div>
      )}
      {backups.isPending && <p className="mt-6 text-sm text-ink-soft">Loading backups…</p>}
      {backups.data?.length === 0 && (
        <div className="mt-8 max-w-md">
          <p className="font-medium">No backups yet</p>
          <p className="mt-1 text-sm leading-relaxed text-ink-soft">
            Choose Back up now to make the first one. After that the app makes one automatically
            about once a day.
          </p>
        </div>
      )}

      {backups.data && backups.data.length > 0 && (
        <div className="mt-6 overflow-x-auto">
          <table className="w-full min-w-[32rem] text-sm">
            <thead>
              <tr className="border-b border-rule text-left text-xs text-ink-soft">
                <th className="py-2 pr-4 font-medium">Made</th>
                <th className="py-2 pr-4 font-medium">Why</th>
                <th className="py-2 pr-4 text-right font-medium">Size</th>
                <th className="py-2 font-medium">
                  <span className="sr-only">Download</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {backups.data.map((b) => (
                <tr key={b.name} className="border-b border-rule align-top hover:bg-sheet">
                  <td className="py-2.5 pr-4">
                    <span className="block">{formatWhen(b.created_at)}</span>
                    <span className="block break-all font-mono text-xs text-ink-soft">
                      {b.name}
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 text-ink-soft">{REASON_LABEL[b.reason]}</td>
                  <td className="whitespace-nowrap py-2.5 pr-4 text-right">
                    {formatBytes(b.size_bytes)}
                  </td>
                  <td className="py-2.5 text-right">
                    <a
                      href={backupsApi.downloadUrl(b.name)}
                      download={b.name}
                      aria-label={`Download the backup made ${formatWhen(b.created_at)}`}
                      className="text-ledger underline-offset-2 hover:underline"
                    >
                      Download
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <section
        aria-labelledby="restore-title"
        className="mt-10 max-w-2xl border-t border-rule pt-6"
      >
        <h2 id="restore-title" className="text-sm font-semibold">
          What is not in a backup
        </h2>
        <p className="mt-2 border-l-2 border-amber bg-amber-tint px-3 py-2 text-sm text-ink">
          Uploaded invoices and other files are not inside these backups. They are kept in the{" "}
          <Ident>backend/data/storage</Ident> folder on the computer the app runs on; copy that
          folder with your usual file backup.
        </p>

        <h2 className="mt-6 text-sm font-semibold">How to restore a backup</h2>
        <ol className="mt-2 list-decimal space-y-1.5 pl-5 text-sm leading-relaxed">
          <li>Stop the app.</li>
          <li>
            In the <Ident>backend/data</Ident> folder, move <Ident>app.db</Ident> somewhere safe. If
            files named <Ident>app.db-wal</Ident> or <Ident>app.db-shm</Ident> are next to it, move
            them too.
          </li>
          <li>
            Copy the backup file into <Ident>backend/data</Ident> and rename it to{" "}
            <Ident>app.db</Ident>. Backups are kept in <Ident>backend/data/backups</Ident>, or use
            one you downloaded.
          </li>
          <li>Start the app.</li>
        </ol>
        <p className="mt-3 text-sm text-ink-soft">
          Anything entered after the backup was made will be missing. If you are not sure how to do
          this, ask whoever set up the app.
        </p>
      </section>
    </div>
  );
}
