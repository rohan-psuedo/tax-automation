import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

type ButtonVariant = "primary" | "secondary" | "quiet";

const buttonStyles: Record<ButtonVariant, string> = {
  primary: "bg-ledger text-white hover:bg-[#0b5a4d] disabled:bg-ledger/50",
  secondary:
    "bg-sheet text-ink border border-rule-strong hover:border-ink-soft disabled:text-ink-soft",
  quiet: "text-ink-soft hover:text-ink hover:bg-rule/50",
};

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      className={`inline-flex h-9 items-center justify-center gap-2 rounded-[3px] px-3.5 text-sm font-medium transition-colors disabled:cursor-not-allowed ${buttonStyles[variant]} ${className}`}
      {...props}
    />
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink-soft">{hint}</span>}
    </label>
  );
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`h-9 w-full rounded-[3px] border border-rule-strong bg-sheet px-2.5 text-sm text-ink placeholder:text-ink-soft/70 focus:border-ledger focus:outline-none ${className}`}
      {...props}
    />
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="border-l-2 border-red-ink bg-red-tint px-3 py-2 text-sm text-red-ink">
      {children}
    </p>
  );
}

/** GSTINs and other identifiers are code-like, so they get the mono face. */
export function Ident({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[0.8125rem] tracking-tight">{children}</span>;
}

export function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
