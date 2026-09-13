// frontend/app/admin/page.tsx
//
// The hospital-wide view. It opens on the queue — who is waiting to see which
// doctor, soonest first — because that is the question an admin actually has.
// History (past and cancelled) is one tab over, not mixed in with it.
//
// The tile worth watching during a demo is "booked by voice" — it counts
// confirmed appointments with created_via='voice', so it ticks up the moment
// the agent books one over the phone.

"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type AdminOverview,
  type Appointment,
  type AppointmentFilter,
  type Doctor,
  type DoctorQueue,
  type DoctorStatus,
} from "@/lib/api";
import {
  ALL_STATUSES,
  STATUS_LABEL,
  formatDateTime,
  formatDay,
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
  Shell,
  StatusPill,
} from "@/components/ui";

type Tab = "queue" | "doctors" | "history";
type History = "past" | "cancelled" | "all";

const HISTORY_FILTER: Record<History, AppointmentFilter> = {
  past: { scope: "past", status: "confirmed" },
  cancelled: { status: "cancelled" },
  all: { scope: "all" },
};

export default function AdminPage() {
  const session = useSession("admin");

  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [doctors, setDoctors] = useState<Doctor[] | null>(null);
  const [queues, setQueues] = useState<Map<number, DoctorQueue>>(new Map());

  const [tab, setTab] = useState<Tab>("queue");
  const [doctorFilter, setDoctorFilter] = useState<number>(0);
  const [history, setHistory] = useState<History>("past");
  const [rows, setRows] = useState<Appointment[] | null>(null);

  const [problem, setProblem] = useState("");
  const [live, setLive] = useState(false);
  const [savingId, setSavingId] = useState<number | null>(null);

  /* ------------------------------------------------------------- loaders */

  const refresh = useCallback(async (token: string) => {
    const [nextOverview, nextDoctors, nextQueues] = await Promise.all([
      api.overview(token),
      api.allDoctors(token),
      api.queue(token),
    ]);
    setOverview(nextOverview);
    setDoctors(nextDoctors);
    setQueues(new Map(nextQueues.map((queue) => [queue.doctor_id, queue])));
  }, []);

  const loadRows = useCallback(
    async (token: string, which: Tab, doctorId: number, filter: History) => {
      if (which === "doctors") return;
      const base: AppointmentFilter =
        which === "queue"
          ? { scope: "upcoming", status: "confirmed" }
          : HISTORY_FILTER[filter];
      setRows(
        await api.allAppointments(token, {
          ...base,
          doctorId: doctorId || undefined,
          limit: 200,
        }),
      );
    },
    [],
  );

  /* --------------------------------------------------------------- setup */

  useEffect(() => {
    if (!session) return;
    void (async () => {
      await refresh(session.token).catch((err: ApiError) => setProblem(err.message));
    })();
  }, [session, refresh]);

  // Reload the list whenever the view or its filters change. The handlers
  // below clear `rows` before switching, so the spinner shows immediately
  // without a synchronous setState inside this effect.
  useEffect(() => {
    if (!session) return;
    void (async () => {
      await loadRows(session.token, tab, doctorFilter, history).catch(
        (err: ApiError) => setProblem(err.message),
      );
    })();
  }, [session, tab, doctorFilter, history, loadRows]);

  useEffect(() => {
    if (!session) return;
    const unsubscribe = subscribeToDoctorStatus((change) => {
      setDoctors((current) =>
        current
          ? current.map((doctor) =>
              doctor.id === change.doctorId
                ? {
                    ...doctor,
                    status: change.status,
                    is_bookable: change.status === "available",
                  }
                : doctor,
            )
          : current,
      );
      // "Available now" is derived from status, so refresh it too.
      void api.overview(session.token).then(setOverview).catch(() => {});
    }, setLive);
    return unsubscribe;
  }, [session]);

  /* -------------------------------------------------------------- actions */

  function switchTab(next: Tab) {
    if (next === tab) return;
    if (next !== "doctors") setRows(null);
    setTab(next);
  }

  function pickDoctor(doctorId: number) {
    setRows(null);
    setDoctorFilter(doctorId);
  }

  function pickHistory(next: History) {
    setRows(null);
    setHistory(next);
  }

  /** From a doctor's row or the Waiting tile: jump to the queue. */
  function showQueueFor(doctorId: number) {
    setRows(null);
    setDoctorFilter(doctorId);
    setTab("queue");
  }

  async function changeStatus(doctorId: number, status: DoctorStatus) {
    if (!session) return;
    setSavingId(doctorId);
    setProblem("");
    try {
      await api.setStatus(doctorId, status, session.token);
      await refresh(session.token);
    } catch (err) {
      if (err instanceof ApiError) setProblem(err.message);
    } finally {
      setSavingId(null);
    }
  }

  if (!session) return null;

  const doctorName = (id: number) =>
    doctors?.find((doctor) => doctor.id === id)?.name ?? "that doctor";

  return (
    <Shell
      session={session}
      title="Hospital overview"
      subtitle="Who is waiting, which doctors are free, and every appointment."
    >
      {problem && (
        <Banner tone="error" onDismiss={() => setProblem("")}>
          {problem}
        </Banner>
      )}

      {/* ---------------------------------------------------------- tiles */}
      {overview === null ? (
        <Loading label="Loading overview…" />
      ) : (
        <div className="mb-8 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Tile label="Doctors" value={overview.doctors_active} hint={`${overview.doctors_total} total`} />
          <Tile label="Available now" value={overview.doctors_available_now} accent />
          <Tile label="Patients" value={overview.patients} />
          <Tile label="Today" value={overview.appointments_today} hint="appointments" />
          <Tile
            label="Waiting"
            value={overview.appointments_upcoming}
            hint="upcoming · view queue"
            onClick={() => showQueueFor(0)}
          />
          <Tile
            label="By voice"
            value={overview.booked_by_voice}
            hint={`${overview.booked_by_web} by web`}
            accent
          />
        </div>
      )}

      {/* ----------------------------------------------------------- tabs */}
      <div className="mb-4 flex items-center gap-2 border-b border-zinc-200 dark:border-zinc-800">
        {(
          [
            ["queue", "Queue"],
            ["doctors", "Doctors"],
            ["history", "History"],
          ] as [Tab, string][]
        ).map(([name, label]) => (
          <button
            key={name}
            onClick={() => switchTab(name)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition ${
              tab === name
                ? "border-teal-600 font-medium text-zinc-900 dark:border-teal-400 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {label}
          </button>
        ))}
        <div className="ml-auto pb-2">
          <LiveDot live={live} />
        </div>
      </div>

      {/* --------------------------------------------------- list filters */}
      {tab !== "doctors" && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          {tab === "history" &&
            (["past", "cancelled", "all"] as History[]).map((name) => (
              <button
                key={name}
                onClick={() => pickHistory(name)}
                className={`rounded-full border px-3 py-1 text-xs capitalize transition ${
                  history === name
                    ? "border-teal-600 bg-teal-700 text-white dark:border-teal-400 dark:bg-teal-600"
                    : "border-zinc-200 text-zinc-600 hover:bg-zinc-50 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800"
                }`}
              >
                {name}
              </button>
            ))}

          <select
            value={doctorFilter}
            onChange={(event) => pickDoctor(Number(event.target.value))}
            aria-label="Filter by doctor"
            className="ml-auto rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-xs text-zinc-700 outline-none transition focus:border-teal-600 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-300"
          >
            <option value={0}>All doctors</option>
            {(doctors ?? []).map((doctor) => (
              <option key={doctor.id} value={doctor.id}>
                {doctor.name} · {doctor.specialization}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* ---------------------------------------------------------- queue */}
      {tab === "queue" &&
        (rows === null ? (
          <Loading label="Loading the queue…" />
        ) : rows.length === 0 ? (
          <Empty>
            {doctorFilter
              ? `No one is waiting for ${doctorName(doctorFilter)}.`
              : "No one is waiting. Upcoming appointments will appear here."}
          </Empty>
        ) : (
          <QueueByDay rows={rows} />
        ))}

      {/* -------------------------------------------------------- doctors */}
      {tab === "doctors" &&
        (doctors === null ? (
          <Loading />
        ) : (
          <div className="space-y-2">
            {doctors.map((doctor) => {
              const queue = queues.get(doctor.id);
              return (
                <Card key={doctor.id} className="px-4 py-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                        {doctor.name}
                        {!doctor.is_bookable && (
                          <span className="ml-2 text-xs font-normal text-zinc-400">
                            not accepting
                          </span>
                        )}
                      </p>
                      <p className="text-xs text-zinc-500 dark:text-zinc-400">
                        {doctor.specialization} · ₹{doctor.consultation_fee} ·{" "}
                        {doctor.slot_minutes} min
                        {doctor.room ? ` · Room ${doctor.room}` : ""}
                      </p>
                    </div>

                    {queue ? (
                      <button
                        onClick={() => showQueueFor(doctor.id)}
                        className="rounded-md bg-teal-50 px-2 py-1 text-xs font-medium text-teal-800 transition hover:bg-teal-100 dark:bg-teal-500/10 dark:text-teal-300 dark:hover:bg-teal-500/20"
                        title="Show this doctor's queue"
                      >
                        {queue.upcoming} waiting
                        {queue.next_at && (
                          <span className="ml-1 font-normal opacity-75">
                            · next {formatDateTime(queue.next_at)}
                          </span>
                        )}
                      </button>
                    ) : (
                      <span className="text-xs text-zinc-400">no one waiting</span>
                    )}

                    <StatusPill status={doctor.status} />

                    <select
                      value={doctor.status}
                      disabled={savingId === doctor.id}
                      aria-label={`Status for ${doctor.name}`}
                      onChange={(event) =>
                        void changeStatus(doctor.id, event.target.value as DoctorStatus)
                      }
                      className="rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-xs text-zinc-700 outline-none transition focus:border-teal-600 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-300"
                    >
                      {ALL_STATUSES.map((status) => (
                        <option key={status} value={status}>
                          {STATUS_LABEL[status]}
                        </option>
                      ))}
                    </select>
                  </div>
                </Card>
              );
            })}
          </div>
        ))}

      {/* -------------------------------------------------------- history */}
      {tab === "history" &&
        (rows === null ? (
          <Loading />
        ) : rows.length === 0 ? (
          <Empty>Nothing here.</Empty>
        ) : (
          <HistoryTable rows={rows} />
        ))}
    </Shell>
  );
}

/* --------------------------------------------------------------- queue list */

/** Upcoming appointments grouped under a heading per day, soonest first. */
function QueueByDay({ rows }: { rows: Appointment[] }) {
  const days: { key: string; label: string; items: Appointment[] }[] = [];
  for (const appointment of rows) {
    const key = toDateParam(new Date(appointment.start_at));
    let day = days[days.length - 1];
    if (!day || day.key !== key) {
      day = { key, label: formatDay(appointment.start_at), items: [] };
      days.push(day);
    }
    day.items.push(appointment);
  }

  return (
    <div className="space-y-6">
      {days.map((day) => (
        <div key={day.key}>
          <p className="mb-2 text-xs font-medium uppercase tracking-[0.12em] text-zinc-500 dark:text-zinc-400">
            {day.label}
            <span className="ml-2 font-normal normal-case tracking-normal text-zinc-400">
              {day.items.length} appointment{day.items.length === 1 ? "" : "s"}
            </span>
          </p>
          <div className="space-y-2">
            {day.items.map((appointment) => (
              <Card
                key={appointment.id}
                className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3"
              >
                <span className="w-16 shrink-0 text-sm font-medium tabular-nums text-zinc-900 dark:text-zinc-100">
                  {formatTime(appointment.start_at)}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-zinc-900 dark:text-zinc-100">
                    {appointment.patient_name}
                    <span className="mx-2 text-zinc-300 dark:text-zinc-600">→</span>
                    <span className="font-medium">{appointment.doctor_name}</span>
                    <span className="ml-1.5 text-xs text-zinc-400">
                      {appointment.specialization}
                    </span>
                  </p>
                  <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">
                    {appointment.reason || "No reason given"}
                  </p>
                </div>
                <ViaBadge via={appointment.created_via} />
              </Card>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ history table */

function HistoryTable({ rows }: { rows: Appointment[] }) {
  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs uppercase tracking-wider text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
              <th className="px-4 py-2.5 font-medium">When</th>
              <th className="px-4 py-2.5 font-medium">Patient</th>
              <th className="px-4 py-2.5 font-medium">Doctor</th>
              <th className="px-4 py-2.5 font-medium">Reason</th>
              <th className="px-4 py-2.5 font-medium">Via</th>
              <th className="px-4 py-2.5 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((appointment) => (
              <tr
                key={appointment.id}
                className="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60"
              >
                <td className="whitespace-nowrap px-4 py-2.5 tabular-nums text-zinc-700 dark:text-zinc-300">
                  {formatDateTime(appointment.start_at)}
                </td>
                <td className="px-4 py-2.5 text-zinc-900 dark:text-zinc-100">
                  {appointment.patient_name}
                </td>
                <td className="px-4 py-2.5 text-zinc-700 dark:text-zinc-300">
                  {appointment.doctor_name}
                  <span className="ml-1.5 text-xs text-zinc-400">
                    {appointment.specialization}
                  </span>
                </td>
                <td className="max-w-[220px] truncate px-4 py-2.5 text-zinc-500 dark:text-zinc-400">
                  {appointment.reason || "—"}
                </td>
                <td className="px-4 py-2.5">
                  <ViaBadge via={appointment.created_via} />
                </td>
                <td className="px-4 py-2.5">
                  <span
                    className={`text-xs ${
                      appointment.status === "confirmed"
                        ? "text-emerald-700 dark:text-emerald-400"
                        : "text-zinc-400"
                    }`}
                  >
                    {appointment.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ pieces */

function ViaBadge({ via }: { via: Appointment["created_via"] }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
        via === "voice"
          ? "bg-teal-50 text-teal-700 dark:bg-teal-500/10 dark:text-teal-400"
          : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
      }`}
    >
      {via}
    </span>
  );
}

function Tile({
  label,
  value,
  hint,
  accent,
  onClick,
}: {
  label: string;
  value: number;
  hint?: string;
  accent?: boolean;
  onClick?: () => void;
}) {
  const body = (
    <>
      <p className="text-xs uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        {label}
      </p>
      <p
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          accent ? "text-teal-700 dark:text-teal-400" : "text-zinc-900 dark:text-zinc-100"
        }`}
      >
        {value}
      </p>
      {hint && <p className="text-xs text-zinc-400 dark:text-zinc-500">{hint}</p>}
    </>
  );

  if (!onClick) return <Card className="px-4 py-3">{body}</Card>;

  return (
    <button
      onClick={onClick}
      className="rounded-lg border border-zinc-200 bg-white px-4 py-3 text-left transition hover:border-teal-600/50 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-teal-400/50"
    >
      {body}
    </button>
  );
}
