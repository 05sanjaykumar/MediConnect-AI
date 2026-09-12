// frontend/lib/api.ts
//
// The one place the dashboards talk to the backend. Every call goes through
// `request`, so the bearer token and the error shape are handled once.
//
// The backend's booking failures are not plain strings: routes/appointments.py
// raises `detail: { code, message, alternates }`, and those alternates are the
// same ranked suggestions the voice agent reads out. ApiError carries them
// through so the patient dashboard can offer the same recovery a caller gets.

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type Role = "patient" | "doctor" | "admin";

export type DoctorStatus =
  | "available"
  | "busy"
  | "in_surgery"
  | "on_break"
  | "off_duty";

export interface Doctor {
  id: number;
  name: string;
  specialization: string;
  qualification: string;
  consultation_fee: number;
  slot_minutes: number;
  room: string | null;
  status: DoctorStatus;
  is_bookable: boolean;
}

export interface Slot {
  id: number;
  doctor_id: number;
  start_at: string;
  end_at: string;
  state: string;
}

export interface DoctorSlots {
  doctor_id: number;
  doctor_name: string;
  specialization: string;
  status: DoctorStatus;
  date: string;
  slots: Slot[];
}

export interface Appointment {
  id: number;
  slot_id: number;
  patient_id: number;
  patient_name: string;
  doctor_id: number;
  doctor_name: string;
  specialization: string;
  start_at: string;
  end_at: string;
  status: string;
  created_via: "web" | "voice";
  reason: string | null;
  created_at: string;
}

export interface Alternate {
  doctor_id: number;
  doctor_name: string;
  specialization: string;
  slot_id: number;
  start_at: string;
  score: number;
  why: string;
}

export interface StatusResult {
  doctor_id: number;
  doctor_name: string;
  from_status: DoctorStatus | null;
  status: DoctorStatus;
  note: string | null;
  updated_at: string;
  slots_blocked: number;
  slots_released: number;
  appointments_needing_attention: number[];
}

export interface AdminOverview {
  doctors_total: number;
  doctors_active: number;
  doctors_available_now: number;
  patients: number;
  appointments_upcoming: number;
  appointments_today: number;
  booked_by_voice: number;
  booked_by_web: number;
}

export interface LoginResult {
  access_token: string;
  token_type: string;
  user_id: number;
  name: string;
  role: Role;
}

export interface Profile {
  id: number;
  name: string;
  phone: string;
  role: Role;
  doctor_id?: number;
  specialization?: string;
}

export interface WorkingHour {
  weekday: number;
  start_time: string;
  end_time: string;
}

/** An error with the backend's own code and, when a booking failed, its alternates. */
export class ApiError extends Error {
  status: number;
  code?: string;
  alternates: Alternate[];

  constructor(
    status: number,
    message: string,
    code?: string,
    alternates: Alternate[] = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.alternates = alternates;
  }
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; token?: string | null } = {},
): Promise<T> {
  const { method = "GET", body, token } = options;

  let response: Response;
  try {
    response = await fetch(`${BASE}/api${path}`, {
      method,
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // fetch rejects when the response never arrives *or* can't be read. The
    // second case is easy to misread: FastAPI's CORS middleware doesn't add
    // headers to an unhandled 500, so the browser blocks that response and
    // this looks identical to the server being down. Say both.
    throw new ApiError(
      0,
      "No response from the server. It may be down, or a request failed — " +
        "check the backend log.",
    );
  }

  if (response.status === 204) return undefined as T;

  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    const detail = (payload as { detail?: unknown } | null)?.detail;

    // Booking failures carry a structured detail; everything else is a string.
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const structured = detail as {
        code?: string;
        message?: string;
        alternates?: Alternate[];
      };
      throw new ApiError(
        response.status,
        structured.message ?? "That didn't work.",
        structured.code,
        structured.alternates ?? [],
      );
    }

    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : `Request failed (${response.status})`,
    );
  }

  return payload as T;
}

export const api = {
  login: (phone: string, password: string) =>
    request<LoginResult>("/auth/login", {
      method: "POST",
      body: { phone, password },
    }),

  me: (token: string) => request<Profile>("/auth/me", { token }),

  // ---------------------------------------------------------------- doctors

  searchDoctors: (params: { specialization?: string; name?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.specialization) query.set("specialization", params.specialization);
    if (params.name) query.set("name", params.name);
    const suffix = query.toString() ? `?${query}` : "";
    return request<Doctor[]>(`/doctors${suffix}`);
  },

  specializations: () => request<string[]>("/doctors/specializations"),

  doctor: (doctorId: number) => request<Doctor>(`/doctors/${doctorId}`),

  doctorSlots: (doctorId: number, on?: string) =>
    request<DoctorSlots>(`/doctors/${doctorId}/slots${on ? `?on=${on}` : ""}`),

  setStatus: (
    doctorId: number,
    status: DoctorStatus,
    token: string,
    note?: string,
  ) =>
    request<StatusResult>(`/doctors/${doctorId}/status`, {
      method: "PATCH",
      body: { status, note: note ?? null },
      token,
    }),

  // ----------------------------------------------------------- appointments

  book: (slotId: number, token: string, reason?: string) =>
    request<Appointment>("/appointments", {
      method: "POST",
      body: { slot_id: slotId, reason: reason ?? null, created_via: "web" },
      token,
    }),

  myAppointments: (token: string, includePast = false) =>
    request<Appointment[]>(
      `/appointments/mine${includePast ? "?include_past=true" : ""}`,
      { token },
    ),

  cancel: (appointmentId: number, token: string) =>
    request<Appointment>(`/appointments/${appointmentId}`, {
      method: "DELETE",
      token,
    }),

  reschedule: (appointmentId: number, newSlotId: number, token: string) =>
    request<Appointment>(`/appointments/${appointmentId}`, {
      method: "PATCH",
      body: { new_slot_id: newSlotId },
      token,
    }),

  // ------------------------------------------------------------------ admin

  overview: (token: string) => request<AdminOverview>("/admin/overview", { token }),

  allDoctors: (token: string) => request<Doctor[]>("/admin/doctors", { token }),

  allAppointments: (token: string, limit = 100) =>
    request<Appointment[]>(`/admin/appointments?limit=${limit}`, { token }),

  workingHours: (doctorId: number, token: string) =>
    request<WorkingHour[]>(`/admin/doctors/${doctorId}/working-hours`, { token }),
};
