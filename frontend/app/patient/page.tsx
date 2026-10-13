// frontend/app/patient/page.tsx
//
// Find a doctor, see what's actually free, book it.
//
// Two things here are worth noticing. Doctor availability updates without a
// refresh — Supabase Realtime pushes `doctor_status` changes straight to the
// browser, so a doctor going into surgery empties this screen while you are
// looking at it. And when a booking fails, the ranked alternates the backend
// returns are shown as one-click options: the same recovery the voice agent
// reads out loud, from the same endpoint.

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  type Alternate,
  type Appointment,
  type Doctor,
  type Slot,
} from "@/lib/api";
import { formatDateTime, formatDay, formatTime, toDateParam, upcomingDays } from "@/lib/format";
import { subscribeToDoctorStatus } from "@/lib/realtime";
import { useSession } from "@/lib/session";
import {
  Banner,
  Button,
  Card,
  Empty,
  LiveDot,
  Loading,
  SectionTitle,
  Shell,
  StatusPill,
} from "@/components/ui";

export default function PatientPage() {
  const session = useSession("patient");

  const [specializations, setSpecializations] = useState<string[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [doctors, setDoctors] = useState<Doctor[] | null>(null);

  const [selected, setSelected] = useState<Doctor | null>(null);
  const [day, setDay] = useState<string>(toDateParam(new Date()));
  const [slots, setSlots] = useState<Slot[] | null>(null);
  const [reason, setReason] = useState("");

  const [appointments, setAppointments] = useState<Appointment[] | null>(null);
  const [ok, setOk] = useState("");
  const [problem, setProblem] = useState("");
  const [alternates, setAlternates] = useState<Alternate[]>([]);
  const [live, setLive] = useState(false);
  const [booking, setBooking] = useState(false);

  // The realtime callback needs the current selection, but re-subscribing on
  // every click would churn the channel. Keep it in refs, synced in an effect —
  // writing to a ref during render is impure.
  const selectedRef = useRef<Doctor | null>(null);
  const dayRef = useRef(day);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  useEffect(() => {
    dayRef.current = day;
  }, [day]);

  /* ------------------------------------------------------------- loaders */

  const loadDoctors = useCallback(async (specialization: string) => {
    const found = await api.searchDoctors(
      specialization ? { specialization } : {},
    );
    setDoctors(found);
    return found;
  }, []);

  const loadSlots = useCallback(async (doctorId: number, on: string) => {
    const result = await api.doctorSlots(doctorId, on);
    setSlots(result.slots);
    return result;
  }, []);

  const loadAppointments = useCallback(async (token: string) => {
    setAppointments(await api.myAppointments(token));
  }, []);

  /* --------------------------------------------------------------- setup */

  useEffect(() => {
    if (!session) return;
    void (async () => {
      await api.specializations().then(setSpecializations).catch(() => {});
      await loadDoctors("").catch((err: ApiError) => setProblem(err.message));
      await loadAppointments(session.token).catch(() => {});
    })();
  }, [session, loadDoctors, loadAppointments]);

  // Live availability. A status change repaints the doctor list, and reloads
  // the slots if it was the doctor currently open.
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

      const open = selectedRef.current;
      if (open && open.id === change.doctorId) {
        setSelected((current) =>
          current
            ? {
                ...current,
                status: change.status,
                is_bookable: change.status === "available",
              }
            : current,
        );
        void loadSlots(open.id, dayRef.current).catch(() => {});
        setOk("");
        setProblem(
          `${open.name} is now ${change.status.replace("_", " ")} — availability updated.`,
        );
      }
    }, setLive);
    return unsubscribe;
  }, [session, loadSlots]);

  /* -------------------------------------------------------------- actions */

  async function openDoctor(doctor: Doctor) {
    setSelected(doctor);
    setSlots(null);
    setAlternates([]);
    setProblem("");
    setOk("");
    await loadSlots(doctor.id, day).catch((err: ApiError) =>
      setProblem(err.message),
    );
  }

  async function pickDay(nextDay: string) {
    setDay(nextDay);
    setAlternates([]);
    if (!selected) return;
    setSlots(null);
    await loadSlots(selected.id, nextDay).catch((err: ApiError) =>
      setProblem(err.message),
    );
  }

  async function book(slotId: number) {
    if (!session) return;
    setBooking(true);
    setProblem("");
    setOk("");
    setAlternates([]);
    try {
      const appointment = await api.book(slotId, session.token, reason || undefined);
      setOk(
        `Booked with ${appointment.doctor_name}, ${formatDateTime(appointment.start_at)}.`,
      );
      setReason("");
      await Promise.all([
        loadAppointments(session.token),
        selected ? loadSlots(selected.id, day) : Promise.resolve(),
      ]);
    } catch (err) {
      if (err instanceof ApiError) {
        setProblem(err.message);
        setAlternates(err.alternates);
        // The slot list is stale the moment a booking is refused.
        if (selected) void loadSlots(selected.id, day).catch(() => {});
      }
    } finally {
      setBooking(false);
    }
  }

  async function cancel(appointmentId: number) {
    if (!session) return;
    try {
      await api.cancel(appointmentId, session.token);
      setOk("Appointment cancelled.");
      await loadAppointments(session.token);
      if (selected) void loadSlots(selected.id, day).catch(() => {});
    } catch (err) {
      if (err instanceof ApiError) setProblem(err.message);
    }
  }

  if (!session) return null;

  return (
    <Shell
      session={session}
      title={`Hello, ${session.name.split(" ")[0]}`}
      subtitle="Find a doctor and book an appointment."
    >
      {ok && (
        <Banner tone="ok" onDismiss={() => setOk("")}>
          {ok}
        </Banner>
      )}
      {problem && (
        <Banner tone="warn" onDismiss={() => setProblem("")}>
          <p>{problem}</p>
          {alternates.length > 0 && (
            <div className="mt-3">
              <p className="mb-2 text-xs uppercase tracking-[0.12em] opacity-70">
                Next best options
              </p>
              <div className="flex flex-wrap gap-2">
                {alternates.map((alternate) => (
                  <button
                    key={alternate.slot_id}
                    onClick={() => void book(alternate.slot_id)}
                    disabled={booking}
                    className="rounded-md border border-current/25 bg-white/60 px-3 py-1.5 text-left text-xs transition hover:bg-white disabled:opacity-50 dark:bg-white/5 dark:hover:bg-white/10"
                  >
                    <span className="block font-medium">
                      {alternate.doctor_name}
                    </span>
                    <span className="block opacity-70">
                      {formatDateTime(alternate.start_at)} · {alternate.why}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </Banner>
      )}

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        {/* ------------------------------------------------------ doctors */}
        <div>
          <div className="mb-3 flex items-center justify-between">
            <SectionTitle>Doctors</SectionTitle>
            <LiveDot live={live} />
          </div>

          <div className="mb-3 flex flex-wrap gap-1.5">
            <FilterChip
              label="All"
              active={filter === ""}
              onClick={() => {
                setFilter("");
                void loadDoctors("");
              }}
            />
            {specializations.map((specialization) => (
              <FilterChip
                key={specialization}
                label={specialization}
                active={filter === specialization}
                onClick={() => {
                  setFilter(specialization);
                  void loadDoctors(specialization);
                }}
              />
            ))}
          </div>

          {doctors === null ? (
            <Loading label="Loading doctors…" />
          ) : doctors.length === 0 ? (
            <Empty>No doctors in that department.</Empty>
          ) : (
            <div className="space-y-2">
              {doctors.map((doctor) => (
                <button
                  key={doctor.id}
                  onClick={() => void openDoctor(doctor)}
                  className={`w-full rounded-lg border px-4 py-3 text-left transition ${
                    selected?.id === doctor.id
                      ? "border-teal-600 bg-teal-50/60 dark:border-teal-400/60 dark:bg-teal-500/10"
                      : "border-zinc-200 bg-white hover:border-zinc-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-700"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                        {doctor.name}
                      </p>
                      <p className="text-xs text-zinc-500 dark:text-zinc-400">
                        {doctor.specialization} · ₹{doctor.consultation_fee}
                      </p>
                    </div>
                    <StatusPill status={doctor.status} />
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* -------------------------------------------------------- slots */}
        <div>
          <SectionTitle>Available times</SectionTitle>

          {!selected ? (
            <Empty>Pick a doctor to see their free appointment times.</Empty>
          ) : (
            <Card className="p-5">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="font-medium text-zinc-900 dark:text-zinc-100">
                    {selected.name}
                  </p>
                  <p className="text-xs text-zinc-500 dark:text-zinc-400">
                    {selected.qualification}
                    {selected.room ? ` · Room ${selected.room}` : ""} ·{" "}
                    {selected.slot_minutes} min
                  </p>
                </div>
                <StatusPill status={selected.status} />
              </div>

              <div className="mb-4 flex gap-1.5 overflow-x-auto pb-1">
                {upcomingDays(7).map((date) => {
                  const value = toDateParam(date);
                  return (
                    <button
                      key={value}
                      onClick={() => void pickDay(value)}
                      className={`shrink-0 rounded-md border px-3 py-1.5 text-xs transition ${
                        day === value
                          ? "border-teal-600 bg-teal-700 text-white dark:border-teal-400 dark:bg-teal-600"
                          : "border-zinc-200 text-zinc-600 hover:bg-zinc-50 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800"
                      }`}
                    >
                      {formatDay(date)}
                    </button>
                  );
                })}
              </div>

              {slots === null ? (
                <Loading label="Checking availability…" />
              ) : slots.length === 0 ? (
                <Empty>
                  {selected.is_bookable
                    ? `${selected.name} has nothing free on ${formatDay(day)}.`
                    : `${selected.name} isn't taking appointments right now.`}
                </Empty>
              ) : (
                <>
                  <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5">
                    {slots.map((slot) => (
                      <button
                        key={slot.id}
                        onClick={() => void book(slot.id)}
                        disabled={booking}
                        className="rounded-md border border-zinc-200 bg-white px-2 py-2 text-sm text-zinc-700 transition hover:border-teal-600 hover:bg-teal-50 hover:text-teal-900 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-300 dark:hover:border-teal-400 dark:hover:bg-teal-500/10 dark:hover:text-teal-200"
                      >
                        {formatTime(slot.start_at)}
                      </button>
                    ))}
                  </div>

                  <div className="mt-4">
                    <label
                      htmlFor="reason"
                      className="mb-1.5 block text-xs text-zinc-500 dark:text-zinc-400"
                    >
                      Reason for the visit (optional)
                    </label>
                    <input
                      id="reason"
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      placeholder="e.g. chest pain"
                      className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm outline-none transition placeholder:text-zinc-400 focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
                    />
                  </div>
                </>
              )}
            </Card>
          )}

          {/* ------------------------------------------- my appointments */}
          <div className="mt-8">
            <SectionTitle>My appointments</SectionTitle>
            {appointments === null ? (
              <Loading />
            ) : appointments.length === 0 ? (
              <Empty>Nothing booked yet.</Empty>
            ) : (
              <div className="space-y-2">
                {appointments.map((appointment) => (
                  <Card
                    key={appointment.id}
                    className="flex flex-wrap items-center gap-3 px-4 py-3"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                        {appointment.doctor_name}
                        <span className="ml-2 text-xs font-normal text-zinc-500 dark:text-zinc-400">
                          {appointment.specialization}
                        </span>
                      </p>
                      <p className="text-xs text-zinc-500 dark:text-zinc-400">
                        {formatDateTime(appointment.start_at)}
                        {appointment.reason ? ` · ${appointment.reason}` : ""}
                      </p>
                    </div>
                    {appointment.created_via === "voice" && (
                      <span className="rounded bg-teal-50 px-1.5 py-0.5 text-[11px] font-medium text-teal-700 dark:bg-teal-500/10 dark:text-teal-400">
                        by voice
                      </span>
                    )}
                    <Button
                      variant="danger"
                      onClick={() => void cancel(appointment.id)}
                    >
                      Cancel
                    </Button>
                  </Card>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition ${
        active
          ? "border-teal-600 bg-teal-700 text-white dark:border-teal-400 dark:bg-teal-600"
          : "border-zinc-200 text-zinc-600 hover:bg-zinc-50 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800"
      }`}
    >
      {label}
    </button>
  );
}