# backend/routes/admin.py
"""Admin dashboard: manage doctors, schedules, and see every appointment.

Everything here requires an admin account.
"""

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_db
from deps import require_role
from models import Appointment, Doctor, DoctorStatus, Slot, User, WorkingHour
from schemas import (
    AdminOverview,
    AppointmentOut,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
    WorkingHourIn,
)
from security import hash_password
from services.availability import blocked_until

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_role("admin"))],
)


def _parse_time(value: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422, detail=f"'{value}' isn't a valid time. Use HH:MM, e.g. 09:30."
        )


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


# --------------------------------------------------------------------- doctors


@router.get("/doctors", response_model=list[DoctorOut])
def list_all_doctors(include_inactive: bool = True, db: Session = Depends(get_db)):
    """Every doctor, including deactivated ones — unlike the public search."""
    query = select(Doctor)
    if not include_inactive:
        query = query.where(Doctor.is_active.is_(True))
    return [_to_out(d) for d in db.scalars(query).all()]


@router.post("/doctors", response_model=DoctorOut, status_code=201)
def create_doctor(body: DoctorCreate, db: Session = Depends(get_db)):
    """Create a doctor's login, profile, schedule and starting status together.

    They're one unit — a doctor without working hours generates no slots and is
    invisible to patients, so it's not a useful half-created state to allow.
    """
    if db.query(User).filter(User.phone == body.phone).first():
        raise HTTPException(status_code=409, detail="That phone number is already registered.")

    user = User(
        name=body.name,
        phone=body.phone,
        email=body.email,
        password_hash=hash_password(body.password),
        role="doctor",
    )
    db.add(user)
    db.flush()

    doctor = Doctor(
        user_id=user.id,
        specialization=body.specialization,
        qualification=body.qualification,
        consultation_fee=body.consultation_fee,
        slot_minutes=body.slot_minutes,
        room=body.room,
    )
    db.add(doctor)
    db.flush()

    for block in body.working_hours:
        db.add(
            WorkingHour(
                doctor_id=doctor.id,
                weekday=block.weekday,
                start_time=_parse_time(block.start_time),
                end_time=_parse_time(block.end_time),
            )
        )

    db.add(DoctorStatus(doctor_id=doctor.id, status="available"))
    db.commit()
    db.refresh(doctor)
    return _to_out(doctor)


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut)
def update_doctor(doctor_id: int, body: DoctorUpdate, db: Session = Depends(get_db)):
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")

    if body.name is not None:
        doctor.user.name = body.name
    for field in ("specialization", "qualification", "consultation_fee", "slot_minutes", "room", "is_active"):
        value = getattr(body, field)
        if value is not None:
            setattr(doctor, field, value)

    db.commit()
    db.refresh(doctor)
    return _to_out(doctor)


@router.delete("/doctors/{doctor_id}", response_model=DoctorOut)
def deactivate_doctor(doctor_id: int, db: Session = Depends(get_db)):
    """Deactivate rather than delete — their past appointments must survive."""
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")

    doctor.is_active = False

    # Take their unbooked future slots off the market. Confirmed appointments
    # are left alone for a human to deal with.
    db.query(Slot).filter(
        Slot.doctor_id == doctor_id,
        Slot.start_at >= datetime.now(),
        Slot.state == "open",
    ).update({"state": "blocked"})

    db.commit()
    db.refresh(doctor)
    return _to_out(doctor)


# --------------------------------------------------------------- working hours


@router.get("/doctors/{doctor_id}/working-hours", response_model=list[WorkingHourIn])
def get_working_hours(doctor_id: int, db: Session = Depends(get_db)):
    blocks = db.scalars(
        select(WorkingHour)
        .where(WorkingHour.doctor_id == doctor_id)
        .order_by(WorkingHour.weekday, WorkingHour.start_time)
    ).all()
    return [
        WorkingHourIn(
            weekday=b.weekday,
            start_time=b.start_time.strftime("%H:%M"),
            end_time=b.end_time.strftime("%H:%M"),
        )
        for b in blocks
    ]


@router.put("/doctors/{doctor_id}/working-hours", response_model=list[WorkingHourIn])
def set_working_hours(
    doctor_id: int, blocks: list[WorkingHourIn], db: Session = Depends(get_db)
):
    """Replace a doctor's whole weekly schedule.

    Already-generated future slots are left in place; changing the rule doesn't
    retroactively cancel appointments people have already booked.
    """
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="No such doctor.")

    for block in blocks:
        if _parse_time(block.end_time) <= _parse_time(block.start_time):
            raise HTTPException(
                status_code=422,
                detail=f"Weekday {block.weekday}: end time must be after start time.",
            )

    db.query(WorkingHour).filter(WorkingHour.doctor_id == doctor_id).delete()
    for block in blocks:
        db.add(
            WorkingHour(
                doctor_id=doctor_id,
                weekday=block.weekday,
                start_time=_parse_time(block.start_time),
                end_time=_parse_time(block.end_time),
            )
        )
    db.commit()
    return get_working_hours(doctor_id, db)


# ---------------------------------------------------------------- appointments


@router.get("/appointments", response_model=list[AppointmentOut])
def all_appointments(
    doctor_id: int | None = None,
    status: str | None = Query(default=None),
    on: date | None = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
):
    from routes.appointments import _to_out as appointment_out

    query = select(Appointment).join(Slot, Slot.id == Appointment.slot_id)
    if doctor_id:
        query = query.where(Appointment.doctor_id == doctor_id)
    if status:
        query = query.where(Appointment.status == status)
    if on:
        day_start = datetime.combine(on, datetime.min.time())
        query = query.where(Slot.start_at >= day_start, Slot.start_at < day_start + timedelta(days=1))

    rows = db.scalars(query.order_by(Slot.start_at.desc()).limit(limit)).all()
    return [appointment_out(a) for a in rows]


# -------------------------------------------------------------------- overview


@router.get("/overview", response_model=AdminOverview)
def overview(db: Session = Depends(get_db)):
    """The numbers the admin dashboard puts across the top."""
    now = datetime.now()
    today_start = datetime.combine(now.date(), datetime.min.time())

    statuses = db.scalars(select(DoctorStatus.status)).all()
    available_now = sum(1 for s in statuses if blocked_until(s) is None)

    def count(model, *where):
        return db.scalar(select(func.count(model.id)).where(*where)) or 0

    return AdminOverview(
        doctors_total=count(Doctor),
        doctors_active=count(Doctor, Doctor.is_active.is_(True)),
        doctors_available_now=available_now,
        patients=count(User, User.role == "patient"),
        appointments_upcoming=db.scalar(
            select(func.count(Appointment.id))
            .join(Slot, Slot.id == Appointment.slot_id)
            .where(Appointment.status == "confirmed", Slot.start_at >= now)
        ) or 0,
        appointments_today=db.scalar(
            select(func.count(Appointment.id))
            .join(Slot, Slot.id == Appointment.slot_id)
            .where(
                Appointment.status == "confirmed",
                Slot.start_at >= today_start,
                Slot.start_at < today_start + timedelta(days=1),
            )
        ) or 0,
        booked_by_voice=count(Appointment, Appointment.created_via == "voice"),
        booked_by_web=count(Appointment, Appointment.created_via == "web"),
    )
