# backend/tests/test_agent_tools.py
"""Drive the agent's tools directly, with no audio in the way.

This is the cheap way to test a voice agent: the pipeline's job is to turn
speech into tool calls, so if the tools behave correctly under every case the
agent can produce, the only thing left to test over a real call is the audio.

    .venv/bin/python -m pytest tests/test_agent_tools.py -v
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.tools import VoiceSession, build_tools  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import Appointment, Doctor, User  # noqa: E402


class FakeParams:
    """Stands in for Pipecat's FunctionCallParams."""

    def __init__(self, **arguments):
        self.arguments = arguments
        self.result = None

    async def result_callback(self, value):
        self.result = value


def call(tool, **arguments):
    params = FakeParams(**arguments)
    asyncio.run(tool.handler(params))
    return params.result


@pytest.fixture
def session():
    db = SessionLocal()
    patient = db.query(User).filter(User.phone == "9840012346").first()
    db.close()
    return VoiceSession(patient_id=patient.id, patient_name=patient.name)


@pytest.fixture
def tools(session):
    return {t.name: t for t in build_tools(session)}


# ------------------------------------------------------------------- discovery


def test_search_by_specialization(tools, session):
    result = call(tools["search_doctors"], specialization="cardio")
    assert result["found"] >= 2
    assert all("Cardio" in d["specialization"] for d in result["doctors"])
    # The ids are now whitelisted for this conversation.
    assert session.offered_doctors


def test_search_returns_nothing_gracefully(tools):
    result = call(tools["search_doctors"], specialization="astrology")
    assert result["found"] == 0
    assert "message" in result, "the agent needs telling what to say"


def test_hospital_info_lists_departments(tools):
    result = call(tools["get_hospital_info"])
    names = [d["name"] for d in result["departments"]]
    assert "Cardiology" in names
    assert all(d["doctors"] > 0 for d in result["departments"])


# ------------------------------------------------------------------ guardrails


def test_cannot_get_slots_for_a_doctor_never_looked_up(tools):
    """The model inventing a doctor_id must not reach the database."""
    result = call(tools["get_available_slots"], doctor_id=999)
    assert "error" in result
    assert "search_doctors" in result["error"]


def test_cannot_book_a_slot_never_offered(tools, session):
    """The single most important guardrail: no booking from a made-up id."""
    call(tools["search_doctors"], specialization="cardio")
    result = call(tools["book_appointment"], slot_id=999999)
    assert "error" in result
    assert "wasn't one of the ones you offered" in result["error"]


def test_slots_are_whitelisted_once_offered(tools, session):
    call(tools["search_doctors"], specialization="cardio")
    doctor_id = next(iter(session.offered_doctors))
    day = date.today() + timedelta(days=2)
    call(tools["get_available_slots"], doctor_id=doctor_id, date=day.isoformat())
    assert session.offered_slots, "offered slots should be remembered"


# --------------------------------------------------------------------- booking


def _next_working_day(offset: int = 1) -> date:
    day = date.today() + timedelta(days=offset)
    while day.weekday() > 5:  # doctors work Monday to Saturday
        day += timedelta(days=1)
    return day


def test_book_then_cancel_end_to_end(tools, session):
    call(tools["search_doctors"], specialization="cardio")
    doctor_id = sorted(session.offered_doctors)[0]

    slots = call(tools["get_available_slots"], doctor_id=doctor_id,
                 date=_next_working_day(3).isoformat())
    assert slots["slots"], "no slots to book in the test window"
    slot_id = slots["slots"][-1]["slot_id"]

    booked = call(tools["book_appointment"], slot_id=slot_id, reason="chest pain")
    assert booked["booked"] is True
    assert booked["doctor_name"]
    # The time must be speakable, not an ISO timestamp.
    assert ":" not in booked["when"].split()[-1] or "in the" in booked["when"]

    listed = call(tools["list_my_appointments"])
    assert any(a["appointment_id"] == booked["appointment_id"] for a in listed["appointments"])

    cancelled = call(tools["cancel_appointment"],
                     appointment_id=booked["appointment_id"])
    assert cancelled["cancelled"] is True


def test_double_booking_returns_alternatives(tools, session):
    """The recovery path the agent reads out when a slot is gone."""
    call(tools["search_doctors"], specialization="cardio")
    doctor_id = sorted(session.offered_doctors)[0]
    slots = call(tools["get_available_slots"], doctor_id=doctor_id,
                 date=_next_working_day(4).isoformat())
    assert slots["slots"]
    slot_id = slots["slots"][-1]["slot_id"]

    first = call(tools["book_appointment"], slot_id=slot_id)
    assert first["booked"] is True

    try:
        # A second caller wants the same time.
        other = VoiceSession(patient_id=_another_patient_id())
        other_tools = {t.name: t for t in build_tools(other)}
        other.remember_slots([slot_id])
        clash = call(other_tools["book_appointment"], slot_id=slot_id)

        assert clash["booked"] is False
        assert clash["alternatives"], "the agent has nothing to offer the caller"
        assert "instruction" in clash, "the agent needs telling to offer only the first"
        # Alternatives must be speakable and usable.
        assert all("when" in a and "slot_id" in a for a in clash["alternatives"])
    finally:
        call(tools["cancel_appointment"], appointment_id=first["appointment_id"])


def _another_patient_id() -> int:
    db = SessionLocal()
    try:
        return db.query(User).filter(User.phone == "9840012347").first().id
    finally:
        db.close()


def test_cannot_cancel_someone_elses_appointment(tools, session):
    """Even if the model produces a valid id belonging to another patient."""
    call(tools["search_doctors"], specialization="cardio")
    doctor_id = sorted(session.offered_doctors)[0]
    slots = call(tools["get_available_slots"], doctor_id=doctor_id,
                 date=_next_working_day(5).isoformat())
    slot_id = slots["slots"][-1]["slot_id"]
    booked = call(tools["book_appointment"], slot_id=slot_id)

    try:
        intruder = VoiceSession(patient_id=_another_patient_id())
        intruder_tools = {t.name: t for t in build_tools(intruder)}
        result = call(intruder_tools["cancel_appointment"],
                      appointment_id=booked["appointment_id"])
        assert result["cancelled"] is False

        db = SessionLocal()
        still_there = db.get(Appointment, booked["appointment_id"])
        assert still_there.status == "confirmed"
        db.close()
    finally:
        call(tools["cancel_appointment"], appointment_id=booked["appointment_id"])


def test_unavailable_doctor_is_reported_not_hidden(tools):
    """A doctor in surgery should still be findable, marked as not accepting."""
    db = SessionLocal()
    doctor = (
        db.query(Doctor)
        .join(User)
        .filter(User.name == "Dr. Vikram Choudhary")
        .first()
    )
    name = doctor.user.name
    db.close()

    result = call(tools["search_doctors"], name="Vikram")
    assert result["found"] == 1
    entry = result["doctors"][0]
    assert entry["name"] == name
    assert "_" not in entry["status"], "status must be speakable, not a code"


# ------------------------------------------------------------ caller identity


UNKNOWN_PHONE = "9111100001"


def _delete_user_by_phone(phone: str) -> None:
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.phone == phone).first()
        if u:
            for a in db.query(Appointment).filter(Appointment.patient_id == u.id).all():
                db.delete(a)
            db.delete(u)
            db.commit()
    finally:
        db.close()


def test_prompt_identity_block_covers_all_three_cases():
    from agents.prompt import build_system_prompt

    known = VoiceSession(patient_id=3, patient_name="Sanjay Kumar", caller_phone="9840012346")
    prompt = build_system_prompt(known)
    assert "The caller is Sanjay Kumar" in prompt and "on record" in prompt
    assert "Identity is confirmed" in prompt
    assert "Is this booking for" not in prompt, "the old repeat-question instruction is back"

    unknown = VoiceSession(caller_phone=UNKNOWN_PHONE)
    assert "not on record" in build_system_prompt(unknown)
    assert "set_caller_details" in build_system_prompt(unknown)

    nobody = VoiceSession()
    assert "name and their phone number" in build_system_prompt(nobody)


def test_unknown_caller_cannot_book_without_a_name():
    """A new number must never get a made-up patient record."""
    _delete_user_by_phone(UNKNOWN_PHONE)
    session = VoiceSession(caller_phone=UNKNOWN_PHONE)
    tools = {t.name: t for t in build_tools(session)}

    call(tools["search_doctors"], specialization="cardio")
    doctor_id = sorted(session.offered_doctors)[0]
    slots = call(tools["get_available_slots"], doctor_id=doctor_id,
                 date=_next_working_day(6).isoformat())
    slot_id = slots["slots"][-1]["slot_id"]

    result = call(tools["book_appointment"], slot_id=slot_id)
    assert result["booked"] is False
    assert result["code"] == "NEED_NAME"
    assert "set_caller_details" in result["instruction"]

    db = SessionLocal()
    assert db.query(User).filter(User.phone == UNKNOWN_PHONE).first() is None, \
        "a patient record was created without a name"
    db.close()


def test_unknown_caller_books_after_giving_a_name():
    _delete_user_by_phone(UNKNOWN_PHONE)
    session = VoiceSession(caller_phone=UNKNOWN_PHONE)
    tools = {t.name: t for t in build_tools(session)}
    try:
        call(tools["search_doctors"], specialization="cardio")
        doctor_id = sorted(session.offered_doctors)[0]
        slots = call(tools["get_available_slots"], doctor_id=doctor_id,
                     date=_next_working_day(6).isoformat())
        slot_id = slots["slots"][-1]["slot_id"]

        details = call(tools["set_caller_details"], name="Priya Test")
        assert details["ok"] is True
        assert details["new_patient"] is True

        booked = call(tools["book_appointment"], slot_id=slot_id, reason="check-up")
        assert booked["booked"] is True

        db = SessionLocal()
        user = db.query(User).filter(User.phone == UNKNOWN_PHONE).first()
        assert user is not None and user.name == "Priya Test"
        appt = db.get(Appointment, booked["appointment_id"])
        assert appt.patient_id == user.id and appt.created_via == "voice"
        db.close()
    finally:
        _delete_user_by_phone(UNKNOWN_PHONE)


def test_set_caller_details_needs_a_real_name():
    session = VoiceSession(caller_phone=UNKNOWN_PHONE)
    tools = {t.name: t for t in build_tools(session)}
    assert call(tools["set_caller_details"], name="")["ok"] is False
    assert call(tools["set_caller_details"], name="A")["ok"] is False


def test_caller_with_no_phone_at_all_is_asked_for_both():
    session = VoiceSession()  # browser with nothing configured
    tools = {t.name: t for t in build_tools(session)}
    # name alone isn't enough — we need somewhere to file the booking
    result = call(tools["set_caller_details"], name="Someone New")
    assert result["ok"] is False
    assert "phone" in result["error"].lower()


# ------------------------------------------------- identity: the harder cases


def test_placeholder_names_are_rejected():
    """The model must not be able to satisfy the guard with 'Patient'."""
    session = VoiceSession(caller_phone=UNKNOWN_PHONE)
    tools = {t.name: t for t in build_tools(session)}
    for fake in ("Patient", "caller", "the patient", "unknown", "Me", "N/A"):
        result = call(tools["set_caller_details"], name=fake)
        assert result["ok"] is False, f"accepted placeholder {fake!r}"
        assert "May I have your name" in result["error"]
    db = SessionLocal()
    assert db.query(User).filter(User.phone == UNKNOWN_PHONE).first() is None
    db.close()


def test_correcting_the_name_renames_not_forks():
    """A number first met on this call: a later name corrects the record."""
    _delete_user_by_phone(UNKNOWN_PHONE)
    session = VoiceSession(caller_phone=UNKNOWN_PHONE)  # known_at_start=False
    tools = {t.name: t for t in build_tools(session)}
    try:
        assert call(tools["set_caller_details"], name="Pria Test")["ok"]
        assert call(tools["set_caller_details"], name="Priya Test")["ok"]
        db = SessionLocal()
        rows = db.query(User).filter(User.phone == UNKNOWN_PHONE).all()
        assert len(rows) == 1, "a second record was created for the same number"
        assert rows[0].name == "Priya Test"
        assert session.patient_id == rows[0].id and session.booking_for is None
        db.close()
    finally:
        _delete_user_by_phone(UNKNOWN_PHONE)


def test_known_caller_booking_for_someone_else_files_under_owner(tools, session):
    """Sanjay books for his mother: his record stays his, the note says who for."""
    session.caller_phone = "9840012346"
    session.known_at_start = True
    assert call(tools["set_caller_details"], name="Lakshmi Kumar")["booking_for"] == "Lakshmi Kumar"

    call(tools["search_doctors"], specialization="cardio")
    doctor_id = sorted(session.offered_doctors)[0]
    slots = call(tools["get_available_slots"], doctor_id=doctor_id,
                 date=_next_working_day(7).isoformat())
    slot_id = slots["slots"][-1]["slot_id"]
    booked = call(tools["book_appointment"], slot_id=slot_id, reason="check-up")
    try:
        assert booked["booked"] is True
        db = SessionLocal()
        appt = db.get(Appointment, booked["appointment_id"])
        owner = db.query(User).filter(User.phone == "9840012346").first()
        assert appt.patient_id == owner.id, "booked under the wrong record"
        assert owner.name == "Sanjay Kumar", "the owner's record was renamed"
        assert "(for Lakshmi Kumar)" in appt.reason
        db.close()
    finally:
        call(tools["cancel_appointment"], appointment_id=booked["appointment_id"])



# ------------------------------------------------------------------- greeting


def test_greeting_for_all_three_cases():
    from agents.prompt import greeting_for

    known = VoiceSession(patient_id=3, patient_name="Sanjay Kumar", caller_phone="9840012346")
    assert greeting_for(known) == "Hello Sanjay, this is MediConnect. How can I help you today?"
    assert greeting_for(VoiceSession(caller_phone=UNKNOWN_PHONE)) == \
        "Hello, this is MediConnect. May I have your name, please?"
    assert "name and phone number" in greeting_for(VoiceSession())


def test_greeting_is_the_assistants_first_line_in_context():
    """The model must know it already spoke, or it introduces itself twice."""
    from services.LLM import get_llm_context
    from agents.prompt import greeting_for

    session = VoiceSession(patient_id=3, patient_name="Sanjay Kumar", caller_phone="9840012346")
    pair = get_llm_context(session)
    context = pair.user().context
    messages = context.get_messages()
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == greeting_for(session)


# ---------------------------------------------------------------- tts chunking


def _chunks(text: str) -> list[str]:
    from services.TTS import ClauseTextAggregator
    from pipecat.utils.text.base_text_aggregator import AggregationType

    async def run():
        agg = ClauseTextAggregator(aggregation_type=AggregationType.SENTENCE)
        out = []
        for ch in text:
            async for a in agg.aggregate(ch):
                out.append(a.text)
        tail = await agg.flush()
        if tail is not None:
            out.append(tail.text)
        return out

    return asyncio.run(run())


def test_long_sentence_is_cut_at_a_clause():
    """The 8-second sentence from the call log should start speaking sooner."""
    text = ("We don't have a neurologist right now, but an ENT doctor, Dr. Fatima Sheikh, "
            "is free on Monday the seventh of September at nine in the morning. ")
    chunks = _chunks(text)
    assert len(chunks) >= 2, chunks
    assert chunks[0].endswith(","), "first chunk should end at a clause boundary"
    assert "".join(c.strip() for c in chunks).replace(" ", "") == text.replace(" ", "").strip(), \
        "chunking must not lose or duplicate text"


def test_short_sentences_are_left_whole():
    chunks = _chunks("Your appointment is booked. Dr. Lakshmi Venkat, Monday at nine. ")
    assert chunks[0] == "Your appointment is booked."
    assert "Dr. Lakshmi Venkat, Monday at nine." in chunks[1]


def test_punctuation_only_fragments_are_dropped():
    """The model emits '..' on its own; that must not become a TTS call."""
    assert _chunks(".. ") == []
    assert _chunks("Hello there. .. ") == ["Hello there."]
