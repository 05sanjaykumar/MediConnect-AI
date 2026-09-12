// frontend/components/ui.tsx
//
// The small shared pieces every dashboard is built from. Kept in one file
// because there are only a handful and they are all a dozen lines.

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import type { DoctorStatus } from "@/lib/api";
import { STATUS_DOT, STATUS_LABEL, STATUS_STYLE } from "@/lib/format";
import { clearSession, type Session } from "@/lib/session";

/* ------------------------------------------------------------------ layout */

export function Shell({
  session,
  title,
  subtitle,
  children,
}: {
  session: Session;
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  const router = useRouter();

  const links: { href: string; label: string }[] = [
    { href: "/", label: "Voice" },
    ...(session.role === "patient"
      ? [{ href: "/patient", label: "Book" }]
      : []),
    ...(session.role === "doctor" ? [{ href: "/doctor", label: "My day" }] : []),
    ...(session.role === "admin" ? [{ href: "/admin", label: "Admin" }] : []),
  ];

  return (
    <div className="min-h-full">
      <header className="border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80 sticky top-0 z-10">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-5 py-3">
          <Link href="/" className="flex items-baseline gap-2">
            <span className="text-base font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
              MediConnect
            </span>
            <span className="text-[10px] font-medium uppercase tracking-[0.14em] text-teal-700 dark:text-teal-400">
              AI
            </span>
          </Link>

          <nav className="flex items-center gap-1 text-sm">
            {links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className="rounded-md px-2.5 py-1.5 text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
              >
                {link.label}
              </Link>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-3">
            <span className="hidden text-sm text-zinc-500 sm:block dark:text-zinc-400">
              {session.name}
              <span className="ml-2 rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] uppercase tracking-wide text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                {session.role}
              </span>
            </span>
            <button
              onClick={() => {
                clearSession();
                router.replace("/login");
              }}
              className="rounded-md px-2.5 py-1.5 text-sm text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            {title}
          </h1>
          {subtitle && (
            <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
              {subtitle}
            </p>
          )}
        </div>
        {children}
      </main>
    </div>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900 ${className}`}
    >
      {children}
    </div>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="mb-3 text-xs font-medium uppercase tracking-[0.12em] text-zinc-500 dark:text-zinc-400">
      {children}
    </h2>
  );
}

/* ------------------------------------------------------------------ pieces */

export function StatusPill({
  status,
  className = "",
}: {
  status: DoctorStatus;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[status]} ${className}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[status]}`} />
      {STATUS_LABEL[status]}
    </span>
  );
}

export function Button({
  children,
  onClick,
  type = "button",
  variant = "primary",
  disabled,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  variant?: "primary" | "ghost" | "danger";
  disabled?: boolean;
  className?: string;
}) {
  const styles = {
    primary:
      "bg-teal-700 text-white hover:bg-teal-800 disabled:bg-teal-700/40 dark:bg-teal-600 dark:hover:bg-teal-500",
    ghost:
      "border border-zinc-300 text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800",
    danger:
      "border border-rose-300 text-rose-700 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-500/40 dark:text-rose-400 dark:hover:bg-rose-500/10",
  }[variant];

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export function Banner({
  tone,
  children,
  onDismiss,
}: {
  tone: "ok" | "warn" | "error";
  children: ReactNode;
  onDismiss?: () => void;
}) {
  const styles = {
    ok: "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200",
    warn: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200",
    error:
      "border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-200",
  }[tone];

  return (
    <div
      className={`mb-4 flex items-start gap-3 rounded-lg border px-4 py-3 text-sm ${styles}`}
    >
      <div className="flex-1">{children}</div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          className="shrink-0 opacity-60 transition hover:opacity-100"
          aria-label="Dismiss"
        >
          ✕
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-zinc-300 px-6 py-10 text-center text-sm text-zinc-500 dark:border-zinc-700 dark:text-zinc-400">
      {children}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-10 text-sm text-zinc-500 dark:text-zinc-400">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-zinc-300 border-t-teal-600 dark:border-zinc-700 dark:border-t-teal-400" />
      {label}
    </div>
  );
}

/** Shows whether the Supabase Realtime subscription is actually attached. */
export function LiveDot({ live }: { live: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-zinc-500 dark:text-zinc-400">
      <span className="relative flex h-2 w-2">
        {live && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-teal-400 opacity-75" />
        )}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${live ? "bg-teal-500" : "bg-zinc-300 dark:bg-zinc-600"}`}
        />
      </span>
      {live ? "Live" : "Not live"}
    </span>
  );
}
