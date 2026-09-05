# backend/agents/tools.py
"""The agent's hands.

Every tool here is a thin wrapper over the same service functions the REST API
uses. That is deliberate: a booking made by voice goes through the identical
validation, the identical status checks and the identical unique index as one
made in the browser. There is no second code path to keep in sync, and no way
for the voice channel to create a booking the web channel would have refused.

Two guardrails matter:

1. The model may only use ids that a tool handed it earlier in the conversation.
   It never types a doctor_id from memory, because it never sees the database.
2. Every write re-validates server-side. If the model hallucinates a slot id,
   booking.book raises and the model is told so, rather than silently creating
   a wrong appointment.

And one rule about speed: the caller is on the line, silent, while a tool runs.
Every database round trip is roughly half a second, so each tool is built to
answer in as few as possible — and to answer the *next* question too, so the
model doesn't have to call again.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database import SessionLocal
from models import Appointment, Doctor, User
from services import booking
from services.booking import BookingError
from services.fallback import rank_alternates
from services.slot_engine import available_slots, generate_for_doctors, next_available

PREFETCH_DAYS = 4  # after a search, warm this many days of slots in the background


@dataclass
class VoiceSession:
    """Who the agent is talking to, and what it has looked up so far.

    `offered_slots` is the whitelist. A slot id the model produces that isn't in
    here never reached it from a tool, which means it invented it.
    """

    patient_id: int | None = None
    patient_name: str | None = None
    caller_phone: str | None = None
    # Was this number already on record when the call started? Decides whether
    # a name the caller gives us renames a record we just made, or means the
    # booking is for someone other than the number's owner.
    known_at_start: bool = False
    # Set when a known caller books for another person ("for my mother").
    booking_for: str | None = None
    offered_slots: set[int] = field(default_factory=set)
    offered_doctors: set[int] = field(default_factory=set)

    def remember_doctors(self, doctor_ids) -> None:
        self.offered_doctors.update(doctor_ids)

    def remember_slots(self, slot_ids) -> None:
        self.offered_slots.update(slot_ids)


# --------------------------------------------------------------------- helpers


def _speakable_time(moment: datetime) -> str:
    """A time a text-to-speech engine will read naturally."""
    return (
        moment.strftime("%A %d %B at %I:%M %p")
        .replace(" 0", " ")
        .replace("AM", "in the morning")
        .replace("PM", "in the afternoon")
    )


def _speakable_day(day: date) -> str:
    today = date.today()
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return day.strftime("%A %d %B").replace(" 0", " ")


def _resolve_patient(db, session: VoiceSession) -> int:
    """Find (or create) the patient record for this caller.

    On a phone call we know the number but not the person. If we've seen the
    number before, that's them. If not, we create a record so the appointment
    has an owner — the front desk can fill in the name later.
    """
    if session.patient_id:
        return session.patient_id

    if session.caller_phone:
        user = db.scalar(select(User).where(User.phone == session.caller_phone))
        if user is None:
            if not session.patient_name:
                raise BookingError(
                    "NEED_NAME",
                    "I don't have this caller's name yet. Ask for it, then call "
                    "set_caller_details.",
                )
            from security import hash_password

            user = User(
                name=session.patient_name,
                phone=session.caller_phone,
                password_hash=hash_password("changeme"),
                role="patient",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        session.patient_id = user.id
        session.patient_name = user.name
        return user.id

    raise BookingError(
        "NEED_DETAILS",
        "I don't know who is calling. Ask for their name and phone number, then "
        "call set_caller_details.",
    )


# Names a model produces when it hasn't actually asked. Refusing these is what
# forces the question to be put to the caller.
PLACEHOLDER_NAMES = {
    "patient", "caller", "customer", "user", "unknown", "guest", "someone",
    "me", "myself", "n/a", "na", "none", "null", "test", "anonymous", "the patient",
}


def _looks_like_a_real_name(name: str) -> bool:
    cleaned = " ".join(name.split()).strip(" .,'\"")
    if len(cleaned) < 2 or cleaned.lower() in PLACEHOLDER_NAMES:
        return False
    return any(ch.isalpha() for ch in cleaned)


_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "double": "", "triple": "",  # "double five" is handled below
}


def _digits_in(text: str) -> str:
    """Every digit in `text`, in order — including ones spoken as words.

    Speech-to-text writes "9840012346", "98400 12346", "984-001-2346" or,
    occasionally, "nine eight four double zero". All of those must match.
    """
    out: list[str] = []
    repeat = 1
    for token in text.lower().replace("-", " ").split():
        cleaned = token.strip(".,;:!?()")
        if cleaned in ("double", "triple"):
            repeat = 2 if cleaned == "double" else 3
            continue
        if cleaned in _DIGIT_WORDS and _DIGIT_WORDS[cleaned]:
            out.append(_DIGIT_WORDS[cleaned] * repeat)
        else:
            out.append("".join(ch for ch in cleaned if ch.isdigit()))
        repeat = 1
    return "".join(out)


def _normalize_phone(raw: str) -> str | None:
    """Ten digits, or None. Tolerates +91, spaces, dashes and a leading 0."""
    digits = _digits_in(raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def _looks_fake_phone(digits: str) -> bool:
    """Numbers a model reaches for when it hasn't actually been given one."""
    if digits.startswith("555") or len(set(digits)) == 1:
        return True
    return digits in ("1234567890", "0123456789", "9876543210")


def _spoken_by_caller(context, digits: str) -> bool:
    """Did these ten digits actually occur in something the caller said?

    This is the guard that matters. The model invented 5551234567 for a
    caller who had only given their name — and wrote the caller's "answer"
    itself. The tool can see the transcript, so it checks.
    """
    if context is None:  # unit tests without a pipeline context
        return True
    try:
        messages = context.get_messages()
    except Exception:
        return True
    heard = "".join(
        _digits_in(m.get("content") or "")
        for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    )
    return digits in heard


def _spoken_digits(digits: str) -> str:
    """'9840012346' -> '9 8 4 0 0, 1 2 3, 4 6' — read as digits, not billions."""
    groups = (digits[:5], digits[5:8], digits[8:])
    return ", ".join(" ".join(g) for g in groups if g)


def _parse_day(value: str | None) -> date:
    """Accept an ISO date, or the words the model is most likely to produce."""
    if not value:
        return date.today()
    text = value.strip().lower()
    if text in ("today", "now"):
        return date.today()
    if text == "tomorrow":
        return date.today() + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return date.today()


def _load_doctor(db, doctor_id: int) -> Doctor | None:
    """Doctor with user and status in a single round trip."""
    return db.scalar(
        select(Doctor)
        .options(joinedload(Doctor.user), joinedload(Doctor.status))
        .where(Doctor.id == doctor_id)
    )


def _prefetch_slots(doctor_ids: list[int]) -> None:
    """Warm the next few days of slots for these doctors.

    Runs in the background right after a search, so by the time the model asks
    for times — a second or two later — the slots already exist and the lookup
    is one query instead of three.
    """
    db = SessionLocal()
    try:
        doctors = list(db.scalars(select(Doctor).where(Doctor.id.in_(doctor_ids))).all())
        days = [date.today() + timedelta(days=i) for i in range(PREFETCH_DAYS)]
        made = generate_for_doctors(db, doctors, days)
        if made:
            logger.debug(f"Prefetched {made} slots for doctors {doctor_ids}")
    except Exception as exc:  # never let a warm-up break the call
        logger.warning(f"Slot prefetch failed: {exc}")
    finally:
        db.close()


# ----------------------------------------------------------------------- tools


def build_tools(session: VoiceSession) -> list[FunctionSchema]:
    """Build the tool set, bound to this caller's session."""

    # Each handler runs the database work in a thread. The pipeline is async and
    # SQLAlchemy here is synchronous — calling it directly would block the event
    # loop, which on a phone call is heard as the agent freezing mid-sentence.

    async def search_doctors(params: FunctionCallParams):
        specialization = params.arguments.get("specialization")
        name = params.arguments.get("name")

        def work():
            db = SessionLocal()
            try:
                query = (
                    select(Doctor)
                    .options(joinedload(Doctor.user), joinedload(Doctor.status))
                    .where(Doctor.is_active.is_(True))
                )
                if specialization:
                    query = query.where(Doctor.specialization.ilike(f"%{specialization}%"))
                if name:
                    query = query.join(User, User.id == Doctor.user_id).where(
                        User.name.ilike(f"%{name}%")
                    )
                doctors = list(db.scalars(query).all())
                out = []
                for d in doctors:
                    status = d.status.status if d.status else "available"
                    out.append({
                        "doctor_id": d.id,
                        "name": d.user.name,
                        "specialization": d.specialization,
                        "consultation_fee": d.consultation_fee,
                        "status": status.replace("_", " "),
                        "accepting_appointments": status == "available",
                    })
                departments: list[str] = []
                if not out:
                    # Tell the model what actually exists, so it doesn't guess
                    # another name and call again.
                    departments = list(
                        db.scalars(
                            select(Doctor.specialization).distinct().order_by(Doctor.specialization)
                        ).all()
                    )
                return out, departments
            finally:
                db.close()

        doctors, departments = await asyncio.to_thread(work)
        session.remember_doctors(d["doctor_id"] for d in doctors)

        if not doctors:
            await params.result_callback({
                "found": 0,
                "departments_we_have": departments,
                "message": "We don't have that department. Tell the patient so, read "
                           "out departments_we_have, and ask which they'd like. Do not "
                           "choose a substitute for them.",
            })
            return

        # Warm the slots for these doctors while the model is still talking.
        asyncio.create_task(asyncio.to_thread(_prefetch_slots, [d["doctor_id"] for d in doctors]))

        await params.result_callback({"found": len(doctors), "doctors": doctors})

    async def get_available_slots(params: FunctionCallParams):
        doctor_id = params.arguments.get("doctor_id")
        day = _parse_day(params.arguments.get("date"))

        if doctor_id not in session.offered_doctors:
            await params.result_callback({
                "error": "You haven't looked that doctor up yet. Call search_doctors first.",
            })
            return

        def work():
            db = SessionLocal()
            try:
                doctor = _load_doctor(db, doctor_id)
                if doctor is None:
                    return None
                status = doctor.status.status if doctor.status else "available"
                slots = available_slots(db, doctor, day)
                result = {
                    "doctor_name": doctor.user.name,
                    "status": status.replace("_", " "),
                    "date": day.isoformat(),
                    "day": _speakable_day(day),
                    "slots": [
                        {"slot_id": s.id, "time": s.start_at.strftime("%I:%M %p").lstrip("0")}
                        for s in slots[:8]
                    ],
                    "total_available": len(slots),
                }
                if not slots:
                    # Answer the follow-up question now, so the model doesn't
                    # walk forward a day at a time asking again and again.
                    soonest = next_available(db, doctor)
                    if soonest is not None:
                        result["next_available"] = {
                            "date": soonest.start_at.date().isoformat(),
                            "day": _speakable_day(soonest.start_at.date()),
                            "slot_id": soonest.id,
                            "time": soonest.start_at.strftime("%I:%M %p").lstrip("0"),
                        }
                return result
            finally:
                db.close()

        result = await asyncio.to_thread(work)
        if result is None:
            await params.result_callback({"error": "No such doctor."})
            return

        session.remember_slots(s["slot_id"] for s in result["slots"])
        if not result["slots"]:
            nxt = result.get("next_available")
            if nxt:
                session.remember_slots([nxt["slot_id"]])
                result["message"] = (
                    f"Nothing free {result['day']}. The next opening is {nxt['day']} "
                    f"at {nxt['time']} — offer that. Do not check other days."
                )
            else:
                result["message"] = (
                    f"{result['doctor_name']} has nothing in the next week. Offer "
                    "another doctor in the same department."
                )
        await params.result_callback(result)

    async def book_appointment(params: FunctionCallParams):
        slot_id = params.arguments.get("slot_id")
        reason = params.arguments.get("reason")

        if slot_id not in session.offered_slots:
            await params.result_callback({
                "error": "That time wasn't one of the ones you offered. Call "
                         "get_available_slots and offer a real time.",
            })
            return

        def work():
            db = SessionLocal()
            try:
                patient_id = _resolve_patient(db, session)
                # A new name here, not `reason`: assigning to `reason` inside this
                # closure would make it local to work() and shadow the outer value.
                note = reason
                if session.booking_for:
                    note = f"{reason or 'Appointment'} (for {session.booking_for})"
                appointment = booking.book(
                    db, patient_id=patient_id, slot_id=slot_id,
                    created_via="voice", reason=note,
                )
                return {
                    "booked": True,
                    "doctor_name": appointment.doctor.user.name,
                    "when": _speakable_time(appointment.slot.start_at),
                    "appointment_id": appointment.id,
                }
            except BookingError as err:
                alternates = []
                if err.code in ("SLOT_TAKEN", "SLOT_INVALIDATED", "DOCTOR_UNAVAILABLE"):
                    from models import Slot

                    slot = db.get(Slot, slot_id)
                    if slot is not None:
                        alternates = rank_alternates(
                            db, slot.doctor_id, slot.start_at, session.patient_id
                        )
                return {
                    "booked": False,
                    "code": err.code,
                    "reason": err.message,
                    "alternatives": [
                        {"slot_id": a["slot_id"], "doctor_name": a["doctor_name"],
                         "when": _speakable_time(a["start_at"])}
                        for a in alternates
                    ],
                }
            finally:
                db.close()

        result = await asyncio.to_thread(work)
        if not result.get("booked") and result.get("alternatives"):
            session.remember_slots(a["slot_id"] for a in result["alternatives"])
            result["instruction"] = (
                "Apologise briefly, then offer the FIRST alternative only, in one "
                "sentence, and ask if that works."
            )
        elif result.get("code") in ("NEED_NAME", "NEED_DETAILS"):
            result["instruction"] = (
                "Do not book yet. Ask the caller's name (and phone number if you "
                "don't have it), call set_caller_details, then book_appointment again "
                "with the same slot_id."
            )
        await params.result_callback(result)

    async def set_caller_details(params: FunctionCallParams):
        """Record who we're talking to. The only way an unknown caller gets a name."""
        name = " ".join((params.arguments.get("name") or "").split())
        raw_phone = (params.arguments.get("phone") or "").strip() or None

        if not _looks_like_a_real_name(name):
            await params.result_callback({
                "ok": False,
                "error": "That isn't a name the caller gave you. Ask: \"May I have your "
                         "name, please?\" and stop. Call this again with what they say.",
            })
            return

        if raw_phone:
            digits = _normalize_phone(raw_phone)
            if digits is None or _looks_fake_phone(digits):
                await params.result_callback({
                    "ok": False,
                    "error": "That is not a valid phone number the caller gave you. Ask: "
                             "\"May I have your phone number, please?\" and stop. Pass "
                             "exactly the digits they read out.",
                })
                return
            if not _spoken_by_caller(getattr(params, "context", None), digits):
                await params.result_callback({
                    "ok": False,
                    "error": "The caller has not said that number. Never make one up. "
                             "Ask: \"May I have your phone number, please?\" and stop.",
                })
                return
            session.caller_phone = digits

        if not session.caller_phone:
            await params.result_callback({
                "ok": False,
                "name_recorded": name,
                "error": "Got the name. Now ask: \"And your phone number, please?\" and "
                         "stop — I need it to file the booking.",
            })
            return

        def work():
            db = SessionLocal()
            try:
                user = db.scalar(select(User).where(User.phone == session.caller_phone))
                if user is None:
                    from security import hash_password

                    user = User(
                        name=name,
                        phone=session.caller_phone,
                        password_hash=hash_password("changeme"),
                        role="patient",
                    )
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    created = True
                else:
                    created = False
                if user.name.lower() != name.lower():
                    if session.known_at_start:
                        # A known number, a different name: the owner is booking
                        # for someone else. File it under the owner, note who
                        # it's for. Never rename the owner's record.
                        session.patient_id = user.id
                        session.patient_name = user.name
                        session.booking_for = name
                        return {"ok": True, "account_holder": user.name, "booking_for": name}
                    # A number we first met on this call, and the caller has now
                    # told us (or corrected) their name: rename the record.
                    user.name = name
                    db.commit()
                session.patient_id = user.id
                session.patient_name = user.name
                session.booking_for = None
                return {
                    "ok": True,
                    "name": user.name,
                    "new_patient": created,
                    # If you repeat the number back, say it exactly like this.
                    "phone_spoken": _spoken_digits(session.caller_phone),
                }
            finally:
                db.close()

        await params.result_callback(await asyncio.to_thread(work))

    async def list_my_appointments(params: FunctionCallParams):
        def work():
            db = SessionLocal()
            try:
                if not session.patient_id:
                    _resolve_patient(db, session)
                rows = booking.appointments_for_patient(db, session.patient_id)
                return [
                    {"appointment_id": a.id, "doctor_name": a.doctor.user.name,
                     "when": _speakable_time(a.slot.start_at)}
                    for a in rows
                ]
            except BookingError:
                return []
            finally:
                db.close()

        appointments = await asyncio.to_thread(work)
        await params.result_callback(
            {"count": len(appointments), "appointments": appointments}
        )

    async def cancel_appointment(params: FunctionCallParams):
        appointment_id = params.arguments.get("appointment_id")

        def work():
            db = SessionLocal()
            try:
                existing = db.get(Appointment, appointment_id)
                # Only the caller's own appointments, whatever the model says.
                if existing is None or existing.patient_id != session.patient_id:
                    return {"cancelled": False,
                            "reason": "I can't find that appointment under your number."}
                appointment = booking.cancel(db, appointment_id)
                return {"cancelled": True,
                        "doctor_name": appointment.doctor.user.name,
                        "when": _speakable_time(appointment.slot.start_at)}
            except BookingError as err:
                return {"cancelled": False, "reason": err.message}
            finally:
                db.close()

        await params.result_callback(await asyncio.to_thread(work))

    async def get_hospital_info(params: FunctionCallParams):
        def work():
            db = SessionLocal()
            try:
                rows = db.execute(
                    select(Doctor.specialization, Doctor.consultation_fee)
                ).all()
                departments: dict[str, list[float]] = {}
                for spec, fee in rows:
                    departments.setdefault(spec, []).append(fee)
                return {
                    "departments": [
                        {"name": name, "doctors": len(fees),
                         "consultation_from": min(fees)}
                        for name, fees in sorted(departments.items())
                    ],
                    "opening_hours": "nine in the morning to five in the evening, Monday to Saturday",
                }
            finally:
                db.close()

        await params.result_callback(await asyncio.to_thread(work))

    return [
        FunctionSchema(
            name="search_doctors",
            description="Find doctors by specialization or name, with live availability. "
                        "On no match, returns our departments — pick the closest and retry.",
            properties={
                "specialization": {
                    "type": "string",
                    "description": "Department, e.g. Cardiology, Dermatology, Pediatrics, "
                                   "General Medicine. Map symptoms yourself: chest pain → "
                                   "Cardiology, skin → Dermatology, check-up → General Medicine.",
                },
                "name": {"type": "string", "description": "Part of the doctor's name."},
            },
            required=[],
            handler=search_doctors,
        ),
        FunctionSchema(
            name="get_available_slots",
            description="Free times for a doctor on a day. If empty, includes "
                        "next_available — offer that; don't check more days.",
            properties={
                "doctor_id": {
                    "type": "integer",
                    "description": "From search_doctors.",
                },
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD, 'today' or 'tomorrow'. Default today.",
                },
            },
            required=["doctor_id"],
            handler=get_available_slots,
        ),
        FunctionSchema(
            name="book_appointment",
            description="Book a slot for this caller after confirming doctor, day and time.",
            properties={
                "slot_id": {
                    "type": "integer",
                    "description": "From get_available_slots.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why they want to be seen, briefly.",
                },
            },
            required=["slot_id"],
            handler=book_appointment,
        ),
        FunctionSchema(
            name="set_caller_details",
            description="Record the caller's spoken name (and phone if we lack it). "
                        "Needed before booking an unknown number or for another person.",
            properties={
                "name": {"type": "string", "description": "Full name, as the caller said it."},
                "phone": {"type": "string", "description": "Digits only; only if we don't have it."},
            },
            required=["name"],
            handler=set_caller_details,
        ),
        FunctionSchema(
            name="list_my_appointments",
            description="The caller's upcoming appointments.",
            properties={},
            required=[],
            handler=list_my_appointments,
        ),
        FunctionSchema(
            name="cancel_appointment",
            description="Cancel one of the caller's appointments.",
            properties={
                "appointment_id": {
                    "type": "integer",
                    "description": "From list_my_appointments.",
                },
            },
            required=["appointment_id"],
            handler=cancel_appointment,
        ),
        FunctionSchema(
            name="get_hospital_info",
            description="Departments, doctor counts, fees, opening hours. General "
                        "questions only — not needed before a search.",
            properties={},
            required=[],
            handler=get_hospital_info,
        ),
    ]
