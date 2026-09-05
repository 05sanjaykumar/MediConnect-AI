# backend/services/booking.py
"""Create, cancel and reschedule appointments.

The most important file in the project. Two things guard every booking:

1. An application check (slot open, in the future, doctor not blocked), which
   produces the friendly message a patient or the voice agent hears.
2. The partial unique index on appointments(slot_id) WHERE status='confirmed',
   which is what actually makes double-booking impossible when two requests
   pass check 1 at the same instant.

Check 1 can lose a race. Check 2 cannot.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from models import Appointment, Doctor, Slot, User
from services.availability import is_slot_bookable


class BookingError(Exception):
    """Something stopped the booking. `code` is stable, `message` is for humans."""

    def __init__(self, code: str, message: str, alternates: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.alternates = alternates or []


def _lock_slot(db: Session, slot_id: int) -> Slot | None:
    """Fetch a slot with SELECT ... FOR UPDATE, so concurrent bookers queue.

    Brings the doctor, their status and their name along in the same query.
    Booking used to cost ten round trips, most of them lazy loads of exactly
    these — and each trip is half a second with the database in another
    region. The lock is on the slot row only; the joined rows are read plain.
    """
    return db.scalar(
        select(Slot)
        .options(
            joinedload(Slot.doctor).joinedload(Doctor.user),
            joinedload(Slot.doctor).joinedload(Doctor.status),
        )
        .where(Slot.id == slot_id)
        .with_for_update(of=Slot)
    )


def book(
    db: Session,
    patient_id: int,
    slot_id: int,
    created_via: str = "web",
    reason: str | None = None,
) -> Appointment:
    """Book a slot for a patient. Raises BookingError if it can't be done."""
    patient = db.get(User, patient_id)
    if patient is None:
        raise BookingError("NO_SUCH_PATIENT", "That patient record doesn't exist.")

    slot = _lock_slot(db, slot_id)
    if slot is None:
        raise BookingError("NO_SUCH_SLOT", "That appointment slot doesn't exist.")

    if slot.state == "booked":
        raise BookingError("SLOT_TAKEN", "That slot has just been taken.")
    if slot.state == "blocked":
        raise BookingError(
            "SLOT_INVALIDATED",
            "That slot is no longer available — the doctor's schedule changed.",
        )

    doctor = slot.doctor
    status = doctor.status.status if doctor and doctor.status else "available"

    now = datetime.now()
    if slot.start_at <= now:
        raise BookingError("SLOT_IN_PAST", "That time has already passed.")

    if not is_slot_bookable(status, slot.start_at, now):
        raise BookingError(
            "DOCTOR_UNAVAILABLE",
            f"{doctor.user.name} is {status.replace('_', ' ')} and isn't taking "
            "appointments at that time.",
        )

    appointment = Appointment(
        slot_id=slot.id,
        patient_id=patient_id,
        doctor_id=slot.doctor_id,
        status="confirmed",
        created_via=created_via,
        reason=reason,
    )
    # Hand it the objects we already hold, so whoever reads
    # appointment.doctor.user.name or appointment.slot.start_at next doesn't
    # go back to the database for them.
    appointment.slot = slot
    appointment.doctor = doctor
    appointment.patient = patient
    slot.state = "booked"
    db.add(appointment)

    try:
        db.commit()
    except IntegrityError:
        # Someone else's transaction committed between our check and our insert.
        # This is the race the unique index exists to lose safely.
        db.rollback()
        raise BookingError("SLOT_TAKEN", "That slot has just been taken.")

    # No refresh: the id came back from INSERT ... RETURNING, created_at is a
    # Python-side default, and the session doesn't expire on commit.
    return appointment


def cancel(db: Session, appointment_id: int, actor_id: int | None = None) -> Appointment:
    """Cancel an appointment and release its slot back to open."""
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise BookingError("NO_SUCH_APPOINTMENT", "That appointment doesn't exist.")
    if appointment.status == "cancelled":
        raise BookingError("ALREADY_CANCELLED", "That appointment is already cancelled.")

    appointment.status = "cancelled"
    appointment.cancelled_at = datetime.now()

    slot = db.get(Slot, appointment.slot_id)
    if slot is not None and slot.state == "booked":
        slot.state = "open"

    db.commit()
    db.refresh(appointment)
    return appointment


def reschedule(db: Session, appointment_id: int, new_slot_id: int) -> Appointment:
    """Move an appointment to a different slot.

    Booked first, cancelled second: if the new slot turns out to be gone, the
    patient still has their original appointment.
    """
    original = db.get(Appointment, appointment_id)
    if original is None:
        raise BookingError("NO_SUCH_APPOINTMENT", "That appointment doesn't exist.")
    if original.status != "confirmed":
        raise BookingError(
            "NOT_CONFIRMED", "Only a confirmed appointment can be rescheduled."
        )
    if new_slot_id == original.slot_id:
        raise BookingError("SAME_SLOT", "That's the same time as the current booking.")

    replacement = book(
        db,
        patient_id=original.patient_id,
        slot_id=new_slot_id,
        created_via=original.created_via,
        reason=original.reason,
    )
    cancel(db, original.id)
    return replacement


def appointments_for_patient(
    db: Session, patient_id: int, include_past: bool = False
) -> list[Appointment]:
    query = (
        select(Appointment)
        .join(Slot, Slot.id == Appointment.slot_id)
        .where(Appointment.patient_id == patient_id)
    )
    if not include_past:
        query = query.where(Appointment.status == "confirmed", Slot.start_at >= datetime.now())
    return list(db.scalars(query.order_by(Slot.start_at)).all())


def appointments_for_doctor(
    db: Session, doctor_id: int, include_past: bool = False
) -> list[Appointment]:
    query = (
        select(Appointment)
        .join(Slot, Slot.id == Appointment.slot_id)
        .where(Appointment.doctor_id == doctor_id)
    )
    if not include_past:
        query = query.where(Appointment.status == "confirmed", Slot.start_at >= datetime.now())
    return list(db.scalars(query.order_by(Slot.start_at)).all())
