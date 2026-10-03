"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { api } from "@/lib/api";

export default function LoginPage() {
  // useSearchParams needs a Suspense boundary so the page can still be prerendered.
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

/** Only same-app paths. "//host" and "/\host" are treated by browsers as other sites. */
function safeNext(next: string | null): string {
  if (!next || !next.startsWith("/") || next[1] === "/" || next[1] === "\\") {
    return "/companies";
  }
  return next;
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const destination = safeNext(searchParams.get("next"));
  const queryClient = useQueryClient();
  const setup = useQuery({ queryKey: ["setup-status"], queryFn: api.setupStatus });
  const needsSetup = setup.data?.needs_setup ?? false;

  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");

  const submit = useMutation({
    mutationFn: () =>
      needsSetup
        ? api.setup({ email, full_name: fullName, password })
        : api.login({ email, password }),
    onSuccess: (user) => {
      queryClient.setQueryData(["me"], user);
      queryClient.setQueryData(["setup-status"], { needs_setup: false });
      router.replace(destination);
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    submit.mutate();
  }

  return (
    <main className="grid min-h-screen place-items-center px-4 py-12">
      <div className="w-full max-w-sm">
        <p className="mb-8 text-sm font-medium text-ledger">Tax Automaton</p>
        <h1 className="text-2xl font-semibold tracking-tight">
          {needsSetup ? "Set up your office" : "Sign in"}
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-soft">
          {needsSetup
            ? "Create the first administrator account. You can add your team after this."
            : "Use the account your office administrator created for you."}
        </p>

        <form onSubmit={onSubmit} className="mt-8 space-y-4">
          {needsSetup && (
            <Field label="Your name">
              <Input
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                autoComplete="name"
                required
              />
            </Field>
          )}
          <Field label="Email">
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              required
            />
          </Field>
          <Field label="Password" hint={needsSetup ? "At least 8 characters." : undefined}>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={needsSetup ? "new-password" : "current-password"}
              minLength={needsSetup ? 8 : undefined}
              required
            />
          </Field>
          {submit.error && <ErrorNote>{submit.error.message}</ErrorNote>}
          <Button type="submit" className="w-full" disabled={submit.isPending || setup.isPending}>
            {needsSetup ? "Create administrator" : "Sign in"}
          </Button>
        </form>
      </div>
    </main>
  );
}
