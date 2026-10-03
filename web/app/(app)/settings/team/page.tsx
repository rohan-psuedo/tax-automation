"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { type Role, type User, teamApi } from "@/lib/api";

const ROLES: { role: Role; label: string; help: string }[] = [
  { role: "admin", label: "Administrator", help: "Settings, team and companies." },
  { role: "reviewer", label: "Reviewer", help: "Checks entries and posts them to Tally." },
  { role: "preparer", label: "Preparer", help: "Uploads and corrects documents." },
];

const ROLE_LABEL = Object.fromEntries(ROLES.map((r) => [r.role, r.label])) as Record<Role, string>;

const SELECT =
  "h-9 w-full rounded-[3px] border border-rule-strong bg-sheet px-2 text-sm focus:border-ledger focus:outline-none disabled:bg-paper";

const OK_NOTE = "border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm";

const QUIET_LINK =
  "inline-flex h-9 items-center rounded-[3px] px-3.5 text-sm font-medium text-ink-soft hover:bg-rule/50 hover:text-ink";

type UserChanges = Partial<Pick<User, "full_name" | "role" | "is_active">>;

/**
 * Focuses the element with the given id after the next render. Used when the control that had
 * focus goes away (a form closes, a button is swapped), so keyboard users keep their place.
 */
function useFocusAfterRender() {
  const pending = useRef<string | null>(null);
  useEffect(() => {
    if (pending.current === null) return;
    document.getElementById(pending.current)?.focus();
    pending.current = null;
  });
  return (id: string) => {
    pending.current = id;
  };
}

export default function TeamPage() {
  const queryClient = useQueryClient();
  const me = queryClient.getQueryData<User>(["me"]);
  const users = useQuery({ queryKey: ["users"], queryFn: teamApi.list });
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState<User | null>(null);
  const addButtonId = useId();
  const focusLater = useFocusAfterRender();

  function closeAdd() {
    setAdding(false);
    focusLater(addButtonId);
  }

  return (
    <div className="pt-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold">People in your office</h2>
          <p className="mt-0.5 text-xs text-ink-soft">
            Each person signs in with their own email and password.
          </p>
        </div>
        {!adding && (
          <Button
            id={addButtonId}
            onClick={() => {
              setAdding(true);
              setAdded(null);
            }}
          >
            Add a person
          </Button>
        )}
      </div>

      <dl className="mt-4 grid gap-3 border-y border-rule py-3 text-sm sm:grid-cols-3">
        {ROLES.map((r) => (
          <div key={r.role}>
            <dt className="font-medium">{r.label}</dt>
            <dd className="text-xs text-ink-soft">{r.help}</dd>
          </div>
        ))}
      </dl>

      {adding && (
        <AddPerson
          onCancel={closeAdd}
          onAdded={(user) => {
            setAdded(user);
            closeAdd();
          }}
        />
      )}
      {/* Always in the page, so screen readers announce the message when it appears. */}
      <div role="status">
        {added && (
          <p className={`mt-4 ${OK_NOTE}`}>
            Added {added.full_name} as {ROLE_LABEL[added.role].toLowerCase()}. Give them their email
            and temporary password to sign in. They can change the password under Your account.
          </p>
        )}
      </div>

      {users.error && (
        <div className="mt-6">
          <ErrorNote>{users.error.message}</ErrorNote>
        </div>
      )}
      {users.isPending && <p className="mt-6 text-sm text-ink-soft">Loading people…</p>}

      {users.data && (
        <div className="mt-6 overflow-x-auto">
          <table className="w-full min-w-[38rem] text-sm">
            <thead>
              <tr className="border-b border-rule text-left text-xs text-ink-soft">
                <th className="py-2 pr-4 font-medium">Name</th>
                <th className="w-48 py-2 pr-4 font-medium">Role</th>
                <th className="w-28 py-2 pr-4 font-medium">Status</th>
                <th className="py-2 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {users.data.map((user) => (
                <PersonRow key={user.id} user={user} isMe={user.id === me?.id} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PersonRow({ user, isMe }: { user: User; isMe: boolean }) {
  const queryClient = useQueryClient();
  const uid = useId();
  const roleId = `${uid}-role`;
  const renameId = `${uid}-rename`;
  const resetId = `${uid}-reset`;
  const focusLater = useFocusAfterRender();

  // The role is chosen first and saved with "Save role": on Windows the arrow keys change a
  // closed select straight away, so saving on change would alter access by accident.
  const [role, setRole] = useState<Role>(user.role);
  const [savedRole, setSavedRole] = useState<Role>(user.role);
  if (savedRole !== user.role) {
    setSavedRole(user.role);
    setRole(user.role);
  }

  const [panel, setPanel] = useState<"rename" | "reset" | null>(null);
  const [name, setName] = useState("");
  const [nameMissing, setNameMissing] = useState(false);
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: (changes: UserChanges) => teamApi.update(user.id, changes),
    onSuccess: (updated, changes) => {
      queryClient.setQueryData<User[]>(["users"], (list) =>
        list?.map((u) => (u.id === updated.id ? updated : u)),
      );
      if (isMe) queryClient.setQueryData(["me"], updated);
      queryClient.invalidateQueries({ queryKey: ["users"] });

      const label = ROLE_LABEL[updated.role];
      if (changes.role !== undefined) {
        setNotice(
          isMe ? `Your role is now ${label}.` : `${updated.full_name}'s role is now ${label}.`,
        );
        focusLater(roleId);
      } else if (changes.is_active !== undefined) {
        setNotice(
          updated.is_active
            ? `${updated.full_name} is active again and can sign in.`
            : `${updated.full_name} is deactivated and can't sign in.`,
        );
      } else if (changes.full_name !== undefined) {
        setPanel(null);
        setNotice(`Name changed to ${updated.full_name}.`);
        focusLater(renameId);
      }
    },
  });
  const reset = useMutation({
    mutationFn: (newPassword: string) => teamApi.resetPassword(user.id, newPassword),
    onSuccess: () => {
      setPassword("");
      setPanel(null);
      setNotice(
        `Password reset. Give ${user.full_name} the new temporary password. They can change it under Your account.`,
      );
      focusLater(resetId);
    },
  });

  const busy = update.isPending;

  /** Clears the outcome of the previous action, so only the latest one is shown. */
  function startAction() {
    setNotice(null);
    update.reset();
    reset.reset();
  }

  function openPanel(which: "rename" | "reset") {
    startAction();
    setPanel(which);
    setName(user.full_name);
    setNameMissing(false);
    setPassword("");
  }

  function closePanel() {
    focusLater(panel === "rename" ? renameId : resetId);
    setPanel(null);
    setPassword("");
    update.reset();
    reset.reset();
  }

  function saveRole() {
    if (busy || role === user.role) return;
    if (
      isMe &&
      role !== "admin" &&
      !window.confirm(
        "Remove your own administrator access? You won't be able to open Settings afterwards.",
      )
    ) {
      return;
    }
    startAction();
    update.mutate({ role });
  }

  function cancelRole() {
    setRole(user.role);
    update.reset();
    focusLater(roleId);
  }

  function toggleActive() {
    if (busy) return;
    if (
      user.is_active &&
      !window.confirm(
        `Deactivate ${user.full_name}? They won't be able to sign in until you reactivate them.`,
      )
    ) {
      return;
    }
    startAction();
    update.mutate({ is_active: !user.is_active });
  }

  function onRename(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const value = name.trim();
    if (!value) {
      setNameMissing(true);
      return;
    }
    if (value === user.full_name) {
      closePanel();
      return;
    }
    update.mutate({ full_name: value });
  }

  function onReset(e: FormEvent) {
    e.preventDefault();
    if (reset.isPending) return;
    reset.mutate(password);
  }

  const details = panel !== null || update.error || reset.error;

  return (
    <>
      <tr className={`align-top ${details ? "" : "border-b border-rule"}`}>
        <td className="py-2.5 pr-4">
          <span className="block font-medium">
            {user.full_name}
            {isMe && <span className="font-normal text-ink-soft"> (you)</span>}
          </span>
          <span className="block break-all text-xs text-ink-soft">{user.email}</span>
          {/* Always in the row, so screen readers announce the result of an action. */}
          <span role="status" className={`block text-xs text-ledger ${notice ? "mt-1" : ""}`}>
            {notice}
          </span>
        </td>
        <td className="py-2 pr-4">
          <select
            id={roleId}
            aria-label={`Role for ${user.full_name}`}
            value={role}
            onChange={(e) => {
              if (busy) return;
              setRole(e.target.value as Role);
              setNotice(null);
            }}
            className={SELECT}
          >
            {ROLES.map((r) => (
              <option key={r.role} value={r.role}>
                {r.label}
              </option>
            ))}
          </select>
          {role !== user.role && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              <Button variant="secondary" onClick={saveRole} aria-disabled={busy || undefined}>
                {busy ? "Saving…" : "Save role"}
              </Button>
              <Button variant="quiet" onClick={cancelRole}>
                Cancel
              </Button>
            </div>
          )}
        </td>
        <td className="py-2.5 pr-4">
          <span
            className={`inline-block rounded-[3px] px-1.5 py-0.5 text-xs font-medium ${
              user.is_active ? "bg-ledger-tint text-ledger" : "bg-rule/60 text-ink-soft"
            }`}
          >
            {user.is_active ? "Active" : "Deactivated"}
          </span>
        </td>
        <td className="py-2">
          <div className="flex flex-wrap justify-end gap-1">
            {panel !== "rename" && (
              <Button id={renameId} variant="quiet" onClick={() => openPanel("rename")}>
                Change name
              </Button>
            )}
            {isMe ? (
              <Link href="/account" className={QUIET_LINK}>
                Change your password
              </Link>
            ) : (
              panel !== "reset" && (
                <Button id={resetId} variant="quiet" onClick={() => openPanel("reset")}>
                  Reset password
                </Button>
              )
            )}
            {!isMe && (
              <Button
                variant="quiet"
                className={
                  user.is_active ? "text-red-ink hover:bg-red-tint hover:text-red-ink" : ""
                }
                onClick={toggleActive}
                aria-disabled={busy || undefined}
              >
                {user.is_active ? "Deactivate" : "Reactivate"}
              </Button>
            )}
          </div>
        </td>
      </tr>
      {details && (
        <tr className="border-b border-rule">
          <td colSpan={4} className="space-y-3 pb-3">
            {update.error && <ErrorNote>{update.error.message}</ErrorNote>}
            {panel === "rename" && (
              <form onSubmit={onRename} noValidate className="flex flex-wrap items-end gap-2">
                <div className="w-full max-w-xs">
                  <Field label={`Name for ${user.email}`} hint="As it should appear in the app.">
                    <Input
                      value={name}
                      onChange={(e) => {
                        setName(e.target.value);
                        setNameMissing(false);
                      }}
                      maxLength={255}
                      autoComplete="off"
                      aria-invalid={nameMissing || undefined}
                      required
                      autoFocus
                    />
                  </Field>
                </div>
                <div className="flex gap-2 pb-5">
                  <Button type="submit" aria-disabled={busy || undefined}>
                    {busy ? "Saving…" : "Save name"}
                  </Button>
                  <Button type="button" variant="quiet" onClick={closePanel}>
                    Cancel
                  </Button>
                </div>
              </form>
            )}
            {panel === "rename" && nameMissing && (
              <ErrorNote>Enter the person&apos;s name.</ErrorNote>
            )}
            {panel === "reset" && (
              <form onSubmit={onReset} className="flex flex-wrap items-end gap-2">
                <div className="w-full max-w-xs">
                  <Field
                    label={`New temporary password for ${user.full_name}`}
                    hint="At least 8 characters."
                  >
                    <Input
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      minLength={8}
                      maxLength={128}
                      autoComplete="off"
                      spellCheck={false}
                      required
                      autoFocus
                    />
                  </Field>
                </div>
                <div className="flex gap-2 pb-5">
                  <Button type="submit" aria-disabled={reset.isPending || undefined}>
                    {reset.isPending ? "Setting…" : "Set password"}
                  </Button>
                  <Button type="button" variant="quiet" onClick={closePanel}>
                    Cancel
                  </Button>
                </div>
              </form>
            )}
            {reset.error && <ErrorNote>{reset.error.message}</ErrorNote>}
          </td>
        </tr>
      )}
    </>
  );
}

function AddPerson({ onCancel, onAdded }: { onCancel: () => void; onAdded: (user: User) => void }) {
  const queryClient = useQueryClient();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("preparer");
  const [password, setPassword] = useState("");
  const [nameMissing, setNameMissing] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      teamApi.create({ full_name: fullName.trim(), email: email.trim(), role, password }),
    onSuccess: (user) => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      onAdded(user);
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (create.isPending) return;
    // `required` accepts a name made only of spaces; the server would refuse it.
    if (!fullName.trim()) {
      setNameMissing(true);
      return;
    }
    create.mutate();
  }

  return (
    <form
      onSubmit={onSubmit}
      aria-labelledby="add-person-title"
      className="mt-6 border border-rule bg-sheet p-5"
    >
      <h3 id="add-person-title" className="font-semibold">
        Add a person
      </h3>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <Field label="Name">
          <Input
            value={fullName}
            onChange={(e) => {
              setFullName(e.target.value);
              setNameMissing(false);
            }}
            autoComplete="off"
            maxLength={255}
            aria-invalid={nameMissing || undefined}
            required
            autoFocus
          />
        </Field>
        <Field label="Email" hint="They sign in with this.">
          <Input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="off"
            required
          />
        </Field>
        <Field label="Role">
          <select value={role} onChange={(e) => setRole(e.target.value as Role)} className={SELECT}>
            {ROLES.map((r) => (
              <option key={r.role} value={r.role}>
                {r.label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Temporary password"
          hint="At least 8 characters. Tell them the password; they can change it after signing in."
        >
          <Input
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            maxLength={128}
            autoComplete="off"
            spellCheck={false}
            required
          />
        </Field>
      </div>
      {nameMissing && (
        <div className="mt-4">
          <ErrorNote>Enter the person&apos;s name.</ErrorNote>
        </div>
      )}
      {create.error && (
        <div className="mt-4">
          <ErrorNote>{create.error.message}</ErrorNote>
        </div>
      )}
      <div className="mt-5 flex gap-2">
        <Button type="submit" aria-disabled={create.isPending || undefined}>
          {create.isPending ? "Adding…" : "Add person"}
        </Button>
        <Button type="button" variant="quiet" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
