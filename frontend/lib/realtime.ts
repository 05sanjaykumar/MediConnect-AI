// frontend/lib/realtime.ts
//
// Live doctor availability, straight from Supabase.
//
// The backend deliberately has no WebSocket hub for this. `doctor_status` is in
// the `supabase_realtime` publication with REPLICA IDENTITY FULL, and RLS
// allows anon SELECT on `doctors` and `doctor_status` only — so the browser can
// subscribe directly while `users` and `appointments` stay sealed.
//
// That is what makes the demo work: a doctor flips their status on one screen
// and the patient's screen updates without a refresh, with nothing in between.

"use client";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import type { DoctorStatus } from "./api";

const URL = process.env.NEXT_PUBLIC_SUPABASE_URL;
const KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

let client: SupabaseClient | null = null;

function getClient(): SupabaseClient | null {
  if (!URL || !KEY) return null;
  if (!client) {
    client = createClient(URL, KEY, {
      auth: { persistSession: false },
      realtime: { params: { eventsPerSecond: 5 } },
    });
  }
  return client;
}

export interface StatusChange {
  doctorId: number;
  status: DoctorStatus;
  previous?: DoctorStatus;
  updatedAt?: string;
}

/**
 * Call `onChange` whenever any doctor's status row changes.
 *
 * `onLive` reports whether the channel actually attached — it fires from
 * Supabase's own subscribe callback, never synchronously, so a component can
 * set state from it without triggering a cascading render.
 *
 * Returns an unsubscribe function, or a no-op if Supabase isn't configured —
 * the dashboards still work without it, they just won't update on their own.
 */
export function subscribeToDoctorStatus(
  onChange: (change: StatusChange) => void,
  onLive?: (live: boolean) => void,
): () => void {
  const supabase = getClient();
  if (!supabase) {
    console.warn(
      "[realtime] NEXT_PUBLIC_SUPABASE_URL / _PUBLISHABLE_KEY not set — " +
        "live status updates are off.",
    );
    return () => {};
  }

  const channel = supabase
    .channel("doctor-status")
    .on(
      "postgres_changes",
      { event: "*", schema: "public", table: "doctor_status" },
      (payload) => {
        const next = payload.new as Record<string, unknown> | null;
        const previous = payload.old as Record<string, unknown> | null;
        if (!next?.doctor_id) return;
        onChange({
          doctorId: Number(next.doctor_id),
          status: next.status as DoctorStatus,
          previous: previous?.status as DoctorStatus | undefined,
          updatedAt: next.updated_at as string | undefined,
        });
      },
    )
    .subscribe((status) => {
      onLive?.(status === "SUBSCRIBED");
    });

  return () => {
    // Deliberately not reporting `false` here: cleanup runs on unmount, and a
    // re-subscribe reports `true` again from its own callback.
    supabase.removeChannel(channel);
  };
}
