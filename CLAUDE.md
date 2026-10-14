# MediConnect AI — handoff for the next session

VIT Chennai capstone (BCSE497J). Team: Sanjay Kumar (owner of this repo), Ananya Kumari, Riya Senju.
A patient books a doctor's appointment **by voice** (browser mic now, phone via Twilio later) or on
the web; doctors flip a live status that changes availability for everyone at once.

**Scope expectation: this is a demo for a college panel, not a production app.** The user repeatedly
asked to keep things simple. Don't propose infrastructure (Docker, Redis, CI, monitoring) unprompted.

`README.md` is the human setup guide. `PLAN.md` is the original, now partly outdated, design doc.
This file is the ground truth for how the code actually works and what's left.

---

## Working with this user

- **Answer questions without editing.** If they ask "what do you think / is X done?", investigate and
  answer. They have explicitly pushed back when code was changed in response to a question.
- **Confirm before deleting data** in Supabase. Show exactly what will be removed first.
- They test by watching the uvicorn log. When they report a problem, read `/tmp/mc.log` (or wherever
  the server was started) before theorising.
- They merge PRs fast. Always branch from an up-to-date `main` per PR; a commit pushed to a branch
  whose PR was already merged is silently lost (happened twice).
- Credentials live in `backend/.env` and the user has said not to worry about them being exposed.
- Verify claims against the code/DB rather than memory — several wrong assumptions were caught that way.

---

## Running it

```bash
# backend — Python 3.11 venv (3.14 fails: torch/kokoro wheels). Run from backend/, imports are flat.
cd backend && .venv/bin/python -m uvicorn main:app --reload --port 8000

# frontend — Next.js 16
cd frontend && npm run dev
```

| | |
|---|---|
| Voice screen | http://localhost:3000 |
| Dashboards | http://localhost:3000/login |
| API docs | http://localhost:8000/docs |

Demo accounts, password `demo1234` (one-click buttons on the login page):
patient `9840012346` Sanjay Kumar · patient `9840012347` Riya · doctor `9840020000` Dr. Anitha Sharma
(Cardiology, doctor_id 1) · admin `9840000000`.

Stop both: `pkill -f "uvicorn main:app"; pkill -f "next dev"`

Tests (5–8 min — every query is a round trip to Sydney — and they hit the **real** Supabase DB):
```bash
cd backend && .venv/bin/python -m pytest tests -q
```

---

## Stack

- **Backend**: FastAPI, SQLAlchemy 2.0 (sync), Alembic, psycopg 3 → **Supabase Postgres** (region
  `ap-southeast-2`, Sydney — every round trip ≈ 0.46s from India; this drives many design choices).
- **Voice**: Pipecat 1.8 · STT NVIDIA `nemotron-asr-streaming` · LLM Groq `openai/gpt-oss-120b` ·
  TTS Kokoro (local ONNX, needs `espeak-ng`) · Silero VAD.
- **Frontend**: Next.js 16 (App Router, Turbopack), React 19.2, Tailwind 4, `@pipecat-ai/client-js`
  1.13, `@supabase/supabase-js` for Realtime.
- Voice code started as the user's own earlier repo **Gensail Voice AI** (`05sanjaykumar/Gensail-Voice-AI`).

---

## Code map

```
backend/
  main.py              app, routers, pool warm-up on startup, voice routes optional if pipecat missing
  config.py            model names from env (LLM_MODEL, LLM_REASONING_EFFORT, USER_SPEECH_TIMEOUT…)
  database.py          engine: prepare_threshold=None (pgbouncer), pool 10+10
  models.py            users, doctors, working_hours, doctor_status, slots, appointments, status_events
  schemas.py           Pydantic models
  security.py          stdlib pbkdf2 + hand-rolled HS256 JWT (12h)
  deps.py              get_current_user, require_role, current_doctor
  seed.py              12 doctors / 6 specializations / 5 patients / admin
  routes/auth.py       login, register, /me (includes doctor_id + phone)
  routes/doctors.py    search, slots, next-available, PATCH status
  routes/appointments.py book (409 carries ranked alternates), reschedule, cancel, mine
  routes/admin.py      overview, doctors CRUD, working hours, appointments?scope=, /admin/queue
  routes/audio.py      voice WebSocket; identify_caller; build_pipeline; ACTIVE_CALLS registry
  routes/telephony.py  Twilio: TwiML webhook, media-stream WS, outbound call, status
  tools/fake_twilio_call.py  fakes a Twilio call locally — test the phone path for free
  services/availability.py  what each status blocks and for how long
  services/slot_engine.py   working hours → slots, generated lazily & batched
  services/booking.py       book/cancel/reschedule — the core; the concurrency guarantee
  services/fallback.py      deterministic ranking of alternates (NOT an LLM)
  services/status.py        status change → block/release slots → status_events
  services/LLM.py STT.py TTS.py  Pipecat services; TTS.py has ClauseTextAggregator
  agents/prompt.py     system prompt, greeting_for(), describe_caller()
  agents/tools.py      7 voice tools + VoiceSession + guards
  tests/conftest.py    leave_no_trace fixture (see Gotchas)
  alembic/             migrations (schema owned by Alembic, app never create_all's)

frontend/
  app/page.tsx         voice call screen (dark); passes signed-in phone as caller id
  app/login/ patient/ doctor/ admin/   dashboards
  lib/api.ts           typed client; ApiError carries backend code + alternates
  lib/session.ts       per-tab session in sessionStorage via useSyncExternalStore
  lib/realtime.ts      Supabase Realtime subscription to doctor_status
  lib/format.ts        dates, status labels/colours
  components/ui.tsx    Shell, Card, StatusPill, Banner, LiveDot…
```

---

## Design decisions that aren't obvious from the code

**Booking safety (the thing to defend at the panel).** Two guards: an app-level check that produces
friendly errors, and a partial unique index `appointments(slot_id) WHERE status='confirmed'` that makes
double-booking impossible. `_lock_slot` uses `SELECT … FOR UPDATE`. A test fires 30 concurrent bookings
at one slot and expects exactly one success.

**Voice tools wrap the same service functions as the REST API** — no second code path. Guards in
`agents/tools.py`:
- `VoiceSession.offered_doctors/offered_slots` is a whitelist; the model can only use ids a tool returned.
- `set_caller_details` rejects placeholder names ("Patient") and phone numbers the caller never said
  (checks the transcript via `params.context`, rejects 555-style fakes). The model *did* invent both.
- `cancel_appointment` checks the appointment belongs to the caller.
- DB work runs in `asyncio.to_thread` so the pipeline's event loop never blocks.

**Caller identity.** A real call carries caller ID, so patients are never asked for their number.
`identify_caller(phone)`: known number → greeted by name, identity confirmed; unknown number → asked for
a name only; no number (browser) → synthetic id `web-xxxxxxxx`, behaves as unknown. The voice page passes
the **signed-in patient's phone** as `?phone=` so a voice booking lands on their dashboard record.

**The agent speaks first.** On connect a `TTSSpeakFrame(greeting_for(session))` is queued (no LLM call),
and the same line is inserted into the context as the assistant's first message so the model doesn't
re-introduce itself or re-ask identity.

**Latency work (Sydney DB).** Tools answer the *next* question too (empty day → `next_available`, search
miss → department list) so one tool call per turn instead of eight. Slots prefetched in background after a
search. Lists use `joinedload` (N+1 lazy loads were costing seconds). `reasoning_effort=low` passed via
`Settings.extra`. Smart Turn replaced with a 0.8s speech timeout (it cut users off mid-sentence).
TTS text is chunked at clauses (Kokoro emits nothing until a chunk is fully synthesised). CoreML and int8
Kokoro were measured *slower* than fp32 CPU — don't retry them.

**Browser serializer.** `BrowserFrameSerializer` drops `InterruptionFrame` — pipecat 1.8 sends it, the JS
transport can't parse it ("Unknown frame kind").

**The phone is just another transport.** `build_pipeline(transport, session, vad_analyzer=None)` is
shared; Twilio differs only in the serializer (8 kHz mu-law) and in supplying a VAD analyzer. Three
things telephony forced into the open:
- **A phone sends no speech events.** The pipecat JS client runs VAD in the page; a telephone sends
  only audio. Without a server-side analyzer the agent never decides the caller has finished.
- The browser transport had been passing `vad_enabled` / `vad_analyzer` / `vad_audio_passthrough` to
  `FastAPIWebsocketParams`, which **defines none of them** — pydantic ignored all three silently, so
  that Silero analyzer never ran. Removed; the browser does its own detection.
- **A real call presents E.164** (`+919840012346`); patients are stored as ten digits. `identify_caller`
  falls back to matching the national number, or every phone caller is a stranger.

Who the patient is depends on direction: **inbound they dialled us (`From`), outbound we dialled them
(`To`** — `From` is our own Twilio number and identifies nobody).

**Status never cancels appointments.** Going into surgery blocks *open* slots; already-confirmed
appointments inside the window are returned as `appointments_needing_attention` for a human.

**Live availability without a WebSocket hub.** Browser subscribes straight to Supabase Realtime on
`doctor_status`. RLS is ON for all tables with **only** anon SELECT policies on `doctors` and
`doctor_status`; `users` and `appointments` stay sealed. If you ever add a SQL view, use
`security_invoker = true` or it bypasses RLS and leaks patient data via the publishable key.

**Dashboard sessions are per tab** (`sessionStorage`, not localStorage) so a patient tab and a doctor tab
can be open side by side for the live-status demo.

---

## Gotchas

- **Tests run against the real demo database.** `tests/conftest.py` snapshots max ids before each test
  and deletes everything created after (releasing slots). Don't run pytest while someone is demoing —
  a live booking made mid-run would be deleted too. Before this fixture, tests left 61 cancelled
  appointments and 33 orphaned `booked` slots (cleaned up 13 Sep 2026).
- **Deleting an appointment must also reopen its slot**, otherwise the slot stays `booked` forever and is
  invisible to patients.
- **Twilio trial plays a gate before your TwiML.** On an outbound trial call the recipient hears
  "you have a trial account" and must **press a key**; if they don't, the call ends and your webhook is
  never fetched. Observed: 13s billed, zero requests through ngrok. Upgrading (~$20) removes it.
- **ngrok free URLs change on every restart.** `PUBLIC_HOST` in `.env` *and* the number's voice webhook
  both have to be re-pointed. `tools/fake_twilio_call.py` needs neither — prefer it for testing.
- **Twilio won't sell this account an Indian number** (`AvailablePhoneNumbers/IN/Local` → 404). India
  needs a regulatory bundle. The number we own is **US +1 559 238 8581** ($1.15/mo), so anyone dialling
  it from India pays international rates — which is why the browser demo stays the one for the panel.
- **An account SID + auth token is full API access** — no read-only variant. It can buy numbers and
  place calls. Ask before anything billable.
- **Groq free tier: 8,000 tokens/min per model.** Each LLM call is ~900 tokens (prompt + 7 tool schemas);
  a booking turn uses 2–3 calls. When the agent "goes silent", check for a 429 in the log first.
- **Groq retired the Llama chat models.** Model names are env-driven for this reason.
- **Supabase free project pauses when idle** — symptom: `tenant/user … not found`. User resumes it in the
  dashboard; data survives.
- **Use the pooler (port 6543) for the app, session mode (port 5432, `DIRECT_DATABASE_URL`) for Alembic.**
  The `db.<ref>.supabase.co` host is IPv6-only.
- **RLS policies and the Realtime publication were applied by hand, not via a migration.** A rebuilt DB
  won't have them.
- **Editing backend files under `--reload` while a voice call is open** makes uvicorn wait for the socket
  to close — looks like a hang. End the call or restart.
- **Next.js 16**: read `frontend/node_modules/next/dist/docs/` before using framework APIs (see
  `frontend/AGENTS.md`). React 19.2's `react-hooks/set-state-in-effect` lint rule forbids calling a local
  `useCallback` that sets state directly from an effect — wrap it in an async IIFE; set "live" flags from
  the subscription callback, not the effect body.
- **Time-dependent tests must pin the clock** (`MIDMORNING` in `test_booking.py`); `now + 1h` crosses
  midnight after 23:00.
- Doctors work Mon–Sat 09:00–13:00, and even-indexed seeded doctors also 14:00–17:00. After 17:00 there
  are no slots left today, so a status change visibly blocks nothing.
- 30-thread concurrency test needs a warm pool first, or macOS DNS refuses the burst.

---

## Git / GitHub

- Repo `05sanjaykumar/MediConnect-AI`. PRs #1–#6 merged (config, voice tools, latency+identity,
  phone guard, never-ask-phone, dashboards). `main` is up to date; nothing outstanding.
- The machine has **two GitHub accounts** in `gh`. As of 13 Oct 2026 the active one is
  **`sanjaysk2004`**, which the owner has added as a collaborator — `push: true`, `admin: false`.
  Pushing works directly; no account switch needed. If a push 403s, bypass the keychain credential:
  ```bash
  git -c credential.helper= -c credential.helper='!gh auth git-credential' push -u origin <branch>
  ```
  Because `admin` is false, this account cannot merge its own PRs or change settings — the owner does that.
- Commits are authored as `05sanjaykumar <sanjay@testautomate.ai>` regardless of which `gh` account is
  active; GitHub attributes by email, so commit history is **not** evidence of push access.
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## Status

**Done and verified**
- Auth/roles, doctor search, slot engine, booking/cancel/reschedule, fallback ranking, concurrency guarantee
- Admin API; status changes block/release slots
- Voice agent with tools, caller identity, greeting, phone/name guards, hang-up cleanup of `ACTIVE_CALLS`
- Patient / doctor / admin dashboards, login, live availability via Supabase Realtime
- Admin opens on a **Queue** (upcoming, grouped by day, doctor filter); Doctors tab shows "N waiting";
  History tab has past / cancelled / all
- **Twilio phone layer** — a real call reaches the same agent (see Telephony below)
- 55 backend tests

**Last session ended with** (14 Oct 2026, branch `feat/twilio-phone-layer`, not pushed)
- `routes/telephony.py`: TwiML webhook, media-stream WebSocket, outbound call endpoint, status endpoint
- `tools/fake_twilio_call.py` — fakes a Twilio call locally; verifies the whole phone path without
  dialling anything. Proven working end to end: STT heard "I need to see a cardiologist", the model
  called `search_doctors`, 14.3s of 8 kHz speech came back
- 15 telephony tests; the project report (`report/`, 87pp) was written the session before

**Session before that** (13 Oct 2026, all on `main`, pushed)
- `tests/conftest.py` leave-no-trace fixture — verified: DB counts identical before and after a full
  40-test run
- Admin queue view (`scope` param, `/admin/queue`, joinedload in admin and booking lists)
- PR #6 merged, which brought the dashboards onto `main`
- A full technical rewrite of `README.md` — architecture, data model, the concurrency guarantee, block
  windows, fallback weights, voice guard rails, the 25 endpoints, setup, and measured latency numbers.
  It is the document for the panel; **this file** stays the one for the next AI session.

**Not built — in priority order**
1. **Mid-call interrupt** — the project's novelty claim, and now the *only* headline item left.
   `ACTIVE_CALLS` (in `routes/audio.py`) is populated and cleaned up by both the browser and the phone
   path, but **nothing reads it**. Plan: `services/status.py`, after blocking slots, finds calls
   whose `VoiceSession.offered_slots` intersect the blocked slots, pushes a `TTSSpeakFrame` into that
   `PipelineTask` ("Dr. X just went into surgery…") and adds ranked alternates to the LLM context.
2. **Duplicate greeting in the LLM context.** `get_llm_context` adds the greeting as the assistant's
   first message, and the `TTSSpeakFrame` that speaks it is then recorded *again* by `context.assistant()`.
   Visible in any call log as two identical assistant messages. Pre-existing, affects both transports,
   costs tokens and tells the model it greeted twice. Fix in one place, not two.
3. **Complete / no-show** — statuses exist in `models.py` but nothing ever sets them; past appointments
   stay `confirmed`. Add "Seen"/"No show" on the doctor dashboard. Ties to a cited paper (Kumar et al. 2025).
4. **401 → sign out** in `lib/api.ts` (expired 12h token currently just shows error banners).
5. **Voice session timeout** (Pipecat `session_timeout`) so an abandoned tab doesn't hold a pipeline.
6. Move RLS policies + Realtime publication into an Alembic migration.
