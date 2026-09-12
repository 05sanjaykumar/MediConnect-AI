// frontend/lib/session.ts
//
// Who is signed in, kept in sessionStorage. The JWT the backend issues lasts
// 12 hours, so there is nothing to refresh — if a call comes back 401 the
// session is simply dropped and the user signs in again.
//
// The session is read with useSyncExternalStore rather than an effect. That
// matters for two reasons: storage doesn't exist while the page is being
// rendered on the server, and reading it in an effect would both cause a
// cascading render and hydrate a signed-out shell before swapping it out.
//
// It lives in sessionStorage, not localStorage, and that is deliberate: the
// demo is two windows side by side, a patient watching a doctor go into
// surgery. localStorage is shared across tabs, so signing in as the doctor
// would sign the patient out. sessionStorage is per-tab, so both can be open
// at once. The cost is that closing the tab ends the session, which for this
// app is the behaviour you want anyway.

"use client";

import { useEffect, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import type { LoginResult, Profile, Role } from "./api";

const KEY = "mediconnect.session";

export interface Session {
  token: string;
  userId: number;
  name: string;
  role: Role;
  /** Only for doctor accounts — needed to change their own status. */
  doctorId?: number;
  specialization?: string;
}

/* ------------------------------------------------------------------ store */

// useSyncExternalStore compares snapshots with Object.is, so parsing on every
// read would loop forever. Cache by the raw string and only re-parse on change.
let cachedRaw: string | null = null;
let cachedSession: Session | null = null;

function readSession(): Session | null {
  const raw = sessionStorage.getItem(KEY);
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    try {
      cachedSession = raw ? (JSON.parse(raw) as Session) : null;
    } catch {
      cachedSession = null;
    }
  }
  return cachedSession;
}

/** Sign-in and sign-out within this tab. sessionStorage is never shared. */
const listeners = new Set<() => void>();

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

function notify() {
  listeners.forEach((listener) => listener());
}

/** There is no session while rendering on the server. */
const serverSnapshot = () => null;

/** `false` during SSR and hydration, `true` once running in the browser. */
const neverChanges = () => () => {};
const clientTrue = () => true;
const serverFalse = () => false;

/* ------------------------------------------------------------------- api */

export function saveSession(login: LoginResult, profile?: Profile): Session {
  const session: Session = {
    token: login.access_token,
    userId: login.user_id,
    name: login.name,
    role: login.role,
    doctorId: profile?.doctor_id,
    specialization: profile?.specialization,
  };
  sessionStorage.setItem(KEY, JSON.stringify(session));
  notify();
  return session;
}

export function loadSession(): Session | null {
  if (typeof window === "undefined") return null;
  return readSession();
}

export function clearSession() {
  sessionStorage.removeItem(KEY);
  notify();
}

/** Where each role lands after signing in. */
export function homeFor(role: Role): string {
  if (role === "doctor") return "/doctor";
  if (role === "admin") return "/admin";
  return "/patient";
}

/**
 * Read the session, and bounce anyone who shouldn't be here.
 *
 * Returns `undefined` until the browser has taken over from the server —
 * pages use that to render nothing rather than flashing a signed-out state.
 */
export function useSession(requiredRole?: Role) {
  const router = useRouter();
  const hydrated = useSyncExternalStore(neverChanges, clientTrue, serverFalse);
  const session = useSyncExternalStore(subscribe, readSession, serverSnapshot);

  const allowed = !requiredRole || session?.role === requiredRole;

  useEffect(() => {
    if (!hydrated) return;
    if (!session) {
      router.replace("/login");
    } else if (!allowed) {
      router.replace(homeFor(session.role));
    }
  }, [hydrated, session, allowed, router]);

  if (!hydrated) return undefined;
  if (!session || !allowed) return null;
  return session;
}
