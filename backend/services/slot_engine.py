# backend/services/slot_engine.py
"""Turn working hours into bookable slots.

Slots are generated on demand for a given day and stored, rather than being
pre-created for months ahead. Generation is idempotent: asking twice for the
same day never produces duplicates, because we only insert the start times that
aren't already there and the table has UNIQUE(doctor_id, start_at) underneath.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Doctor, Slot, WorkingHour
from services.availability import is_slot_bookable


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def generate_for_day(db: Session, doctor: Doctor, day: date) -> int:
    """Create any missing slots for this doctor on this day. Returns how many."""
    blocks = db.scalars(
        select(WorkingHour).where(
            WorkingHour.doctor_id == doctor.id,
            WorkingHour.weekday == day.weekday(),
        )
    ).all()
    if not blocks:
        return 0

    day_start, day_end = _day_bounds(day)
    existing = set(
        db.scalars(
            select(Slot.start_at).where(
                Slot.doctor_id == doctor.id,
                Slot.start_at >= day_start,
                Slot.start_at < day_end,
            )
        ).all()
    )

    step = timedelta(minutes=doctor.slot_minutes)
    created = 0

    for block in blocks:
        cursor = datetime.combine(day, block.start_time)
        block_end = datetime.combine(day, block.end_time)
        while cursor + step <= block_end:
            if cursor not in existing:
                db.add(
                    Slot(
                        doctor_id=doctor.id,
                        start_at=cursor,
                        end_at=cursor + step,
                        state="open",
                    )
                )
                created += 1
            cursor += step

    if created:
        try:
            db.commit()
        except IntegrityError:
            # Another request generated the same day at the same moment.
            # The unique constraint did its job; their rows are just as good.
            db.rollback()
            return 0
    return created


def available_slots(db: Session, doctor: Doctor, day: date) -> list[Slot]:
    """Slots a patient could actually book on this day, soonest first."""
    generate_for_day(db, doctor, day)

    day_start, day_end = _day_bounds(day)
    slots = db.scalars(
        select(Slot)
        .where(
            Slot.doctor_id == doctor.id,
            Slot.start_at >= day_start,
            Slot.start_at < day_end,
            Slot.state == "open",
        )
        .order_by(Slot.start_at)
    ).all()

    status = doctor.status.status if doctor.status else "available"
    now = datetime.now()
    return [s for s in slots if is_slot_bookable(status, s.start_at, now)]


def next_available(db: Session, doctor: Doctor, days_ahead: int = 7) -> Slot | None:
    """The soonest bookable slot for this doctor, searching forward."""
    today = date.today()
    for offset in range(days_ahead):
        slots = available_slots(db, doctor, today + timedelta(days=offset))
        if slots:
            return slots[0]
    return None
