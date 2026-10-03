"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, type ReactNode, useState } from "react";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { type Role, type User, teamApi } from "@/lib/api";

const ROLE_LABEL: Record<Role, string> = {
  admin: "Administrator",
  reviewer: "Reviewer",
  preparer: "Preparer",
};

export default function AccountPage() {
  const me = useQueryClient().getQueryData<User>(["me"]);

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="border-b border-rule pb-4">
        <h1 className="text-xl font-semibold tracking-tight">Your account</h1>
        <p className="mt-1 text-sm text-ink-soft">
          {me?.role === "admin" ? (
            <>
              You can change your name and role under{" "}
              <Link
                href="/settings/team"
                className="text-ledger underline-offset-2 hover:underline"
              >
                Settings, Team
              </Link>
              .
            </>
          ) : (
            "Ask an administrator to change your name or role."
          )}{" "}
          Your email can&apos;t be changed. To sign in with a different one, an administrator adds
          you again with that email.
        </p>
      </header>

      {me && (
        <dl className="mt-6 max-w-md divide-y divide-rule border-y border-rule text-sm">
          <Fact label="Name">{me.full_name}</Fact>
          <Fact label="Email">{me.email}</Fact>
          <Fact label="Role">{ROLE_LABEL[me.role]}</Fact>
        </dl>
      )}

      <ChangePassword email={me?.email} />
    </div>
  );
}

function ChangePassword({ email }: { email: string | undefined }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [mismatch, setMismatch] = useState(false);

  const change = useMutation({
    mutationFn: () => teamApi.changeOwnPassword(current, next),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirm("");
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      setMismatch(true);
      return;
    }
    change.mutate();
  }

  return (
    <section aria-labelledby="password-title" className="mt-8 max-w-md">
      <h2 id="password-title" className="text-sm font-semibold">
        Change your password
      </h2>
      <form onSubmit={onSubmit} className="mt-3 space-y-4">
        {/* Lets password managers file the new password under the right account. */}
        <input
          type="email"
          name="username"
          value={email ?? ""}
          autoComplete="username"
          readOnly
          hidden
        />
        <Field label="Current password">
          <Input
            type="password"
            value={current}
            onChange={(e) => {
              setCurrent(e.target.value);
              change.reset();
            }}
            autoComplete="current-password"
            required
          />
        </Field>
        <Field label="New password" hint="At least 8 characters.">
          <Input
            type="password"
            value={next}
            onChange={(e) => {
              setNext(e.target.value);
              setMismatch(false);
              change.reset();
            }}
            autoComplete="new-password"
            minLength={8}
            maxLength={128}
            required
          />
        </Field>
        <Field label="Type the new password again">
          <Input
            type="password"
            value={confirm}
            onChange={(e) => {
              setConfirm(e.target.value);
              setMismatch(false);
              change.reset();
            }}
            autoComplete="new-password"
            aria-invalid={mismatch || undefined}
            required
          />
        </Field>

        {mismatch && (
          <ErrorNote>
            The two new passwords are different. Type the same password in both.
          </ErrorNote>
        )}
        {change.error && <ErrorNote>{change.error.message}</ErrorNote>}
        {change.isSuccess && (
          <p role="status" className="border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm">
            Password changed. Use the new one next time you sign in.
          </p>
        )}

        <Button type="submit" disabled={change.isPending}>
          {change.isPending ? "Changing…" : "Change password"}
        </Button>
      </form>
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_1fr] gap-3 py-2">
      <dt className="text-ink-soft">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}
