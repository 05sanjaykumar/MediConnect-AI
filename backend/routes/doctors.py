# backend/routes/doctors.py
"""Find doctors, read their live status, and (for the doctor) change it."""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Doctor, DoctorStatus, StatusEvent, User
from schemas import DoctorOut, DoctorSlots, SlotOut, StatusOut, StatusUpdate
from services.availability import blocked_until
from services.slot_engine import available_slots, next_available

router = APIRouter(prefix="/doctors", tags=["doctors"])


def _to_out(doctor: Doctor) -> DoctorOut:
    status = doctor.status.status if doctor.status else "available"
    return DoctorOut(
        id=doctor.id,
        name=doctor.user.name,
        specialization=doctor.specialization,
        qualification=doctor.qualification,
        consultation_fee=doctor.consultation_fee,
        slot_minutes=doctor.slot_minutes,
        room=doctor.room,
        status=status,
        is_bookable=blocked_until(status) is None,
    )


@router.get("", response_model=list[DoctorOut])
def search_doctors(
    specialization: str | None = Query(default=None),
    name: str | None = Query(default=None),
    available_only: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """Search by specialization or name. This is what the voice agent calls first."""
    query = select(Doctor).where(Doctor.is_active.is_(True))

    if specialization:
        # Loose match so "cardiology", "Cardiology" and "cardio" all work —
        # the voice transcript won't be neatly capitalised.
        query = query.where(Doctor.specialization.ilike(f"%{specialization}%"))
    if name:
        query = query.join(User, User.id == Doctor.user_id).where(User.name.ilike(f"%{name}%"))

    doctors = list(db.scalars(query).all())
    results = [_to_out(d) for d in doctors]

    if available_only:
        results = [d for d in results if d.is_bookable]

    results.sort(key=lambda d: (not d.is_bookable, d.name))
    return results


@router.get("/specializations", response_model=list[str])
def specializations(db: Session = Depends(get_db)):
    rows = db.scalars(select(Doctor.specialization).distinct().order_by(Doctor.specialization))
    return list(rows.all())


@router.get("/{doctor_id}", response_model=DoctorOut)
def get_doctor(doctor_id: int, db: Session = Depends(get_db)):
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")
    return _to_out(doctor)


@router.get("/{doctor_id}/slots", response_model=DoctorSlots)
def doctor_slots(
    doctor_id: int,
    on: date | None = Query(default=None, description="Defaults to today"),
    db: Session = Depends(get_db),
):
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")

    day = on or date.today()
    slots = available_slots(db, doctor, day)

    return DoctorSlots(
        doctor_id=doctor.id,
        doctor_name=doctor.user.name,
        specialization=doctor.specialization,
        status=doctor.status.status if doctor.status else "available",
        date=day.isoformat(),
        slots=[SlotOut.model_validate(s) for s in slots],
    )


@router.get("/{doctor_id}/next-available", response_model=SlotOut | None)
def doctor_next_available(doctor_id: int, db: Session = Depends(get_db)):
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")
    slot = next_available(db, doctor)
    return SlotOut.model_validate(slot) if slot else None


@router.patch("/{doctor_id}/status", response_model=StatusOut)
def update_status(
    doctor_id: int,
    body: StatusUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change a doctor's live status.

    A doctor may change their own; an admin may change anyone's. The slot
    blocking and websocket broadcast get wired in at the next step.
    """
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")

    if user.role == "doctor" and doctor.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only change your own status.")
    if user.role == "patient":
        raise HTTPException(status_code=403, detail="Only doctors and admins can do this.")

    record = doctor.status
    previous = record.status if record else None

    if record is None:
        record = DoctorStatus(doctor_id=doctor.id)
        db.add(record)

    record.status = body.status
    record.note = body.note
    record.updated_at = datetime.now()

    db.add(StatusEvent(doctor_id=doctor.id, from_status=previous, to_status=body.status))
    db.commit()
    db.refresh(record)

    return StatusOut(
        doctor_id=doctor.id,
        doctor_name=doctor.user.name,
        status=record.status,
        note=record.note,
        updated_at=record.updated_at,
    )
