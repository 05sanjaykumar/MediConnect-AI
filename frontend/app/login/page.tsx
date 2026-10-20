// frontend/app/login/page.tsx

"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, api } from "@/lib/api";
import { homeFor, loadSession, saveSession } from "@/lib/session";
import { Banner, Button } from "@/components/ui";

/** Seeded by backend/seed.py. One click each, so a demo never stalls on typing. */
const DEMO_ACCOUNTS = [
  { label: "Patient", name: "Sanjay Kumar", phone: "9840012346" },
  { label: "Doctor", name: "Dr. Anitha Sharma", phone: "9840020000" },
  { label: "Admin", name: "Hospital Admin", phone: "9840000000" },
];

const DEMO_PASSWORD = "demo1234";

export default function LoginPage() {
  const router = useRouter();
  // Creating an account is a form, not a conversation. Reading a phone number
  // out to the voice agent was unreliable — speech recognition mangles spoken
  // digits, and the guard that refuses a number the caller never clearly said
  // then (correctly) rejects it. Typing it once is simply better.
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Already signed in? Go straight to the right dashboard.
  useEffect(() => {
    const existing = loadSession();
    if (existing) router.replace(homeFor(existing.role));
  }, [router]);

  async function signIn(withPhone: string, withPassword: string) {
    setBusy(true);
    setError("");
    try {
      const login = await api.login(withPhone, withPassword);
      // /auth/me carries doctor_id, which a doctor needs to change their status.
      const profile = await api.me(login.access_token).catch(() => undefined);
      saveSession(login, profile);
      router.replace(homeFor(login.role));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Something went wrong. Try again.",
      );
      setBusy(false);
    }
  }

  async function signUp() {
    setBusy(true);
    setError("");
    try {
      const login = await api.register(name.trim(), phone.trim(), password);
      const profile = await api.me(login.access_token).catch(() => undefined);
      saveSession(login, profile);
      router.replace(homeFor(login.role));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Something went wrong. Try again.",
      );
      setBusy(false);
    }
  }

  const signingUp = mode === "signup";

  return (
    <div className="flex min-h-full items-center justify-center px-5 py-16">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="flex items-baseline justify-center gap-2">
            <span className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
              MediConnect
            </span>
            <span className="text-xs font-medium uppercase tracking-[0.14em] text-teal-700 dark:text-teal-400">
              AI
            </span>
          </div>
          <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400">
            {signingUp
              ? "Create an account, then book by voice or on the web."
              : "Sign in with the phone number on your record."}
          </p>
        </div>

        {error && <Banner tone="error">{error}</Banner>}

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void (signingUp ? signUp() : signIn(phone, password));
          }}
          className="space-y-4 rounded-lg border border-zinc-200 bg-white p-6 dark:border-zinc-800 dark:bg-zinc-900"
        >
          {signingUp && (
            <div>
              <label
                htmlFor="name"
                className="mb-1.5 block text-sm font-medium text-zinc-700 dark:text-zinc-300"
              >
                Full name
              </label>
              <input
                id="name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoComplete="name"
                placeholder="Arun Chauhan"
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none transition placeholder:text-zinc-400 focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
              />
            </div>
          )}

          <div>
            <label
              htmlFor="phone"
              className="mb-1.5 block text-sm font-medium text-zinc-700 dark:text-zinc-300"
            >
              Phone number
            </label>
            <input
              id="phone"
              value={phone}
              onChange={(event) => setPhone(event.target.value)}
              inputMode="numeric"
              autoComplete="tel"
              placeholder="9840012346"
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none transition placeholder:text-zinc-400 focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
            />
          </div>

          <div>
            <label
              htmlFor="password"
              className="mb-1.5 block text-sm font-medium text-zinc-700 dark:text-zinc-300"
            >
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={signingUp ? "new-password" : "current-password"}
              placeholder="••••••••"
              className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none transition placeholder:text-zinc-400 focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
            />
          </div>

          <Button
            type="submit"
            disabled={busy || !phone || !password || (signingUp && name.trim().length < 2)}
            className="w-full"
          >
            {busy
              ? signingUp ? "Creating…" : "Signing in…"
              : signingUp ? "Create account" : "Sign in"}
          </Button>

          <p className="text-center text-xs text-zinc-500 dark:text-zinc-400">
            {signingUp ? "Already registered?" : "New patient?"}{" "}
            <button
              type="button"
              onClick={() => {
                setMode(signingUp ? "signin" : "signup");
                setError("");
              }}
              className="font-medium text-teal-700 underline-offset-2 hover:underline dark:text-teal-400"
            >
              {signingUp ? "Sign in" : "Create an account"}
            </button>
          </p>
        </form>

        <div className="mt-6">
          <p className="mb-2 text-center text-xs uppercase tracking-[0.12em] text-zinc-400 dark:text-zinc-500">
            Demo accounts
          </p>
          <div className="grid grid-cols-3 gap-2">
            {DEMO_ACCOUNTS.map((account) => (
              <button
                key={account.phone}
                disabled={busy}
                onClick={() => void signIn(account.phone, DEMO_PASSWORD)}
                className="rounded-md border border-zinc-200 bg-white px-2 py-2 text-center transition hover:border-teal-600/40 hover:bg-teal-50 disabled:opacity-50 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-teal-400/40 dark:hover:bg-teal-500/10"
              >
                <span className="block text-xs font-medium text-zinc-900 dark:text-zinc-100">
                  {account.label}
                </span>
                <span className="mt-0.5 block truncate text-[11px] text-zinc-500 dark:text-zinc-400">
                  {account.name}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
