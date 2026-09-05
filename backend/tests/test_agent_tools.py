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
