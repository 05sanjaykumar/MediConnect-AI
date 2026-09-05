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
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams
from sqlalchemy import select

from database import SessionLocal
from models import Appointment, Doctor, User
from services import booking
from services.booking import BookingError
from services.fallback import rank_alternates
from services.slot_engine import available_slots


@dataclass
class VoiceSession:
    """Who the agent is talking to, and what it has looked up so far.

    `offered_slots` is the whitelist. A slot id the model produces that isn't in
    here never reached it from a tool, which means it invented it.
    """

    patient_id: int | None = None
    patient_name: str | None = None
    caller_phone: str | None = None
    offered_slots: set[int] = field(default_factory=set)
    offered_doctors: set[int] = field(default_factory=set)

    def remember_doctors(self, doctor_ids) -> None:
        self.offered_doctors.update(doctor_ids)

    def remember_slots(self, slot_ids) -> None:
        self.offered_slots.update(slot_ids)


# --------------------------------------------------------------------- helpers


def _speakable_time(moment: datetime) -> str:
    """A time a text-to-speech engine will read naturally."""
    return moment.strftime("%A %d %B at %I:%M %p").replace(" 0", " ").replace("AM", "in the morning").replace("PM", "in the afternoon")


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
            from security import hash_password

            user = User(
                name=session.patient_name or f"Caller {session.caller_phone[-4:]}",
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

    raise BookingError("NO_CALLER", "I don't have your details on this call.")


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
                query = select(Doctor).where(Doctor.is_active.is_(True))
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
                return out
            finally:
                db.close()

        doctors = await asyncio.to_thread(work)
        session.remember_doctors(d["doctor_id"] for d in doctors)

        if not doctors:
            await params.result_callback({
                "found": 0,
                "message": "No doctors matched that. Ask the patient to name a "
                           "department, or offer to list the departments.",
            })
            return
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
                doctor = db.get(Doctor, doctor_id)
                if doctor is None:
                    return None
                slots = available_slots(db, doctor, day)
                status = doctor.status.status if doctor.status else "available"
                return {
                    "doctor_name": doctor.user.name,
                    "status": status.replace("_", " "),
                    "date": day.isoformat(),
                    "slots": [
                        {"slot_id": s.id, "time": s.start_at.strftime("%I:%M %p").lstrip("0")}
                        for s in slots[:8]
                    ],
                    "total_available": len(slots),
                }
            finally:
                db.close()

        result = await asyncio.to_thread(work)
        if result is None:
            await params.result_callback({"error": "No such doctor."})
            return

        session.remember_slots(s["slot_id"] for s in result["slots"])
        if not result["slots"]:
            result["message"] = (
                f"{result['doctor_name']} has nothing free that day. Offer another "
                "day, or another doctor in the same department."
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
                appointment = booking.book(
                    db, patient_id=patient_id, slot_id=slot_id,
                    created_via="voice", reason=reason,
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
        await params.result_callback(result)

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
            description="Find doctors by specialization or name, with their live "
                        "availability. Call this before naming any doctor.",
            properties={
                "specialization": {
                    "type": "string",
                    "description": "Department, e.g. Cardiology, Dermatology, "
                                   "Pediatrics. Map symptoms yourself: chest pain "
                                   "means Cardiology, a skin problem means Dermatology.",
                },
                "name": {"type": "string", "description": "Part of the doctor's name."},
            },
            required=[],
            handler=search_doctors,
        ),
        FunctionSchema(
            name="get_available_slots",
            description="List free appointment times for a doctor on a day. Call "
                        "this before offering any time.",
            properties={
                "doctor_id": {
                    "type": "integer",
                    "description": "An id from search_doctors. Never invent one.",
                },
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD, or 'today' or 'tomorrow'. Defaults to today.",
                },
            },
            required=["doctor_id"],
            handler=get_available_slots,
        ),
        FunctionSchema(
            name="book_appointment",
            description="Book a slot for this caller. Confirm the doctor, day and "
                        "time with them before calling this.",
            properties={
                "slot_id": {
                    "type": "integer",
                    "description": "An id from get_available_slots. Never invent one.",
                },
                "reason": {
                    "type": "string",
                    "description": "Briefly, why they want to be seen.",
                },
            },
            required=["slot_id"],
            handler=book_appointment,
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
                    "description": "An id from list_my_appointments.",
                },
            },
            required=["appointment_id"],
            handler=cancel_appointment,
        ),
        FunctionSchema(
            name="get_hospital_info",
            description="Departments, how many doctors are in each, fees and "
                        "opening hours.",
            properties={},
            required=[],
            handler=get_hospital_info,
        ),
    ]
