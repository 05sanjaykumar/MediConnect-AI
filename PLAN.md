# MediConnect AI — Implementation Plan

**BCSE497J Project I · Fall 2026-2027 · SCOPE, VIT Chennai**
Ananya Kumari (23BCE1474) · Sanjay Kumar (23BCE1248) · Riya Senju (23BCE1290)

---

## 1. What we are actually building

A doctor appointment platform with two front doors — a **phone call** and a **web app** — both
talking to **one REST API**. A doctor flips their live status from a dashboard; that change
invalidates affected slots and propagates to a web user and a mid-call voice user within a second.

The contribution we defend at the panel is not "we used voice AI." It is:

> **Booking safely against availability that changes underneath you.**
> A slot can be invalidated *while a voice caller is in the middle of confirming it.* The system
> detects the invalidation, releases the hold, ranks alternates, and the agent recovers the
> conversation without dropping the call or double-booking.

Everything below exists to make that sentence demonstrable and measurable.

---

## 2. Decisions that shape everything

| # | Decision | Why |
|---|----------|-----|
| D1 | **One realtime path: our own WebSocket hub on FastAPI.** Drop Supabase Realtime. | The deck lists Redis + WebSocket + Supabase Realtime for the same job. Three push mechanisms is three failure modes and nothing to show for it. Redis stays — but for slot holds, pub/sub fan-out across workers, and voice session state. |
| D2 | **The voice agent is an API client, not a DB client.** | The LLM gets tools (`search_doctors`, `get_slots`, `hold_slot`, `confirm_booking`) that are thin wrappers over the exact HTTP endpoints the web app calls. This makes "off the same API" literal and testable, not a claim on a slide. |
| D3 | **Slots are generated lazily and persisted on first request**, not pre-materialized forever. | A doctor with 8h/day at 15-min granularity = ~32 rows/day/doctor. Pre-generating a year for 50 doctors is 580k dead rows. Generate on demand for a date window, persist idempotently, cache the availability view in Redis. |
| D4 | **Two-layer booking safety: Redis hold (advisory) + DB unique constraint (authoritative).** | The Redis hold gives us a UX-level reservation with a TTL so a voice caller can deliberate. The DB constraint is what actually makes double-booking impossible. Never trust the first without the second. |
| D5 | **Every provider (STT / TTS / LLM / telephony) sits behind an interface with a mock implementation.** | We have no API keys yet. This is not a workaround — it is what lets us run the full evaluation suite deterministically in CI, without spending money or needing a phone line. |
| D6 | **Groq — Llama 3.3 70B.** Drop Mixtral. | Mixtral is deprecated on Groq and dated. Fix this on the slide too. |

---

## 3. System design — the parts the deck left vague

### 3.1 Data model

Keeps the slide-8 entities, adds what booking safety needs.

```
users            (id, name, phone UNIQUE, email, password_hash, role, created_at)
                 role ∈ {patient, doctor, admin}

doctors          (id, user_id FK→users 1:1, specialization, qualification,
                  consultation_fee, slot_duration_min DEFAULT 15, room, is_active)

working_hours    (id, doctor_id FK, weekday 0-6, start_time, end_time)
                 -- the RULE. slots are derived from this, never hand-entered.

doctor_status    (doctor_id PK FK, status, note, updated_at, updated_by)
                 status ∈ {available, busy, in_surgery, on_break, off_duty}

slots            (id, doctor_id FK, start_at, end_at, state, generated_at)
                 state ∈ {open, held, booked, blocked}
                 UNIQUE (doctor_id, start_at)          ← D4 authoritative guard

appointments     (id, slot_id FK UNIQUE, patient_id FK, doctor_id FK, status,
                  created_via, reason, created_at, cancelled_at)
                 status ∈ {confirmed, completed, cancelled, no_show}
                 created_via ∈ {web, voice}
                 UNIQUE (slot_id) WHERE status = 'confirmed'   ← partial index

status_events    (id, doctor_id, from_status, to_status, at, affected_slot_ids)
                 -- append-only. this IS our evaluation data for propagation latency.

audit_log        (id, actor_id, action, entity, entity_id, at, payload_json)
```

`UNIQUE (slot_id) WHERE status='confirmed'` is the single line that makes double-booking
structurally impossible. Everything else is optimization.

### 3.2 Slot generation

```
get_slots(doctor_id, date_range):
  1. Redis lookup  availability:{doctor_id}:{date}   → hit? return
  2. Expand working_hours for each weekday in range into [start, start+slot_duration) intervals
  3. INSERT ... ON CONFLICT (doctor_id, start_at) DO NOTHING     -- idempotent, safe concurrent
  4. Subtract: state != 'open', past slots, and status-blocked windows (§3.4)
  5. Cache in Redis, TTL 60s, key tagged for invalidation on status change
```

### 3.3 The booking protocol — the core of the project

```
  CALLER (voice or web)                    SERVER
  ─────────────────────                    ──────
  POST /slots/{id}/hold      ──────────►   SET hold:slot:{id} = {session_id}
                                           NX EX 90        (voice: 90s, web: 120s)
                             ◄──────────   201 {hold_token, expires_at}
                                           ✗ 409 if already held/booked

  ... caller deliberates, agent speaks, patient says "yes" ...

  POST /appointments         ──────────►   BEGIN
    {hold_token, patient_id}                 verify hold still ours (Redis GET)
                                             SELECT slot FOR UPDATE
                                             assert slot.state = 'held'
                                             assert doctor_status allows booking
                                             INSERT appointment  ← unique index fires here
                                             UPDATE slot.state = 'booked'
                                           COMMIT
                                           DEL hold, publish booking.created
                             ◄──────────   201 {appointment}
                                           ✗ 409 HOLD_EXPIRED
                                           ✗ 409 SLOT_INVALIDATED   ← the interesting one
```

**`SLOT_INVALIDATED` is the case the whole project is about.** If the doctor went `in_surgery`
between hold and confirm, the response carries `{reason, alternates: [...]}` from §3.5, and the
voice agent is instructed to apologize, explain, and offer the top alternate — in one turn,
without ending the call.

### 3.4 Doctor status → propagation

```
PATCH /doctors/{id}/status  {status: "in_surgery"}
  ├─ write doctor_status + append status_events (t0 recorded here)
  ├─ compute affected window:
  │     in_surgery / off_duty  → block all open slots to end of working day
  │     on_break               → block next 30 min
  │     busy                   → block current slot only
  │     available              → unblock future slots not otherwise taken
  ├─ UPDATE slots SET state='blocked' WHERE ... AND state='open'
  ├─ for each active hold on a now-blocked slot:
  │     DEL hold  +  publish slot.invalidated {slot_id, session_id, alternates}
  ├─ invalidate Redis availability cache for that doctor
  └─ redis.publish("doctor:{id}") → WS hub → every subscribed browser AND
                                    the live voice session (t1 recorded on delivery)

     propagation latency = t1 − t0    ← our headline metric
```

Confirmed appointments are **never** silently cancelled by a status change. They surface on the
doctor dashboard as "needs attention" for a human decision. Automating that would be wrong in a
healthcare system, and saying so out loud is worth a point at the panel.

### 3.5 Smart fallback — the ranking function

When the requested doctor is unavailable, or a slot is invalidated, we score candidates
deterministically (no LLM in this decision — it must be testable):

```
score(candidate) =  3.0 · same_specialization
                 +  2.0 · same_doctor_later_slot
                 +  1.5 · (1 / (1 + hours_until_slot))      -- sooner is better
                 +  1.0 · patient_has_seen_before
                 +  0.5 · (1 − doctor_load_today)           -- spread the load
                 −  2.0 · requires_different_day
```

Weights live in one config file, are unit-tested with fixed fixtures, and get tuned once against
real scenarios. The LLM only *narrates* the top 3 — it never picks them. This is what turns
"smart-fallback logic" from a bullet into an algorithm we can defend.

### 3.6 Voice pipeline

```
Twilio (PSTN) ──WS── Pipecat ── VAD(Silero) ── STT ── LLM + tools ── TTS ── back
                          │
                          └── tool calls → HTTP → the same FastAPI endpoints
                          └── subscribes to slot.invalidated for its held slot
```

Three agents, routed by an orchestrator on intent: **Appointment**, **Hospital Info**, **Feedback**.
Each is a system prompt + an allowed tool subset — not three separate models.

**Until we have keys** (D5): `MockSTT` replays scripted transcripts, `MockLLM` runs a
deterministic intent table, `MockTTS` writes silence with correct timing. The whole conversation
suite runs in CI in seconds. Swapping to real providers is a config change, not a rewrite.

---

## 4. Repository layout

```
mediconnect/
├── backend/
│   ├── app/
│   │   ├── main.py                 FastAPI app, WS hub mount
│   │   ├── core/                   config, security (JWT), deps, exceptions
│   │   ├── models/                 SQLAlchemy 2.0 async models
│   │   ├── schemas/                Pydantic v2
│   │   ├── api/v1/                 auth, doctors, slots, appointments, admin
│   │   ├── services/
│   │   │   ├── slot_engine.py      §3.2
│   │   │   ├── booking.py          §3.3   ← most important file in the repo
│   │   │   ├── status.py           §3.4
│   │   │   └── fallback.py         §3.5
│   │   ├── realtime/               WS hub, Redis pub/sub bridge
│   │   └── metrics/                §6 instrumentation
│   ├── alembic/
│   └── tests/                      unit · integration · concurrency
├── voice/
│   ├── pipeline.py                 Pipecat assembly
│   ├── agents/                     appointment · info · feedback
│   ├── tools.py                    HTTP wrappers = the LLM's only capability
│   ├── providers/                  real/ and mock/ behind one interface
│   └── tests/                      scripted conversation suite
├── web/                            Next.js 15 · TS · Tailwind · shadcn
│   └── app/(patient|doctor|admin)/
├── eval/                           §6 harness, scenario fixtures, report generator
├── docker-compose.yml              postgres · redis · api · web
└── docs/                           ERD, sequence diagrams, API spec, review decks
```

---

## 5. Eight-week schedule

Owners follow the slide-9 split. **Week 1 starts the week of 24 Aug 2026.**

| Wk | Deliverable | Owner | Done when |
|----|-------------|-------|-----------|
| 1 | Repo, Docker Compose, CI, schema + Alembic migrations, seed data (20 doctors, 6 specializations) | Sanjay | `docker compose up` gives a populated DB |
| 2 | Auth + RBAC, doctor CRUD, `/doctors/search`, slot engine §3.2 | Sanjay | Search returns correct slots for a seeded week |
| 3 | Status service §3.4, WS hub, `PATCH /status` → browser updates live | Sanjay | Two browsers open; one flips status, other updates <1s |
| 3-4 | Booking §3.3: hold, confirm, reschedule, cancel | Riya | Full appointment lifecycle via API |
| 4 | **Concurrency test suite** — 100 parallel bookings on one slot | Riya | Exactly 1 succeeds, 99 get clean 409s. *This is the proof.* |
| 4-5 | Fallback ranking §3.5 + unit tests on fixed fixtures | Riya | Ranking is stable and explainable for 10 scenarios |
| 3-5 | Voice pipeline with mock providers, 3 agents, tool layer | Ananya | Scripted call books a real appointment end-to-end |
| 5-6 | **Mid-call invalidation recovery** — the headline demo | Ananya + Sanjay | Doctor flips status mid-call; agent recovers and rebooks |
| 5-7 | Patient / doctor / admin dashboards | Ananya + Riya | One-click status switch works; live availability visible |
| 7 | Evaluation harness §6, metrics collected, report generated | All | Numbers exist for every claim on the Review 3 slides |
| 7-8 | Real provider swap (if keys arrive), hardening, demo rehearsal | All | Two-device demo runs clean three times in a row |

Rule: **nothing integrates until it passes tests against the shared API contract.** The OpenAPI
spec is frozen at end of Week 2 and changes only by agreement — that is what lets three people
build in parallel.

---

## 6. How we prove it works

The deck has no evaluation slide. This is the highest-value thing we can add for Review 3.

| Metric | How measured | Target |
|--------|--------------|--------|
| Double-booking rate under load | 100 concurrent confirms on 1 slot × 50 runs | **0** |
| Status propagation latency | `t1 − t0` from `status_events` | p95 < 1000 ms |
| Intent recognition accuracy | 100 scripted utterances, 8 intents | > 90% |
| Booking task success rate | 30 end-to-end scripted calls | > 85% |
| Mid-call invalidation recovery | 20 injected invalidations | > 80% recovered without call drop |
| Voice round-trip latency | mic-end → speech-start, real providers | p95 < 2.5 s |
| Fallback acceptance | % of scenarios where top-1 alternate is the human-preferred one | > 70% |

`eval/` runs the whole suite and emits a markdown report. Run it weekly from Week 4 so the trend
line itself becomes a slide.

---

## 7. Working without API keys

| Component | Now | When keys arrive |
|-----------|-----|------------------|
| LLM | `MockLLM` — deterministic intent + tool-call table | Groq Llama 3.3 70B |
| STT | `MockSTT` — scripted transcripts with realistic timing | Deepgram or NVIDIA Riva |
| TTS | `MockTTS` — silent audio, correct duration | Kokoro (self-hosted, free) |
| Telephony | Local WS client simulating Twilio media frames | Twilio number |
| Everything else | **Fully real from day one** | — |

Kokoro runs locally at no cost, so TTS can go real early. Groq has a free tier — worth getting
that key by **Week 3** so the LLM is real before the mid-call demo is built. Twilio can wait to
Week 7; the demo can run browser-mic → WebSocket if a phone line never materializes.

---

## 8. Risks

| Risk | Mitigation |
|------|------------|
| Voice latency feels bad on a real call | Stream STT and TTS, keep the tool layer under 150 ms, pre-warm the LLM. Measure from Week 5, not Week 8. |
| The panel calls the novelty thin | Lead the Review 3 demo with mid-call invalidation recovery, not with "we built a booking system." Show the 0-double-bookings number. |
| Three people, one API, merge chaos | Contract frozen Week 2, OpenAPI-generated TS client for the frontend, CI blocks on contract drift. |
| Healthcare data / privacy questions | Consent announcement at call start, recordings off by default, 30-day retention, PII redacted in logs, explicit "no diagnosis" boundary. Add the slide. |
| Timeline slips | Weeks 1-6 are the defensible core. Dashboards can degrade to functional-not-pretty; real telephony can be cut entirely. |

---

## 9. Deck fixes before Review 3

1. Section numbers skip 05, 09, 10 and footer pages jump 3 → 8 → 15. Renumber.
2. Slide 5 blocks 3 and 4 are swapped — "Real-Time Data & Decision Layer" describes TTS output;
   "Response Generation" describes the dev sequence. Also fix "**he** selected agent."
3. Remove Supabase Realtime (D1). State Redis's actual job: slot holds, pub/sub, session state.
4. Replace "Groq Llama 3 / Mixtral" with "Groq — Llama 3.3 70B."
5. **Add an evaluation slide** (§6 table).
6. **Add a privacy & compliance slide** — reference [14] raises it; we should answer it.
7. Reorder references so IEEE / Springer / MDPI / arXiv lead.
8. Restate the novelty as §1 — booking safely against changing availability.
