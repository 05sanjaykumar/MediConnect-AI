# backend/tests/test_booking.py
"""Booking rules, and the concurrency guarantee.

Run from the backend directory:

    .venv/bin/python -m pytest tests -v
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import SessionLocal  # noqa: E402
from models import Appointment, Doctor, Slot, User  # noqa: E402
from services import booking  # noqa: E402
from services.availability import is_slot_bookable  # noqa: E402
from services.booking import BookingError  # noqa: E402
from services.slot_engine import available_slots, generate_for_day  # noqa: E402


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def doctor(db):
    return db.query(Doctor).join(User).filter(User.name == "Dr. Anitha Sharma").first()


@pytest.fixture
def patient(db):
    return db.query(User).filter(User.phone == "9840012346").first()


@pytest.fixture
def free_slot(db, doctor):
    """A slot far enough ahead that other tests won't have taken it."""
    day = date.today() + timedelta(days=5)
    generate_for_day(db, doctor, day)
    slots = available_slots(db, doctor, day)
    assert slots, "no free slots to test with — is the doctor's schedule seeded?"
    return slots[-1]


# ------------------------------------------------------------- the slot engine


def test_generation_is_idempotent(db, doctor):
    """Generating a day twice must never change how many slots exist.

    The database persists between runs, so this counts rows rather than
    assuming the first call is the one that creates them.
    """
    day = date.today() + timedelta(days=6)
    start = datetime.combine(day, datetime.min.time())

    def slot_count() -> int:
        return (
            db.query(Slot)
            .filter(
                Slot.doctor_id == doctor.id,
                Slot.start_at >= start,
                Slot.start_at < start + timedelta(days=1),
            )
            .count()
        )

    generate_for_day(db, doctor, day)
    after_first = slot_count()
    assert after_first > 0, "no slots generated — is the doctor's schedule seeded?"

    created = generate_for_day(db, doctor, day)
    assert created == 0, "second generation reported creating new slots"
    assert slot_count() == after_first, "generating twice duplicated slots"


def test_slots_respect_slot_duration(db, doctor):
    day = date.today() + timedelta(days=6)
    slots = available_slots(db, doctor, day)
    gap = (slots[1].start_at - slots[0].start_at).total_seconds() / 60
    assert gap == doctor.slot_minutes


def test_past_slots_are_never_offered(db, doctor):
    for slot in available_slots(db, doctor, date.today()):
        assert slot.start_at > datetime.now()


# ------------------------------------------------------------- status blocking


def test_in_surgery_blocks_the_rest_of_the_day():
    tomorrow_9am = datetime.now().replace(hour=9, minute=0) + timedelta(days=1)
    in_an_hour = datetime.now() + timedelta(hours=1)

    assert is_slot_bookable("in_surgery", in_an_hour) is False
    assert is_slot_bookable("in_surgery", tomorrow_9am) is True  # tomorrow is fine
    assert is_slot_bookable("available", in_an_hour) is True


def test_on_break_blocks_only_half_an_hour():
    assert is_slot_bookable("on_break", datetime.now() + timedelta(minutes=10)) is False
    assert is_slot_bookable("on_break", datetime.now() + timedelta(minutes=45)) is True


# -------------------------------------------------------------------- booking


def test_book_and_cancel(db, patient, free_slot):
    appointment = booking.book(db, patient.id, free_slot.id, created_via="voice")
    assert appointment.status == "confirmed"
    assert appointment.created_via == "voice"

    db.refresh(free_slot)
    assert free_slot.state == "booked"

    booking.cancel(db, appointment.id)
    db.refresh(free_slot)
    assert free_slot.state == "open", "cancelling should release the slot"


def test_cannot_book_a_slot_twice(db, patient, free_slot):
    first = booking.book(db, patient.id, free_slot.id)
    try:
        with pytest.raises(BookingError) as caught:
            booking.book(db, patient.id, free_slot.id)
        assert caught.value.code == "SLOT_TAKEN"
    finally:
        booking.cancel(db, first.id)


def test_cannot_book_the_past(db, patient, doctor):
    yesterday = datetime.now() - timedelta(days=1)
    slot = Slot(
        doctor_id=doctor.id,
        start_at=yesterday,
        end_at=yesterday + timedelta(minutes=20),
        state="open",
    )
    db.add(slot)
    db.commit()
    try:
        with pytest.raises(BookingError) as caught:
            booking.book(db, patient.id, slot.id)
        assert caught.value.code == "SLOT_IN_PAST"
    finally:
        db.delete(slot)
        db.commit()


# ----------------------------------------------------------- the headline test


CONCURRENT_ATTEMPTS = 30


def test_concurrent_bookings_produce_exactly_one_appointment(db, doctor, free_slot):
    """Thirty patients grab the same slot at the same instant.

    Exactly one must win. The others must fail cleanly rather than crash or
    silently create a second appointment. This is the guarantee the partial
    unique index exists to provide.
    """
    patient_ids = [
        u.id for u in db.query(User).filter(User.role == "patient").limit(5).all()
    ]
    assert patient_ids, "no patients seeded"

    slot_id = free_slot.id

    def attempt(index: int) -> str:
        session = SessionLocal()
        try:
            booking.book(session, patient_ids[index % len(patient_ids)], slot_id)
            return "booked"
        except BookingError as err:
            return err.code
        except Exception as err:  # anything else is a real failure
            return f"CRASH:{type(err).__name__}"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=CONCURRENT_ATTEMPTS) as pool:
        outcomes = list(pool.map(attempt, range(CONCURRENT_ATTEMPTS)))

    booked = outcomes.count("booked")
    crashes = [o for o in outcomes if o.startswith("CRASH")]

    confirmed = (
        db.query(Appointment)
        .filter(Appointment.slot_id == slot_id, Appointment.status == "confirmed")
        .count()
    )

    # Clean up before asserting, so a failure doesn't poison later runs.
    for appointment in db.query(Appointment).filter(Appointment.slot_id == slot_id).all():
        db.delete(appointment)
    slot = db.get(Slot, slot_id)
    if slot:
        slot.state = "open"
    db.commit()

    assert not crashes, f"unexpected exceptions: {crashes}"
    assert booked == 1, f"{booked} of {CONCURRENT_ATTEMPTS} attempts succeeded, expected 1"
    assert confirmed == 1, f"database holds {confirmed} confirmed appointments, expected 1"
