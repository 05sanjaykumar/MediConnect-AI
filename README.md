# MediConnect AI

**A Voice-AI Driven Doctor Appointment and Live Availability Scheduling Platform**

BCSE497J Project I · Fall 2026–2027 · School of Computer Science and Engineering, VIT Chennai

Ananya Kumari (23BCE1474) · Sanjay Kumar (23BCE1248) · Riya Senju (23BCE1290)

---

A patient books an appointment by talking — over a phone call or in the browser — while
doctors change their live status from a dashboard. Both channels talk to the same REST API, so
when a doctor goes into surgery, the slots disappear for the voice caller and the web user at
the same moment.

The voice layer extends our earlier Pipecat implementation
([Gensail Voice AI](https://github.com/05sanjaykumar/Gensail-Voice-AI)), adding tool calling so
the agent can search and book rather than only converse.

---

## Status

| Working | Not built yet |
|---|---|
| Auth with patient / doctor / admin roles | Voice agent tool calling |
| Doctor search with live status | Twilio phone layer |
| Slot generation from working hours | Status change → automatic slot blocking |
| Book · cancel · reschedule | Live dashboard updates (Supabase Realtime) |
| Ranked alternates when booking fails | Patient / doctor / admin dashboards |
| Concurrency-safe booking (proven by test) | |
| Admin: manage doctors and schedules | |

> `PLAN.md` is the original design document. Several decisions have changed since it was
> written — Redis and the custom WebSocket hub are out, Alembic and Supabase Realtime are in.

---

## Requirements

- **Python 3.11+**
- **Node.js 20+** (frontend)
- **espeak-ng** — required by Kokoro TTS, voice only

```bash
brew install espeak-ng
```

---

## Setup

### 1. Backend environment

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Installing the full `requirements.txt` pulls in Pipecat, Kokoro and the audio stack, which takes
a few minutes. **If you're only working on booking or the dashboards**, this is enough:

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn "sqlalchemy>=2.0" "psycopg[binary]" python-dotenv alembic pytest
```

The app detects that Pipecat is missing and starts anyway, without the voice endpoints. You'll
see `voice: disabled (pipecat not installed)` on startup.

### 2. Configure

```bash
cp backend/.example.env backend/.env
```

Fill in `backend/.env`:

```env
DATABASE_URL="postgresql+psycopg://postgres.<project>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"
DIRECT_DATABASE_URL="postgresql+psycopg://postgres.<project>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"

GROQ_API_KEY=
NVIDIA_API_KEY=
KOKORO_VOICE=af_heart

SECRET_KEY=any-random-string
FRONTEND_URL=http://localhost:3000
```

Two connection strings on purpose: the app uses the **transaction pooler (6543)**, and Alembic
uses **session mode (5432)** because schema changes can't run through pgbouncer. Don't use the
`db.<project>.supabase.co` direct host — it's IPv6-only and fails on most networks.

### 3. Create the schema

```bash
cd backend && .venv/bin/alembic upgrade head
```

### 4. Seed demo data

```bash
cd backend && .venv/bin/python seed.py
```

12 doctors across 6 specializations, their weekly schedules, 5 patients and an admin. Add
`--reset` to wipe and rebuild.

### 5. Frontend (optional for now)

```bash
cd frontend && npm install
```

```bash
echo "NEXT_PUBLIC_BACKEND_WS_URL=ws://localhost:8000/api/ws/voice" > frontend/.env.local
```

---

## Running

```bash
cd backend && .venv/bin/python -m uvicorn main:app --reload --port 8000
```

`--reload` restarts on every save. **Ctrl+C** stops it. If it was started in the background:

```bash
pkill -f "uvicorn main:app"
```

Frontend:

```bash
cd frontend && npm run dev
```

| | |
|---|---|
| API | http://localhost:8000 |
| **Interactive docs** | **http://localhost:8000/docs** |
| Frontend | http://localhost:3000 |

`/docs` is the fastest way to explore — click **Authorize**, paste a token, and every endpoint
has a *Try it out* button.

---

## Demo accounts

Password for every account: **`demo1234`**

| Role | Phone | Who |
|---|---|---|
| Admin | `9840000000` | Hospital Admin |
| Patient | `9840012346` | Sanjay Kumar |
| Patient | `9840012347` | Riya Senju |
| Doctor | `9840020000` | Dr. Anitha Sharma (Cardiology) |
| Doctor | `9840020005` | Dr. Vikram Choudhary (Orthopedics) |

---

## API

```
POST   /api/auth/login                          phone + password → JWT
POST   /api/auth/register                       new patient
GET    /api/auth/me                             current user

GET    /api/doctors?specialization=&name=       search, with live status
GET    /api/doctors/specializations             distinct list
GET    /api/doctors/{id}                        profile
GET    /api/doctors/{id}/slots?on=YYYY-MM-DD    bookable slots
GET    /api/doctors/{id}/next-available         soonest slot
PATCH  /api/doctors/{id}/status                 doctor or admin only

POST   /api/appointments                        book
GET    /api/appointments/mine                   my upcoming
PATCH  /api/appointments/{id}                   reschedule
DELETE /api/appointments/{id}                   cancel

GET    /api/admin/overview                      dashboard numbers
GET    /api/admin/doctors                       all, including deactivated
POST   /api/admin/doctors                       create with schedule
PATCH  /api/admin/doctors/{id}                  edit
DELETE /api/admin/doctors/{id}                  deactivate
GET    /api/admin/doctors/{id}/working-hours
PUT    /api/admin/doctors/{id}/working-hours    replace the week
GET    /api/admin/appointments                  filterable

WS     /api/ws/voice                            browser voice (needs Pipecat)
```

### Walkthrough

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login -H "Content-Type: application/json" -d '{"phone":"9840012346","password":"demo1234"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

```bash
curl -s "http://localhost:8000/api/doctors?specialization=cardio" | python3 -m json.tool
```

```bash
curl -s "http://localhost:8000/api/doctors/1/slots" | python3 -m json.tool
```

```bash
curl -s -X POST http://localhost:8000/api/appointments -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"slot_id":1,"reason":"chest pain"}' | python3 -m json.tool
```

Booking a taken slot returns `409` with ranked alternates — the same payload the voice agent
uses to recover a conversation:

```json
{ "code": "SLOT_TAKEN",
  "message": "That slot has just been taken.",
  "alternates": [
    { "doctor_name": "Dr. Anitha Sharma", "start_at": "2026-08-25T09:20:00",
      "score": 5.515, "why": "same doctor, later time" } ] }
```

### The status demo

```bash
DTOK=$(curl -s -X POST http://localhost:8000/api/auth/login -H "Content-Type: application/json" -d '{"phone":"9840020000","password":"demo1234"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

```bash
curl -s -X PATCH http://localhost:8000/api/doctors/1/status -H "Authorization: Bearer $DTOK" -H "Content-Type: application/json" -d '{"status":"in_surgery","note":"emergency bypass"}' | python3 -m json.tool
```

Dr. Sharma's 21 open slots drop to 0 and `is_bookable` becomes `false`. Send
`{"status":"available"}` to restore them.

---

## Tests

```bash
cd backend && .venv/bin/python -m pytest tests -v
```

Nine tests. The important one is
`test_concurrent_bookings_produce_exactly_one_appointment`: thirty threads race for the same
slot, exactly one appointment exists afterwards, the other twenty-nine get a clean `SLOT_TAKEN`.

Tests run against the real Supabase database and clean up after themselves.

---

## Migrations

The app never alters the schema on startup — Alembic owns it.

```bash
cd backend && .venv/bin/alembic revision --autogenerate -m "describe your change"
```

```bash
cd backend && .venv/bin/alembic upgrade head
```

Edit a model, autogenerate, **read the generated file**, then upgrade. Commit the migration so
the others get it. Check where you are with `alembic current`.

---

## Structure

```
backend/
├── main.py              FastAPI app, router mounts
├── config.py            voice-service settings
├── database.py          Supabase connection, session factory
├── models.py            7 tables
├── schemas.py           request/response models
├── security.py          password hashing + JWT (standard library only)
├── deps.py              current user, role gates
├── seed.py              demo data
├── routes/
│   ├── auth.py          login, register, me
│   ├── doctors.py       search, slots, status
│   ├── appointments.py  book, reschedule, cancel
│   ├── admin.py         manage doctors and schedules
│   └── audio.py         Pipecat WebSocket pipeline
├── services/
│   ├── availability.py  what each status blocks, and for how long
│   ├── slot_engine.py   working hours → slots
│   ├── booking.py       book / cancel / reschedule  ← the core
│   ├── fallback.py      ranking function for alternates
│   └── STT.py · LLM.py · TTS.py   voice services
├── tests/
│   └── test_booking.py
└── alembic/             migrations

frontend/                Next.js 16, Pipecat JS client
└── app/page.tsx         voice UI — mic button, transcripts
```

### The two files worth reading

**`services/booking.py`** — two guards on every booking. An application check that produces the
message a patient hears, and a partial unique index
(`UNIQUE (slot_id) WHERE status = 'confirmed'`) that makes double-booking impossible when two
requests pass that check at the same instant. The first can lose a race; the second cannot.

**`services/fallback.py`** — a plain scoring function, deliberately not an LLM call. The voice
agent reads the suggestions aloud but never chooses them, so the choice stays deterministic and
testable. Weights live in one dictionary at the top.

---

## Troubleshooting

**`address already in use`** — an old server still holds the port.

```bash
lsof -i :8000
```

**`prepared statement "..." already exists`** — something is bypassing the pgbouncer setting in
`database.py`. Check `DATABASE_URL` uses port **6543** and `connect_args={"prepare_threshold": None}` is intact.

**Connection hangs or times out** — you're probably on the `db.<project>.supabase.co` host,
which is IPv6-only. Use the `pooler.supabase.com` string instead.

**`voice: disabled (pipecat not installed)`** — expected on the light install. Run the full
`pip install -r requirements.txt` to enable voice.

**Kokoro TTS errors** — `espeak-ng` is missing: `brew install espeak-ng`.

**Alembic can't connect** — migrations need `DIRECT_DATABASE_URL` on port **5432**. DDL cannot
run over the transaction pooler.

---

## Security notes

Row Level Security is enabled on all seven tables with **no policies**, so Supabase's
auto-generated REST API returns nothing to anyone holding the publishable key. The backend
connects as `postgres` over the pooler and is unaffected.

When the dashboards start using Supabase Realtime, they'll need a `SELECT` policy on `doctors`
and `doctor_status` only. `users` and `appointments` stay sealed.

`backend/.env` is gitignored. Rotate the database password and any keys before making this
repository public.
