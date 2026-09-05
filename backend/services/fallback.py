# backend/services/fallback.py
"""Rank replacement appointments when the wanted one isn't available.

Deliberately a plain scoring function, not an LLM call. The voice agent reads
these suggestions out loud, but it never chooses them — so the choice stays
deterministic, unit-testable and explainable to a panel.

Written to be query-frugal. This runs at the exact moment a booking fails,
which on a phone call is the moment the caller is waiting in silence. The
database is in another region, so every round trip costs roughly half a second
and the count of them is what determines whether the agent sounds broken.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload

from models import Appointment, Doctor, Slot
from services.availability import is_slot_bookable
from services.slot_engine import generate_for_doctors

# All the tuning lives here, so it can be changed in one place and tested.
WEIGHTS = {
    "same_specialization": 3.0,
    "same_doctor": 2.0,
    "sooner": 1.5,
    "seen_before": 1.0,
    "lighter_load": 0.5,
    "different_day": -2.0,
}

SEARCH_DAYS = 3          # how far forward to look
SLOTS_PER_DOCTOR = 2     # candidates taken from each doctor


def rank_alternates(
    db: Session,
    wanted_doctor_id: int,
    wanted_start: datetime,
    patient_id: int | None = None,
    limit: int = 3,
) -> list[dict]:
    """Return the best replacement slots, highest score first."""
    wanted_doctor = db.get(Doctor, wanted_doctor_id)
    if wanted_doctor is None:
        return []

    now = datetime.now()
    days = [date.today() + timedelta(days=offset) for offset in range(SEARCH_DAYS)]
    window_start = datetime.combine(days[0], datetime.min.time())
    window_end = datetime.combine(days[-1], datetime.min.time()) + timedelta(days=1)

    # 1 query — peers, with their user and status eagerly loaded so scoring
    # later doesn't trigger a lazy load per doctor.
    peers = list(
        db.scalars(
            select(Doctor)
            .options(joinedload(Doctor.user), joinedload(Doctor.status))
            .where(
                Doctor.specialization == wanted_doctor.specialization,
                Doctor.is_active.is_(True),
            )
        ).all()
    )
    if not peers:
        return []

    peer_ids = [d.id for d in peers]

    # 2 queries total, however many peers there are.
    generate_for_doctors(db, peers, days)

    # 1 query — every candidate slot for every peer across the whole window.
    slots = list(
        db.scalars(
            select(Slot)
            .where(
                Slot.doctor_id.in_(peer_ids),
                Slot.state == "open",
                Slot.start_at > now,
                Slot.start_at < window_end,
            )
            .order_by(Slot.start_at)
        ).all()
    )
    if not slots:
        return []

    # 1 query — how busy each doctor is on each day, for the load component.
    load_rows = db.execute(
        select(
            Slot.doctor_id,
            func.date(Slot.start_at).label("day"),
            func.count(Slot.id).label("total"),
            func.count(case((Slot.state == "booked", 1))).label("booked"),
        )
        .where(
            Slot.doctor_id.in_(peer_ids),
            Slot.start_at >= window_start,
            Slot.start_at < window_end,
        )
        .group_by(Slot.doctor_id, func.date(Slot.start_at))
    ).all()

    load: dict[tuple[int, date], float] = {}
    for doctor_id, day, total, booked in load_rows:
        if isinstance(day, str):  # some drivers hand back a string
            day = date.fromisoformat(day)
        load[(doctor_id, day)] = (booked / total) if total else 0.0

    # 1 query — every doctor this patient has seen before.
    seen_before: set[int] = set()
    if patient_id is not None:
        seen_before = set(
            db.scalars(
                select(Appointment.doctor_id)
                .where(Appointment.patient_id == patient_id)
                .distinct()
            ).all()
        )

    # Everything from here is in memory. No more round trips.
    doctors_by_id = {d.id: d for d in peers}
    taken_per_doctor: dict[int, int] = defaultdict(int)
    candidates: list[dict] = []

    for slot in slots:
        doctor = doctors_by_id.get(slot.doctor_id)
        if doctor is None:
            continue
        if taken_per_doctor[doctor.id] >= SLOTS_PER_DOCTOR:
            continue

        status = doctor.status.status if doctor.status else "available"
        if not is_slot_bookable(status, slot.start_at, now):
            continue

        # For the original doctor, only later slots count as a fallback.
        if doctor.id == wanted_doctor_id and slot.start_at <= wanted_start:
            continue

        candidates.append(
            _score(doctor, slot, wanted_doctor_id, wanted_start, seen_before, load, now)
        )
        taken_per_doctor[doctor.id] += 1

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:limit]


def _score(
    doctor: Doctor,
    slot: Slot,
    wanted_doctor_id: int,
    wanted_start: datetime,
    seen_before: set[int],
    load: dict[tuple[int, date], float],
    now: datetime,
) -> dict:
    reasons: list[str] = []
    score = 0.0

    # Every peer shares the specialization, but state it explicitly so the
    # weight is visible in the score rather than assumed.
    score += WEIGHTS["same_specialization"]

    if doctor.id == wanted_doctor_id:
        score += WEIGHTS["same_doctor"]
        reasons.append("same doctor, later time")
    else:
        reasons.append(f"also {doctor.specialization}")

    hours_away = max((slot.start_at - now).total_seconds() / 3600, 0.0)
    score += WEIGHTS["sooner"] * (1 / (1 + hours_away))

    if doctor.id in seen_before:
        score += WEIGHTS["seen_before"]
        reasons.append("you've seen them before")

    score += WEIGHTS["lighter_load"] * (1 - load.get((doctor.id, slot.start_at.date()), 0.0))

    if slot.start_at.date() != wanted_start.date():
        score += WEIGHTS["different_day"]
        reasons.append("different day")

    return {
        "doctor_id": doctor.id,
        "doctor_name": doctor.user.name,
        "specialization": doctor.specialization,
        "slot_id": slot.id,
        "start_at": slot.start_at,
        "score": round(score, 3),
        "why": ", ".join(reasons),
    }
