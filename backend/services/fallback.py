# backend/services/fallback.py
"""Rank replacement appointments when the wanted one isn't available.

Deliberately a plain scoring function, not an LLM call. The voice agent reads
these suggestions out loud, but it never chooses them — so the choice stays
deterministic, unit-testable and explainable to a panel.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Appointment, Doctor, Slot
from services.slot_engine import available_slots

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


def _load_today(db: Session, doctor_id: int, day: date) -> float:
    """Fraction of today's slots already booked, 0.0 to 1.0."""
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)

    total = db.scalar(
        select(func.count(Slot.id)).where(
            Slot.doctor_id == doctor_id, Slot.start_at >= start, Slot.start_at < end
        )
    ) or 0
    booked = db.scalar(
        select(func.count(Slot.id)).where(
            Slot.doctor_id == doctor_id,
            Slot.start_at >= start,
            Slot.start_at < end,
            Slot.state == "booked",
        )
    ) or 0
    return (booked / total) if total else 0.0


def _has_seen_before(db: Session, patient_id: int | None, doctor_id: int) -> bool:
    if patient_id is None:
        return False
    return db.scalar(
        select(Appointment.id).where(
            Appointment.patient_id == patient_id, Appointment.doctor_id == doctor_id
        ).limit(1)
    ) is not None


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

    peers = list(
        db.scalars(
            select(Doctor).where(
                Doctor.specialization == wanted_doctor.specialization,
                Doctor.is_active.is_(True),
            )
        ).all()
    )

    candidates: list[dict] = []
    today = date.today()

    for doctor in peers:
        found = 0
        for offset in range(SEARCH_DAYS):
            if found >= SLOTS_PER_DOCTOR:
                break
            day = today + timedelta(days=offset)
            for slot in available_slots(db, doctor, day):
                if slot.start_at <= datetime.now():
                    continue
                # For the original doctor, only later slots are a fallback.
                if doctor.id == wanted_doctor_id and slot.start_at <= wanted_start:
                    continue
                candidates.append(_score(db, doctor, slot, wanted_doctor_id, wanted_start, patient_id))
                found += 1
                if found >= SLOTS_PER_DOCTOR:
                    break

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:limit]


def _score(
    db: Session,
    doctor: Doctor,
    slot: Slot,
    wanted_doctor_id: int,
    wanted_start: datetime,
    patient_id: int | None,
) -> dict:
    reasons: list[str] = []
    score = 0.0

    # Every peer shares the specialization, but state it explicitly so the
    # weight is visible in the score rather than assumed.
    score += WEIGHTS["same_specialization"]

    same_doctor = doctor.id == wanted_doctor_id
    if same_doctor:
        score += WEIGHTS["same_doctor"]
        reasons.append("same doctor, later time")
    else:
        reasons.append(f"also {doctor.specialization}")

    hours_away = max((slot.start_at - datetime.now()).total_seconds() / 3600, 0.0)
    score += WEIGHTS["sooner"] * (1 / (1 + hours_away))

    if _has_seen_before(db, patient_id, doctor.id):
        score += WEIGHTS["seen_before"]
        reasons.append("you've seen them before")

    load = _load_today(db, doctor.id, slot.start_at.date())
    score += WEIGHTS["lighter_load"] * (1 - load)

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
