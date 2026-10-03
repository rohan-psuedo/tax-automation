"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Sidebar } from "@/components/sidebar";
import { StatusBar } from "@/components/status-bar";
import { ApiError, api } from "@/lib/api";

export default function AppLayout({ children }: LayoutProps<"/">) {
  const router = useRouter();
  const pathname = usePathname();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const unauthenticated = me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (unauthenticated) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [unauthenticated, router, pathname]);

  if (!me.data) {
    return (
      <div className="grid min-h-screen place-items-center text-sm text-ink-soft">
        {me.error && !unauthenticated ? "Can't reach the server. Is the backend running?" : ""}
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col">
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
      </div>
      <StatusBar user={me.data} />
    </div>
  );
}
