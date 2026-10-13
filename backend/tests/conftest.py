# backend/tests/conftest.py
"""Every test leaves the database exactly as it found it.

The suite runs against the real Supabase database — the same one the demo
uses — so anything a test writes shows up on the admin dashboard. It used to:
tests booked appointments and *cancelled* them to clean up, and a single run
left a dozen cancelled rows behind. Sixty-one of them had piled up, along
with 33 slots stuck as `booked` with no appointment at all, silently hiding
real availability from patients.

This fixture wraps every test. Before it runs, note the highest appointment
and user ids and which slots are already booked. Afterwards, delete anything
created past those ids, release the slots they held, and release any slot a
test left `booked` without an appointment behind it.

Don't run the suite while demoing: an appointment someone books live during a
test run has an id past the floor too, and would be removed with the rest.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import SessionLocal  # noqa: E402
from models import Appointment, Slot, User  # noqa: E402


@pytest.fixture(autouse=True)
def leave_no_trace():
    db = SessionLocal()
    try:
        appointment_floor = db.scalar(select(func.coalesce(func.max(Appointment.id), 0)))
        user_floor = db.scalar(select(func.coalesce(func.max(User.id), 0)))
        booked_before = set(db.scalars(select(Slot.id).where(Slot.state == "booked")).all())
    finally:
        db.close()

    yield

    db = SessionLocal()
    try:
        # Appointments this test created, and the slots they were holding.
        for appointment in db.scalars(
            select(Appointment).where(Appointment.id > appointment_floor)
        ).all():
            slot = db.get(Slot, appointment.slot_id)
            if slot is not None and slot.id not in booked_before:
                slot.state = "open"
            db.delete(appointment)
        db.flush()

        # Patients this test created — callers identified by the voice tools.
        # Never the seeded accounts, which all sit below the floor.
        for user in db.scalars(
            select(User).where(User.id > user_floor, User.role == "patient")
        ).all():
            db.delete(user)

        # A test that deleted its own appointment without releasing the slot
        # leaves it booked with nothing behind it. That is how 33 orphans
        # accumulated; release any this test produced.
        confirmed_slots = select(Appointment.slot_id).where(Appointment.status == "confirmed")
        for slot in db.scalars(
            select(Slot).where(Slot.state == "booked", Slot.id.not_in(confirmed_slots))
        ).all():
            if slot.id not in booked_before:
                slot.state = "open"

        db.commit()
    finally:
        db.close()
