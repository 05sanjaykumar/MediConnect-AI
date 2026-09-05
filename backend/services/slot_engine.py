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


def generate_for_doctors(db: Session, doctors: list[Doctor], days: list[date]) -> int:
    """Fill in missing slots for several doctors at once.

    Two queries and one insert, regardless of how many doctors or days — so
    the cost doesn't grow with the size of a specialization.
    """
    if not doctors or not days:
        return 0

    doctor_ids = [d.id for d in doctors]
    window_start = datetime.combine(min(days), datetime.min.time())
    window_end = datetime.combine(max(days), datetime.min.time()) + timedelta(days=1)

    # 1 — every working-hour rule for every doctor.
    rules: dict[int, dict[int, list[WorkingHour]]] = {}
    for block in db.scalars(
        select(WorkingHour).where(WorkingHour.doctor_id.in_(doctor_ids))
    ).all():
        rules.setdefault(block.doctor_id, {}).setdefault(block.weekday, []).append(block)

    # 2 — every slot that already exists in the window.
    existing: set[tuple[int, datetime]] = set(
        db.execute(
            select(Slot.doctor_id, Slot.start_at).where(
                Slot.doctor_id.in_(doctor_ids),
                Slot.start_at >= window_start,
                Slot.start_at < window_end,
            )
        ).all()
    )

    new_slots: list[Slot] = []
    for doctor in doctors:
        by_weekday = rules.get(doctor.id)
        if not by_weekday:
            continue
        step = timedelta(minutes=doctor.slot_minutes)
        for day in days:
            for block in by_weekday.get(day.weekday(), []):
                cursor = datetime.combine(day, block.start_time)
                block_end = datetime.combine(day, block.end_time)
                while cursor + step <= block_end:
                    key = (doctor.id, cursor)
                    if key not in existing:
                        new_slots.append(
                            Slot(
                                doctor_id=doctor.id,
                                start_at=cursor,
                                end_at=cursor + step,
                                state="open",
                            )
                        )
                        existing.add(key)
                    cursor += step

    if not new_slots:
        return 0

    db.add_all(new_slots)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return 0
    return len(new_slots)


def generate_for_days(db: Session, doctor: Doctor, days: list[date]) -> int:
    """Create any missing slots for this doctor across several days at once.

    Batched deliberately. Doing this a day at a time costs a query per day, and
    with the database in another region that is half a second each — which is
    what made the fallback ranking take fourteen seconds.
    """
    if not days:
        return 0

    blocks_by_weekday: dict[int, list[WorkingHour]] = {}
    for block in db.scalars(
        select(WorkingHour).where(WorkingHour.doctor_id == doctor.id)
    ).all():
        blocks_by_weekday.setdefault(block.weekday, []).append(block)

    if not blocks_by_weekday:
        return 0

    window_start = datetime.combine(min(days), datetime.min.time())
    window_end = datetime.combine(max(days), datetime.min.time()) + timedelta(days=1)

    existing = set(
        db.scalars(
            select(Slot.start_at).where(
                Slot.doctor_id == doctor.id,
                Slot.start_at >= window_start,
                Slot.start_at < window_end,
            )
        ).all()
    )

    step = timedelta(minutes=doctor.slot_minutes)
    new_slots: list[Slot] = []

    for day in days:
        for block in blocks_by_weekday.get(day.weekday(), []):
            cursor = datetime.combine(day, block.start_time)
            block_end = datetime.combine(day, block.end_time)
            while cursor + step <= block_end:
                if cursor not in existing:
                    new_slots.append(
                        Slot(
                            doctor_id=doctor.id,
                            start_at=cursor,
                            end_at=cursor + step,
                            state="open",
                        )
                    )
                    existing.add(cursor)
                cursor += step

    if not new_slots:
        return 0

    db.add_all(new_slots)
    try:
        db.commit()
    except IntegrityError:
        # Another request generated the same window at the same moment. The
        # unique constraint did its job; their rows are just as good as ours.
        db.rollback()
        return 0
    return len(new_slots)


def generate_for_day(db: Session, doctor: Doctor, day: date) -> int:
    """Create any missing slots for this doctor on this day. Returns how many."""
    return generate_for_days(db, doctor, [day])


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
