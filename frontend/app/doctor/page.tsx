// frontend/app/doctor/page.tsx
//
// The doctor's own screen: today's list, and the one-click status switch.
//
// Changing status is not just a flag. The backend blocks the slots inside the
// affected window and reports how many — and, importantly, which already-booked
// appointments now fall inside it. Those are never cancelled automatically;
// they are surfaced here for a human to deal with, which is the whole point.

"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type Appointment,
  type Doctor,
  type DoctorStatus,
  type StatusResult,
} from "@/lib/api";
import {
  ALL_STATUSES,
  STATUS_DOT,
  STATUS_LABEL,
  formatDateTime,
  formatTime,
  toDateParam,
} from "@/lib/format";
import { subscribeToDoctorStatus } from "@/lib/realtime";
import { useSession } from "@/lib/session";
import {
  Banner,
  Card,
  Empty,
  LiveDot,
  Loading,
  SectionTitle,
  Shell,
  StatusPill,
} from "@/components/ui";

export default function DoctorPage() {
  const session = useSession("doctor");

  const [profile, setProfile] = useState<Doctor | null>(null);
  const [appointments, setAppointments] = useState<Appointment[] | null>(null);
  const [lastChange, setLastChange] = useState<StatusResult | null>(null);
  const [problem, setProblem] = useState("");
  const [saving, setSaving] = useState<DoctorStatus | null>(null);
  const [live, setLive] = useState(false);

  const doctorId = session?.doctorId;

  const loadProfile = useCallback(async (id: number) => {
    setProfile(await api.doctor(id));
  }, []);

  const loadAppointments = useCallback(async (token: string) => {
    setAppointments(await api.myAppointments(token));
  }, []);

  useEffect(() => {
    if (!session || !doctorId) return;
    void (async () => {
      await loadProfile(doctorId).catch((err: ApiError) => setProblem(err.message));
      await loadAppointments(session.token).catch(() => {});
    })();
  }, [session, doctorId, loadProfile, loadAppointments]);

  // An admin can change a doctor's status too — reflect it here without a refresh.
  useEffect(() => {
    if (!doctorId) return;
    const unsubscribe = subscribeToDoctorStatus((change) => {
      if (change.doctorId !== doctorId) return;
      setProfile((current) =>
        current
          ? {
              ...current,
              status: change.status,
              is_bookable: change.status === "available",
            }
          : current,
      );
    }, setLive);
    return unsubscribe;
  }, [doctorId]);

  async function changeStatus(status: DoctorStatus) {
    if (!session || !doctorId) return;
    setSaving(status);
    setProblem("");
    try {
      const result = await api.setStatus(doctorId, status, session.token);
      setLastChange(result);
      setProfile((current) =>
        current
          ? { ...current, status: result.status, is_bookable: result.status === "available" }
          : current,
      );
      await loadAppointments(session.token);
    } catch (err) {
      if (err instanceof ApiError) setProblem(err.message);
    } finally {
      setSaving(null);
    }
  }

  if (!session) return null;

  if (!doctorId) {
    return (
      <Shell session={session} title="My day">
        <Banner tone="error">
          This account has no doctor profile attached. Sign out and back in, or
          ask an admin to check the record.
        </Banner>
      </Shell>
    );
  }

  const today = toDateParam(new Date());
  const todays = (appointments ?? []).filter(
    (appointment) => toDateParam(new Date(appointment.start_at)) === today,
  );
  const later = (appointments ?? []).filter(
    (appointment) => toDateParam(new Date(appointment.start_at)) !== today,
  );
  const needsAttention = new Set(lastChange?.appointments_needing_attention ?? []);

  return (
    <Shell
      session={session}
      title="My day"
      subtitle={profile ? `${profile.specialization} · ${profile.qualification}` : undefined}
    >
      {problem && (
        <Banner tone="error" onDismiss={() => setProblem("")}>
          {problem}
        </Banner>
      )}

      {lastChange && (
        <Banner tone={needsAttention.size ? "warn" : "ok"} onDismiss={() => setLastChange(null)}>
          <p>
            Status set to <strong>{STATUS_LABEL[lastChange.status]}</strong>.{" "}
            {lastChange.slots_blocked > 0 &&
              `${lastChange.slots_blocked} open slot${lastChange.slots_blocked === 1 ? "" : "s"} taken off the market.`}
            {lastChange.slots_released > 0 &&
              `${lastChange.slots_released} slot${lastChange.slots_released === 1 ? "" : "s"} released back.`}
            {lastChange.slots_blocked === 0 &&
              lastChange.slots_released === 0 &&
              "No open slots were affected."}
          </p>
          {needsAttention.size > 0 && (
            <p className="mt-1.5">
              {needsAttention.size} confirmed appointment
              {needsAttention.size === 1 ? "" : "s"} fall inside that window.
              They have <strong>not</strong> been cancelled — they&apos;re marked
              below for you to decide.
            </p>
          )}
        </Banner>
      )}

      <div className="grid gap-6 lg:grid-cols-[300px_1fr]">
        {/* ------------------------------------------------------- status */}
        <div>
          <div className="mb-3 flex items-center justify-between">
            <SectionTitle>My status</SectionTitle>
            <LiveDot live={live} />
          </div>

          <Card className="p-5">
            {profile ? (
              <>
                <div className="mb-4">
                  <p className="font-medium text-zinc-900 dark:text-zinc-100">
                    {profile.name}
                  </p>
                  <div className="mt-2">
                    <StatusPill status={profile.status} />
                  </div>
                </div>

                <div className="space-y-1.5">
                  {ALL_STATUSES.map((status) => {
                    const active = profile.status === status;
                    return (
                      <button
                        key={status}
                        onClick={() => void changeStatus(status)}
                        disabled={saving !== null || active}
                        className={`flex w-full items-center gap-2.5 rounded-md border px-3 py-2 text-left text-sm transition disabled:cursor-not-allowed ${
                          active
                            ? "border-teal-600 bg-teal-50 font-medium text-teal-900 dark:border-teal-400/60 dark:bg-teal-500/10 dark:text-teal-200"
                            : "border-zinc-200 text-zinc-700 hover:border-zinc-300 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-800 dark:text-zinc-300 dark:hover:border-zinc-700 dark:hover:bg-zinc-800"
                        }`}
                      >
                        <span className={`h-2 w-2 rounded-full ${STATUS_DOT[status]}`} />
                        {STATUS_LABEL[status]}
                        {saving === status && (
                          <span className="ml-auto text-xs opacity-60">saving…</span>
                        )}
                        {active && saving === null && (
                          <span className="ml-auto text-xs opacity-60">current</span>
                        )}
                      </button>
                    );
                  })}
                </div>

                <p className="mt-4 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
                  In surgery and Off duty block the rest of today. On break
                  blocks 30 minutes, Busy the current slot.
                </p>
              </>
            ) : (
              <Loading />
            )}
          </Card>
        </div>

        {/* ----------------------------------------------------- schedule */}
        <div>
          <SectionTitle>Today</SectionTitle>
          {appointments === null ? (
            <Loading label="Loading your schedule…" />
          ) : todays.length === 0 ? (
            <Empty>Nothing booked for today.</Empty>
          ) : (
            <div className="space-y-2">
              {todays.map((appointment) => (
                <AppointmentRow
                  key={appointment.id}
                  appointment={appointment}
                  flagged={needsAttention.has(appointment.id)}
                  showDay={false}
                />
              ))}
            </div>
          )}

          <div className="mt-8">
            <SectionTitle>Coming up</SectionTitle>
            {appointments === null ? null : later.length === 0 ? (
              <Empty>Nothing else booked.</Empty>
            ) : (
              <div className="space-y-2">
                {later.map((appointment) => (
                  <AppointmentRow
                    key={appointment.id}
                    appointment={appointment}
                    flagged={needsAttention.has(appointment.id)}
                    showDay
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}

function AppointmentRow({
  appointment,
  flagged,
  showDay,
}: {
  appointment: Appointment;
  flagged: boolean;
  showDay: boolean;
}) {
  return (
    <Card
      className={`flex flex-wrap items-center gap-3 px-4 py-3 ${
        flagged ? "border-amber-300 bg-amber-50/60 dark:border-amber-500/40 dark:bg-amber-500/5" : ""
      }`}
    >
      <div className="w-28 shrink-0 text-sm tabular-nums text-zinc-900 dark:text-zinc-100">
        {showDay ? formatDateTime(appointment.start_at) : formatTime(appointment.start_at)}
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
          {appointment.patient_name}
        </p>
        <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">
          {appointment.reason || "No reason given"}
        </p>
      </div>
      {appointment.created_via === "voice" && (
        <span className="rounded bg-teal-50 px-1.5 py-0.5 text-[11px] font-medium text-teal-700 dark:bg-teal-500/10 dark:text-teal-400">
          by voice
        </span>
      )}
      {flagged && (
        <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-500/15 dark:text-amber-300">
          needs attention
        </span>
      )}
    </Card>
  );
}
