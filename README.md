# MediConnect AI

**A Voice-AI Driven Doctor Appointment and Live Availability Scheduling Platform**

---

A patient books an appointment **by talking** — the agent finds a doctor, reads out real free
times and books one — while doctors change a live status from their own dashboard. Both channels
go through the same REST API and the same booking code, so a doctor going into surgery removes
those times from the voice caller and the web user at the same moment.

The voice layer extends my earlier Pipecat implementation
([Gensail Voice AI](https://github.com/05sanjaykumar/Gensail-Voice-AI)), adding tool calling so the
agent can search and book rather than only converse.

---

## Contents

- [What it does](#what-it-does) · [Architecture](#architecture) · [Stack](#stack)
- [How it works](#how-it-works) — [data model](#data-model), [slot engine](#the-slot-engine),
  [booking](#booking-and-the-concurrency-guarantee), [status](#doctor-status--availability),
  [fallback](#the-fallback-ranking), [voice](#the-voice-agent), [phone](#the-phone-channel), [live updates](#live-availability)
- [Setup](#setup) · [Running](#running) · [One command, with a public link](#one-command-with-a-public-link)
- [Demo accounts](#demo-accounts) · [API](#api)
- [Testing](#testing) · [Engineering notes](#engineering-notes) · [Status](#status)

---

## What it does

| Role | Can |
|---|---|
| **Patient** | Search doctors by department, see who is free *right now*, book / cancel, and book by voice |
| **Doctor** | See today's list, switch live status in one click, see which booked appointments a status change affects |
| **Admin** | Watch the queue (who is waiting for whom), manage doctors and working hours, see every appointment and how it was booked |

**The demo in four steps**

1. Sign in as a patient and open the voice screen. Say *"I need to see a cardiologist on Monday morning."*
2. The agent searches, offers real times, and books one. It appears in **My appointments**, tagged *by voice*.
3. In a second tab, sign in as that doctor and press **In surgery**.
4. The patient's screen repaints on its own — the doctor turns red and their open times disappear.

---

## Architecture

```
          ┌──────────────┐        ┌──────────────┐
 patient  │ Voice screen │        │  Dashboards  │  patient / doctor / admin
          │   (mic)      │        │  (Next.js)   │
          └──────┬───────┘        └───┬──────┬───┘
                 │ WebSocket          │ REST │ Realtime (read-only)
                 │ (Protobuf)         │      │
        ┌────────▼────────────────────▼──┐   │
        │          FastAPI               │   │
        │  ┌──────────────────────────┐  │   │
        │  │ Pipecat pipeline         │  │   │
        │  │ VAD → STT → LLM → TTS    │  │   │
        │  │        │                 │  │   │
        │  │        └─ 7 tools ───┐   │  │   │
        │  └──────────────────────┼───┘  │   │
        │                         ▼      │   │
        │   services/ booking · slots ·  │   │
        │             status · fallback  │   │
        └───────────────┬────────────────┘   │
                        │ SQLAlchemy         │
                 ┌──────▼─────────────────────▼──┐
                 │     Supabase PostgreSQL       │
                 │  (RLS on; Realtime publishes  │
                 │   doctor_status to browsers)  │
                 └───────────────────────────────┘
```

**The voice agent has no privileged path.** Its tools call the same `services/` functions the REST
API calls, so a booking made by voice passes the identical validation, status checks and database
constraints as one made in the browser. There is no second code path to keep in sync.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI, SQLAlchemy 2.0, Alembic | Python is required — Pipecat is Python-only |
| Database | Supabase PostgreSQL (`ap-southeast-2`) | Managed Postgres with Realtime and row-level security |
| Speech-to-text | NVIDIA `nemotron-asr-streaming` | Streams partial transcripts while the caller is still talking |
| Language model | Groq `openai/gpt-oss-120b` | Fast tool calling; ~0.13s to first token |
| Text-to-speech | Kokoro (local ONNX) | Runs on-device, no per-word cost |
| Turn-taking | Silero VAD + 0.8s speech timeout | See [engineering notes](#engineering-notes) |
| Telephony | Twilio Media Streams (8 kHz mu-law) | Same pipeline, different serializer |
| Frontend | Next.js 16, React 19.2, Tailwind 4 | App Router, all dashboards are client components |

---

## How it works

### Data model

Seven tables. The interesting part is that **availability is derived, not stored by hand**.

```
users           id, name, phone (unique), password_hash, role          role ∈ {patient, doctor, admin}
doctors         id, user_id→users, specialization, qualification,
                consultation_fee, slot_minutes, room, is_active
                a doctor's name lives on their user row, not here
working_hours   id, doctor_id→doctors, weekday 0-6, start_time, end_time    ← the rule
doctor_status   doctor_id→doctors (PK), status, note, updated_at            ← 5 live states
slots           id, doctor_id→doctors, start_at, end_at, state              ← generated from the rule
                UNIQUE (doctor_id, start_at)                     state ∈ {open, booked, blocked}
appointments    id, slot_id→slots, patient_id→users, doctor_id→doctors,
                status, created_via, reason, created_at, cancelled_at
                UNIQUE (slot_id) WHERE status = 'confirmed'       ← the double-booking guard
status_events   id, doctor_id, from_status, to_status, blocked_slots, at    ← append-only history
```

`created_via ∈ {web, voice}` is what lets the admin dashboard show how many appointments the voice
agent has booked. `status_events` is an audit trail of every status change and how many slots it took
off the market.

### The slot engine

Doctors don't own a list of appointment times. They own **working hours** — a weekly rule — and slots
are generated from it on demand:

```
get_slots(doctor, day):
   1. read the slots that already exist for that day       (1 query)
   2. if none exist, expand working_hours into [start, start + slot_minutes) intervals
      and insert them  — idempotent, protected by UNIQUE(doctor_id, start_at)
   3. drop slots that are not 'open', in the past, or inside the doctor's blocked window
```

Pre-generating a year for 50 doctors would be ~580,000 rows that mostly never get used. Generating
lazily keeps the table proportional to what people actually look at, and the unique constraint means
two simultaneous requests for the same day can't produce duplicates.

### Booking and the concurrency guarantee

Two independent guards, and the distinction between them matters:

```python
slot = _lock_slot(db, slot_id)        # SELECT … FOR UPDATE
...checks: slot open? in the future? doctor accepting?...
db.add(appointment); slot.state = "booked"
db.commit()                           # ← UNIQUE(slot_id) WHERE confirmed fires here
```

1. **The application check** produces the friendly message a patient or the voice agent hears
   (*"That slot has just been taken."*). It can lose a race.
2. **The partial unique index** cannot. Two requests that both pass check 1 at the same instant still
   produce exactly one appointment; the loser gets an `IntegrityError`, which is translated into the
   same friendly error.

A slot may accumulate many *cancelled* appointments over its life but only ever one *confirmed* — which
is exactly what a partial index expresses and a plain unique constraint cannot.

This is verified, not asserted. `test_concurrent_bookings_produce_exactly_one_appointment` fires **30
concurrent bookings at one slot** and requires exactly one success, 29 clean refusals, and exactly one
confirmed row in the database afterwards.

### Doctor status → availability

Changing status is not just a flag. It reconciles the schedule:

| Status | Blocks |
|---|---|
| `available` | nothing — releases previously blocked future slots |
| `busy` | the next 15 minutes (the consultation in progress) |
| `on_break` | the next 30 minutes |
| `in_surgery` | the rest of the working day |
| `off_duty` | the rest of the working day |

Open slots inside the window become `blocked`; coming back to `available` releases them again.

**Confirmed appointments are never cancelled automatically.** Appointments that fall inside a newly
blocked window are returned as `appointments_needing_attention` and flagged on the doctor's dashboard
for a human to deal with. Silently cancelling someone's appointment because a status changed would be
the wrong behaviour in a hospital system.

### The fallback ranking

When a booking fails, the API returns ranked alternatives in the same 409 response. It is a **plain
scoring function, deliberately not an LLM call** — the voice agent reads the suggestions aloud but
never chooses them, so the choice stays deterministic, unit-testable and explainable:

```
score = 3.0 · same specialization
      + 2.0 · same doctor, later time
      + 1.5 · 1/(1 + hours away)          ← sooner is better
      + 1.0 · patient has seen them before
      + 0.5 · (1 − doctor's load that day) ← spread the work
      − 2.0 · requires a different day
```

Every weight lives in one dictionary in `services/fallback.py` and each alternative carries a `why`
string ("same doctor, later time", "also Cardiology, different day") that the UI and the agent both use.

### The voice agent

```
browser mic ──WS── VAD(Silero) ── STT(NVIDIA) ── LLM(Groq) ── TTS(Kokoro) ──► caller
                                                    │
                                                    └── 7 tools ──► services/ ──► Postgres
```

**Tools:** `search_doctors` · `get_available_slots` · `book_appointment` · `cancel_appointment` ·
`list_my_appointments` · `set_caller_details` · `get_hospital_info`

A model that can write to a database needs guard rails, and these were all added in response to
behaviour actually observed in testing:

- **Id whitelist.** `VoiceSession` records every doctor and slot id a tool has returned. An id the
  model produces that isn't on that list was invented, and the tool refuses before touching the database.
- **Server-side revalidation.** Every write goes through `services/booking.py` regardless, so a
  hallucinated id fails loudly instead of creating a wrong appointment.
- **Real names only.** `set_caller_details` rejects placeholders — the model tried `{"name": "Patient"}`
  to satisfy the "ask for a name" rule.
- **Phone numbers must have been spoken.** The tool reads the conversation transcript and refuses any
  number that doesn't appear in what the caller actually said. The model invented `5551234567` for a
  caller who had only given a name — and wrote the caller's confirmation of it as well.
- **Ownership.** `cancel_appointment` checks the appointment belongs to this caller.

**Caller identity.** A real phone call arrives with caller ID, so patients are never asked for their
number. A known number is greeted by name (*"Hello Sanjay, this is MediConnect"*); an unknown number is
asked for a name only; the browser, which has no caller ID, gets a synthetic id and behaves like an
unknown caller. When a patient is signed in to the dashboard, the voice screen passes their number, so a
voice booking lands on their record.

**The agent speaks first.** On connect the greeting is sent straight to text-to-speech — no model call,
so the caller hears a voice within a second — and the same line is written into the conversation as the
assistant's opening so the model doesn't introduce itself twice.

### The phone channel

A real call reaches the same agent. Twilio answers, asks `/api/twilio/voice` what to do, and gets back
TwiML that opens a bidirectional media stream to `/api/twilio/stream`. From `build_pipeline` onwards
nothing differs from a browser call — only the transport and the serializer change.

```
caller ──PSTN── Twilio ──WS(8kHz mu-law)── /api/twilio/stream ── the same pipeline
```

Three things telephony forces that the browser hides:

- **A phone sends no speech events.** The browser client runs voice activity detection in the page and
  tells us when the caller starts and stops; a telephone sends only audio. The phone path therefore
  builds its own analyzer — without one the agent never decides the caller has finished.
- **A real call presents E.164** (`+919840012346`) while patients are stored as the ten digits they'd
  actually give you. `identify_caller` falls back to matching the national number, or every phone
  caller is a stranger.
- **Who the patient is depends on direction.** Inbound, they dialled us, so they are `From`. Outbound,
  we dialled them, so they are `To` — `From` is the Twilio number and identifies nobody.

Testing it needn't cost anything. `tools/fake_twilio_call.py` speaks Twilio's media-stream protocol
directly, so the whole inbound path can be exercised locally — no number, no tunnel, no call charges:

```bash
python tools/fake_twilio_call.py --caller +919840012346 --speech /tmp/question.wav
```

That run is how the phone path was verified: the agent recognised the caller by name, the transcript
came back as *"I need to see a cardiologist / what times are available tomorrow morning"*, the model
called `search_doctors`, and 14.3 seconds of 8 kHz speech came back down the wire.

**Setting it up for real** needs a Twilio number whose voice webhook points at this server, and a
public URL — `ngrok http 8000` in development, with the host (no scheme) in `PUBLIC_HOST`. A free
ngrok URL changes on every restart, so both that variable and the number's webhook have to be
re-pointed each time. On a Twilio trial account the caller first hears a "you have a trial account"
notice and must press a key before your TwiML runs at all.

### Live availability

The browser subscribes **directly to Supabase Realtime** on `doctor_status`. There is no WebSocket hub
in the backend for this; a doctor's status change goes database → Supabase → every open dashboard.

Row-level security is enabled on all seven tables, with anonymous `SELECT` policies on **only**
`doctors` and `doctor_status` — public information. `users` and `appointments` are sealed, so the
publishable key in the browser cannot read patient records.

> If you ever add a SQL view over these tables, create it with `security_invoker = true`. A normal
> Postgres view runs with its owner's rights and would bypass RLS entirely.

---

## Setup

**Requirements:** Python 3.11 · Node.js 20+ · `espeak-ng` (`brew install espeak-ng`, needed by Kokoro)

> Python 3.12+ will not work for the voice stack — the ONNX/audio wheels aren't available.

**1. Backend**

```bash
cd backend && python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Working on booking or the dashboards only? This is enough, and the app starts without the voice
endpoints (`voice: disabled (pipecat not installed)`):

```bash
cd backend && python3.11 -m venv .venv && .venv/bin/pip install fastapi uvicorn "sqlalchemy>=2.0" "psycopg[binary]" python-dotenv alembic pytest
```

**2. Configure** — `cp backend/.example.env backend/.env`, then fill in:

```env
DATABASE_URL="postgresql+psycopg://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres"
DIRECT_DATABASE_URL="postgresql+psycopg://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres"
GROQ_API_KEY=
NVIDIA_API_KEY=
KOKORO_VOICE=af_heart
SECRET_KEY=any-random-string
FRONTEND_URL=http://localhost:3000
```

Two connection strings on purpose: the app uses the **transaction pooler (6543)**; Alembic uses
**session mode (5432)**, because schema changes can't run through pgbouncer. Don't use the
`db.<ref>.supabase.co` host — it is IPv6-only and fails on most networks.

**3. Schema and data**

```bash
cd backend && .venv/bin/alembic upgrade head && .venv/bin/python seed.py
```

12 doctors across 6 specializations with weekly schedules, 5 patients and an admin. `seed.py --reset`
wipes and rebuilds.

**4. Frontend**

```bash
cd frontend && npm install && cp .example.env .env.local
```

Fill in the Supabase URL and publishable key. Without them the dashboards still work — they just
won't update on their own.

---

## Running

```bash
cd backend && .venv/bin/python -m uvicorn main:app --reload --port 8000
```

```bash
cd frontend && npm run dev
```

| | |
|---|---|
| Voice screen | http://localhost:3000 |
| Dashboards | http://localhost:3000/login |
| **Interactive API docs** | **http://localhost:8000/docs** |

Startup prints which database it reached, whether Pipecat loaded and that the connection pool is warm.
Stop both with `pkill -f "uvicorn main:app"; pkill -f "next dev"`.

If port 3000 is already taken, run the frontend elsewhere — `npm run dev -- -p 3001` — and read the
URLs above as `:3001`. `demo.sh` below uses 3001 for exactly this reason.

---

## One command, with a public link

Four processes, three config files and a Twilio setting have to agree before a voice call works from
outside the machine. `demo.sh` does all of it and prints a link anyone can open:

```bash
./demo.sh up
```

```
→ ngrok
   api=https://ab12cd34.ngrok-free.app
   web=https://ef56gh78.ngrok-free.app
→ pointing the config at today's URLs
→ backend
→ frontend
→ re-pointing the Twilio SIP domain

checks:
   backend  :8000      200
   frontend :3001      200
   public page         200
   api via proxy       200
   active calls        0

SHARE THIS:  https://ef56gh78.ngrok-free.app
local:       http://localhost:3001   (voice)   /login  (dashboards)
phone:       +1 559 238 8581    SIP: mediconnect-097e72.sip.twilio.com
```

Open the `SHARE THIS` URL on a phone, hand it to someone across the country, or use it yourself —
it serves the voice screen and proxies the API through the same origin, so the microphone works
without a second tunnel or a CORS exception.

| | |
|---|---|
| `./demo.sh up` | starts everything and prints the link |
| `./demo.sh link` | prints the current link again |
| `./demo.sh down` | stops backend, frontend, ngrok and the keep-awake |

**What `up` actually does**, because skipping any of it is what breaks a live demo:

1. Kills anything already running on those ports
2. Holds the Mac awake with `caffeinate` (the lid still has to stay open)
3. Starts both ngrok tunnels and waits for them to report their URLs
4. Rewrites `PUBLIC_HOST` and `FRONTEND_URL` in `backend/.env`, and
   `NEXT_PUBLIC_BACKEND_WS_URL` in `frontend/.env.local` — **a free ngrok URL changes on every
   restart**, so these are rewritten every time
5. Starts uvicorn (log: `/tmp/mc.log`) and Next.js on 3001 (log: `/tmp/mc-front.log`)
6. Re-points the Twilio SIP domain at today's host via `backend/tools/setup_sip.py`
7. Checks all four endpoints and prints the result

**Prerequisites** — [ngrok](https://ngrok.com/download) installed and authenticated
(`ngrok config add-authtoken <token>`), with both tunnels named in `~/Library/Application
Support/ngrok/ngrok.yml`:

```yaml
version: "3"
tunnels:
  api:
    addr: 8000
    proto: http
  web:
    addr: 3001
    proto: http
```

The tunnel **names** matter: the script looks up `api` and `web` by name through ngrok's local API on
port 4040. The phone and SIP lines only apply if Twilio credentials are in `backend/.env`; without
them everything else still comes up and the link still works.

The link lives only as long as the terminal does. Close the laptop or stop the script and it dies —
run `./demo.sh up` again and share the new URL.

---

## Demo accounts

Password for every account: **`demo1234`** — the login page has one-click buttons.

| Role | Phone | Who |
|---|---|---|
| Admin | `9840000000` | Hospital Admin |
| Patient | `9840012346` | Sanjay Kumar |
| Patient | `9840012347` | Riya Senju |
| Doctor | `9840020000` | Dr. Anitha Sharma (Cardiology) |
| Doctor | `9840020005` | Dr. Vikram Choudhary (Orthopedics) |

Doctors work Mon–Sat, 09:00–13:00, some also 14:00–17:00. After 17:00 there are no slots left today,
so pick a later day when testing.

---

## API

```
POST   /api/auth/login                           phone + password → JWT (12h)
POST   /api/auth/register                        new patient
GET    /api/auth/me                              current user (+ doctor_id, phone)

GET    /api/doctors?specialization=&name=        search, with live status
GET    /api/doctors/specializations              distinct departments
GET    /api/doctors/{id}                         profile
GET    /api/doctors/{id}/slots?on=YYYY-MM-DD     bookable slots for a day
GET    /api/doctors/{id}/next-available          soonest bookable slot
PATCH  /api/doctors/{id}/status                  doctor (own) or admin

POST   /api/appointments                         book  → 409 carries ranked alternates
GET    /api/appointments/mine                    mine (patient or doctor)
GET    /api/appointments/{id}
PATCH  /api/appointments/{id}                    reschedule
DELETE /api/appointments/{id}                    cancel, releases the slot

GET    /api/admin/overview                       dashboard tiles
GET    /api/admin/queue                          upcoming count + next time per doctor
GET    /api/admin/appointments?scope=&status=&doctor_id=     scope ∈ {upcoming, past, all}
GET    /api/admin/doctors                        all, including deactivated
POST   /api/admin/doctors                        create with schedule and status
PATCH  /api/admin/doctors/{id}                   edit
DELETE /api/admin/doctors/{id}                   deactivate (never deletes)
GET    /api/admin/doctors/{id}/working-hours
PUT    /api/admin/doctors/{id}/working-hours     replace the week

GET    /api/health                               includes active voice calls
WS     /api/ws/voice?phone=<caller id>           voice pipeline (browser)

POST   /api/twilio/voice                         TwiML — Twilio asks what to do with a call
WS     /api/twilio/stream                        the call's audio, both ways
POST   /api/twilio/call                          ring a patient and connect them   (admin)
GET    /api/twilio/status                        what telephony has, and what it's missing
```

Roles are enforced server-side: a patient token on an admin route gets `403`, no token `401`.

**A failed booking** returns the recovery payload the voice agent speaks aloud:

```json
{ "code": "SLOT_TAKEN",
  "message": "That slot has just been taken.",
  "alternates": [
    { "doctor_name": "Dr. Anitha Sharma", "start_at": "2026-09-07T09:20:00",
      "score": 5.515, "why": "same doctor, later time" }
  ] }
```

---

## Testing

```bash
cd backend && .venv/bin/python -m pytest tests -q        # 67 tests, 9–10 min
```

Takes minutes because every query is a real round trip to a hosted database.

| File | Covers |
|---|---|
| `tests/test_booking.py` (9) | Slot generation is idempotent, past slots are never offered, status blocking windows, book/cancel, **the 30-way concurrency test** |
| `tests/test_agent_tools.py` (31) | Every voice tool, the id whitelist, invented names and phone numbers, caller identity, cancelling someone else's appointment, TTS chunking |
| `tests/test_telephony.py` (27) | TwiML for inbound and outbound, the caller the number resolves to, E.164 → patient matching, Twilio's connected/start handshake, configuration reporting |

The suite runs against the **real** database, so `tests/conftest.py` snapshots the highest ids and the
booked slots before each test and removes anything created afterwards. Row counts are identical before
and after a full run. Don't run the tests during a demo.

---

## Engineering notes

Things that were measured rather than guessed — useful context for why the code looks as it does.

**The database is in Sydney; every round trip costs ~0.46s.** That single fact shaped more of this
codebase than any other. A voice turn that made eight tool calls took 27 seconds of silence. Fixes:
tools answer the *next* question too (an empty day returns `next_available`; a failed search returns
the real department list), slot generation is batched across doctors, lists use eager loading, and
slots are prefetched in the background after a search. The same turn now takes **2.7 seconds**.

**Lazy loading was the hidden cost.** Serializing an appointment reads the patient, the doctor, the
doctor's user row and the slot — three or four extra round trips *per row*. Eager loading took
`/admin/doctors` from **~11s to ~1.5s**.

**Text-to-speech latency is about sentence length, not total length.** Kokoro emits nothing until a
whole chunk is synthesised, so one long sentence is one long silence. Chunking at clause boundaries
took time-to-first-audio from **1.67s to 0.46s**. CoreML (2.3s) and an int8 model (2.7s) were both
measured *slower* than fp32 on CPU and rejected.

**Smart turn detection cut users off.** The default model ended the turn on *"I want to see"* before
*"a neurologist"* arrived, sending half a sentence to the model. A plain 0.8s speech timeout is duller
and more predictable, which is what a demo needs.

**Model availability is not stable.** Groq retired the Llama chat line mid-project, so the configured
model 404'd. Model names now come from the environment; swapping one is a one-line change.

---

## Status

**Working**

Auth and roles · doctor search with live status · slot generation · book / cancel / reschedule ·
ranked alternates · the concurrency guarantee · admin management · status changes blocking and
releasing slots · voice agent booking by browser microphone with caller identity and guard rails ·
patient, doctor and admin dashboards · live availability via Supabase Realtime ·
**booking by telephone through Twilio** · 67 tests

**Not built yet**

1. **Mid-call interrupt** — a doctor's status change reaching a call *in progress*, so the agent
   apologises and offers an alternative mid-sentence. The call registry exists; nothing reads it yet.
   This is the project's main contribution and the next piece of work.
2. Marking appointments *completed* / *no-show* (the statuses exist but nothing sets them)
3. Signing out automatically when the token expires
4. An idle timeout for abandoned voice calls

---

> Working on this with an AI assistant? Start with [`CLAUDE.md`](CLAUDE.md) — design decisions,
> gotchas and the current state. [`PLAN.md`](PLAN.md) is the original design document; several
> decisions have changed since.
