"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { api, type User } from "@/lib/api";

/** Persistent footer, in the spirit of Tally's own: connection, current company, user. */
export function StatusBar({ user }: { user: User }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const params = useParams<{ id?: string }>();
  const companyId = params.id ? Number(params.id) : null;

  const tally = useQuery({
    queryKey: ["tally-status"],
    queryFn: api.tallyStatus,
    refetchInterval: 30_000,
  });
  const company = useQuery({
    queryKey: ["company", companyId],
    queryFn: () => api.company(companyId!),
    enabled: companyId !== null,
  });

  async function signOut() {
    await api.logout();
    queryClient.clear();
    router.replace("/login");
  }

  const connected = tally.data?.ok;
  const tallyLabel = tally.isPending
    ? "Checking Tally…"
    : connected
      ? "Tally connected"
      : "Tally not reachable";

  return (
    <footer className="flex h-8 shrink-0 items-center gap-5 bg-bar px-4 text-xs text-bar-ink">
      <span
        className="flex items-center gap-2"
        title={tally.data ? `${tally.data.url}: ${tally.data.detail}` : undefined}
      >
        <span
          aria-hidden
          className={`size-2 rounded-full ${
            tally.isPending ? "bg-bar-ink/40" : connected ? "bg-[#4fd1a5]" : "bg-[#ff7b84]"
          }`}
        />
        {tallyLabel}
        {tally.data && <span className="text-bar-ink/60">{tally.data.url}</span>}
      </span>

      {company.data && (
        <span className="hidden truncate sm:inline">
          Company in Tally: <span className="text-white">{company.data.external_company_name}</span>
        </span>
      )}

      <span className="ml-auto flex items-center gap-3">
        <span className="hidden sm:inline">
          {user.full_name} <span className="text-bar-ink/60">({user.role})</span>
        </span>
        <button type="button" onClick={signOut} className="underline-offset-2 hover:underline">
          Sign out
        </button>
      </span>
    </footer>
  );
}
