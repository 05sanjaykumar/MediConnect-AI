// frontend/lib/format.ts
//
// Dates, times and the five doctor statuses, formatted the same way everywhere.

import type { DoctorStatus } from "./api";

export const STATUS_LABEL: Record<DoctorStatus, string> = {
  available: "Available",
  busy: "Busy",
  in_surgery: "In surgery",
  on_break: "On break",
  off_duty: "Off duty",
};

/** Tailwind classes per status. Semantic colour, kept away from the accent. */
export const STATUS_STYLE: Record<DoctorStatus, string> = {
  available:
    "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-400/30",
  busy: "bg-amber-50 text-amber-700 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-400 dark:ring-amber-400/30",
  in_surgery:
    "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-400 dark:ring-rose-400/30",
  on_break:
    "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-400 dark:ring-sky-400/30",
  off_duty:
    "bg-zinc-100 text-zinc-600 ring-zinc-500/20 dark:bg-zinc-500/10 dark:text-zinc-400 dark:ring-zinc-400/30",
};

export const STATUS_DOT: Record<DoctorStatus, string> = {
  available: "bg-emerald-500",
  busy: "bg-amber-500",
  in_surgery: "bg-rose-500",
  on_break: "bg-sky-500",
  off_duty: "bg-zinc-400",
};

export const ALL_STATUSES: DoctorStatus[] = [
  "available",
  "busy",
  "on_break",
  "in_surgery",
  "off_duty",
];

/** "YYYY-MM-DD" in the *local* timezone — toISOString() would shift the day. */
export function toDateParam(date: Date): string {
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatDay(value: string | Date): string {
  const date = typeof value === "string" ? new Date(value) : value;
  const today = new Date();
  const tomorrow = new Date();
  tomorrow.setDate(today.getDate() + 1);

  if (toDateParam(date) === toDateParam(today)) return "Today";
  if (toDateParam(date) === toDateParam(tomorrow)) return "Tomorrow";
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

export function formatDateTime(iso: string): string {
  return `${formatDay(iso)}, ${formatTime(iso)}`;
}

/** The next `count` days, for a date picker strip. */
export function upcomingDays(count: number): Date[] {
  return Array.from({ length: count }, (_, index) => {
    const date = new Date();
    date.setDate(date.getDate() + index);
    date.setHours(0, 0, 0, 0);
    return date;
  });
}
