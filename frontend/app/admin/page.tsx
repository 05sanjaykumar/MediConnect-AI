// frontend/app/admin/page.tsx
//
// The hospital-wide view: the numbers across the top, every doctor's live
// status, and every appointment.
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
  type Doctor,
  type DoctorStatus,
} from "@/lib/api";
import { ALL_STATUSES, STATUS_LABEL, formatDateTime } from "@/lib/format";
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

type Tab = "doctors" | "appointments";

export default function AdminPage() {
  const session = useSession("admin");

  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [doctors, setDoctors] = useState<Doctor[] | null>(null);
  const [appointments, setAppointments] = useState<Appointment[] | null>(null);
  const [tab, setTab] = useState<Tab>("doctors");
  const [problem, setProblem] = useState("");
  const [live, setLive] = useState(false);
  const [savingId, setSavingId] = useState<number | null>(null);

  const refresh = useCallback(async (token: string) => {
    const [nextOverview, nextDoctors, nextAppointments] = await Promise.all([
      api.overview(token),
      api.allDoctors(token),
      api.allAppointments(token, 100),
    ]);
    setOverview(nextOverview);
    setDoctors(nextDoctors);
    setAppointments(nextAppointments);
  }, []);

  useEffect(() => {
    if (!session) return;
    void (async () => {
      await refresh(session.token).catch((err: ApiError) => setProblem(err.message));
    })();
  }, [session, refresh]);

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
      // The "available now" tile is derived from status, so refresh it too.
      void api.overview(session.token).then(setOverview).catch(() => {});
    }, setLive);
    return unsubscribe;
  }, [session]);

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

  return (
    <Shell
      session={session}
      title="Hospital overview"
      subtitle="Doctors, availability and every appointment booked."
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
          <Tile
            label="Available now"
            value={overview.doctors_available_now}
            accent
          />
          <Tile label="Patients" value={overview.patients} />
          <Tile label="Today" value={overview.appointments_today} hint="appointments" />
          <Tile label="Upcoming" value={overview.appointments_upcoming} />
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
        {(["doctors", "appointments"] as Tab[]).map((name) => (
          <button
            key={name}
            onClick={() => setTab(name)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm capitalize transition ${
              tab === name
                ? "border-teal-600 font-medium text-zinc-900 dark:border-teal-400 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {name}
          </button>
        ))}
        <div className="ml-auto pb-2">
          <LiveDot live={live} />
        </div>
      </div>

      {/* -------------------------------------------------------- doctors */}
      {tab === "doctors" &&
        (doctors === null ? (
          <Loading />
        ) : (
          <div className="space-y-2">
            {doctors.map((doctor) => (
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

                  <StatusPill status={doctor.status} />

                  <select
                    value={doctor.status}
                    disabled={savingId === doctor.id}
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
            ))}
          </div>
        ))}

      {/* --------------------------------------------------- appointments */}
      {tab === "appointments" &&
        (appointments === null ? (
          <Loading />
        ) : appointments.length === 0 ? (
          <Empty>No appointments yet.</Empty>
        ) : (
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
                  {appointments.map((appointment) => (
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
                        <span
                          className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                            appointment.created_via === "voice"
                              ? "bg-teal-50 text-teal-700 dark:bg-teal-500/10 dark:text-teal-400"
                              : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
                          }`}
                        >
                          {appointment.created_via}
                        </span>
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
        ))}
    </Shell>
  );
}

function Tile({
  label,
  value,
  hint,
  accent,
}: {
  label: string;
  value: number;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <Card className="px-4 py-3">
      <p className="text-xs uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        {label}
      </p>
      <p
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          accent
            ? "text-teal-700 dark:text-teal-400"
            : "text-zinc-900 dark:text-zinc-100"
        }`}
      >
        {value}
      </p>
      {hint && (
        <p className="text-xs text-zinc-400 dark:text-zinc-500">{hint}</p>
      )}
    </Card>
  );
}
