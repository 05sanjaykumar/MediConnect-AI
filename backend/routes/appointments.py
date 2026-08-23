# backend/routes/appointments.py
"""Book, reschedule, cancel, and list appointments."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Appointment, User
from schemas import Alternate, AppointmentOut, BookRequest, RescheduleRequest
from services import booking
from services.booking import BookingError
from services.fallback import rank_alternates

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _to_out(appointment: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=appointment.id,
        slot_id=appointment.slot_id,
        patient_id=appointment.patient_id,
        patient_name=appointment.patient.name,
        doctor_id=appointment.doctor_id,
        doctor_name=appointment.doctor.user.name,
        specialization=appointment.doctor.specialization,
        start_at=appointment.slot.start_at,
        end_at=appointment.slot.end_at,
        status=appointment.status,
        created_via=appointment.created_via,
        reason=appointment.reason,
        created_at=appointment.created_at,
    )


def _raise_booking_error(err: BookingError, db: Session, slot_id: int, patient_id: int):
    """Turn a BookingError into an HTTP response, with alternates where useful."""
    codes = {
        "NO_SUCH_PATIENT": 404,
        "NO_SUCH_SLOT": 404,
        "NO_SUCH_APPOINTMENT": 404,
        "SLOT_TAKEN": 409,
        "SLOT_INVALIDATED": 409,
        "DOCTOR_UNAVAILABLE": 409,
        "SLOT_IN_PAST": 400,
        "ALREADY_CANCELLED": 409,
        "NOT_CONFIRMED": 409,
        "SAME_SLOT": 400,
    }

    detail: dict = {"code": err.code, "message": err.message}

    # When the reason is "you can't have this one", offer the next best things.
    if err.code in ("SLOT_TAKEN", "SLOT_INVALIDATED", "DOCTOR_UNAVAILABLE"):
        from models import Slot

        slot = db.get(Slot, slot_id)
        if slot is not None:
            detail["alternates"] = [
                Alternate(**a).model_dump(mode="json")
                for a in rank_alternates(db, slot.doctor_id, slot.start_at, patient_id)
            ]

    raise HTTPException(status_code=codes.get(err.code, 400), detail=detail)


@router.post("", response_model=AppointmentOut, status_code=201)
def create_appointment(
    body: BookRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Patients book for themselves; staff may book on a patient's behalf.
    patient_id = body.patient_id if user.role in ("admin", "doctor") else user.id
    patient_id = patient_id or user.id

    try:
        appointment = booking.book(
            db,
            patient_id=patient_id,
            slot_id=body.slot_id,
            created_via=body.created_via,
            reason=body.reason,
        )
    except BookingError as err:
        _raise_booking_error(err, db, body.slot_id, patient_id)

    return _to_out(appointment)


@router.get("/mine", response_model=list[AppointmentOut])
def my_appointments(
    include_past: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role == "doctor":
        from models import Doctor

        doctor = db.query(Doctor).filter(Doctor.user_id == user.id).first()
        if doctor is None:
            return []
        rows = booking.appointments_for_doctor(db, doctor.id, include_past)
    else:
        rows = booking.appointments_for_patient(db, user.id, include_past)

    return [_to_out(a) for a in rows]


@router.get("/{appointment_id}", response_model=AppointmentOut)
def get_appointment(
    appointment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if user.role == "patient" and appointment.patient_id != user.id:
        raise HTTPException(status_code=403, detail="That isn't your appointment.")
    return _to_out(appointment)


@router.patch("/{appointment_id}", response_model=AppointmentOut)
def reschedule_appointment(
    appointment_id: int,
    body: RescheduleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.get(Appointment, appointment_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if user.role == "patient" and existing.patient_id != user.id:
        raise HTTPException(status_code=403, detail="That isn't your appointment.")

    try:
        appointment = booking.reschedule(db, appointment_id, body.new_slot_id)
    except BookingError as err:
        _raise_booking_error(err, db, body.new_slot_id, existing.patient_id)

    return _to_out(appointment)


@router.delete("/{appointment_id}", response_model=AppointmentOut)
def cancel_appointment(
    appointment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.get(Appointment, appointment_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No such appointment.")
    if user.role == "patient" and existing.patient_id != user.id:
        raise HTTPException(status_code=403, detail="That isn't your appointment.")

    try:
        appointment = booking.cancel(db, appointment_id, actor_id=user.id)
    except BookingError as err:
        _raise_booking_error(err, db, existing.slot_id, existing.patient_id)

    return _to_out(appointment)
