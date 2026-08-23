# backend/services/status.py
"""Doctor status changes, and what they do to the schedule.

A status change is not just a flag. Going into surgery has to take the affected
slots off the market, and coming back has to put them back — otherwise the
patient-facing availability and the doctor's reality drift apart.

Confirmed appointments are deliberately never cancelled here. They surface on
the doctor's dashboard as needing attention, for a human to decide. Silently
cancelling someone's appointment because a status flipped would be the wrong
behaviour in a hospital system.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Appointment, Doctor, DoctorStatus, Slot, StatusEvent
from services.availability import blocked_until


@dataclass
class StatusChange:
    """What actually happened, so the API and the dashboard can report it."""

    doctor_id: int
    doctor_name: str
    from_status: str | None
    to_status: str
    note: str | None
    updated_at: datetime
    slots_blocked: int
    slots_released: int
    appointments_needing_attention: list[int]

    @property
    def changed(self) -> bool:
        return self.from_status != self.to_status


def change_status(
    db: Session,
    doctor: Doctor,
    new_status: str,
    note: str | None = None,
) -> StatusChange:
    """Apply a status change and reconcile the doctor's future slots."""
    now = datetime.now()

    record = doctor.status
    previous = record.status if record else None

    if record is None:
        record = DoctorStatus(doctor_id=doctor.id)
        db.add(record)

    record.status = new_status
    record.note = note
    record.updated_at = now

    blocked = 0
    released = 0
    until = blocked_until(new_status, now)

    if until is not None:
        # Take open slots inside the blocked window off the market.
        blocked = (
            db.query(Slot)
            .filter(
                Slot.doctor_id == doctor.id,
                Slot.state == "open",
                Slot.start_at > now,
                Slot.start_at <= until,
            )
            .update({"state": "blocked"}, synchronize_session=False)
        )
    elif doctor.is_active:
        # Back to available — restore everything we'd blocked that hasn't since
        # been booked. Deactivated doctors stay blocked regardless.
        released = (
            db.query(Slot)
            .filter(
                Slot.doctor_id == doctor.id,
                Slot.state == "blocked",
                Slot.start_at > now,
            )
            .update({"state": "open"}, synchronize_session=False)
        )

    # Appointments already confirmed inside a now-blocked window. We don't touch
    # them; we surface them.
    needs_attention: list[int] = []
    if until is not None:
        needs_attention = list(
            db.scalars(
                select(Appointment.id)
                .join(Slot, Slot.id == Appointment.slot_id)
                .where(
                    Appointment.doctor_id == doctor.id,
                    Appointment.status == "confirmed",
                    Slot.start_at > now,
                    Slot.start_at <= until,
                )
            ).all()
        )

    db.add(
        StatusEvent(
            doctor_id=doctor.id,
            from_status=previous,
            to_status=new_status,
            blocked_slots=blocked,
            at=now,
        )
    )
    db.commit()
    db.refresh(record)

    return StatusChange(
        doctor_id=doctor.id,
        doctor_name=doctor.user.name,
        from_status=previous,
        to_status=new_status,
        note=note,
        updated_at=record.updated_at,
        slots_blocked=blocked,
        slots_released=released,
        appointments_needing_attention=needs_attention,
    )


def recent_events(db: Session, limit: int = 20) -> list[StatusEvent]:
    """Latest status changes across all doctors — for the admin dashboard."""
    return list(
        db.scalars(select(StatusEvent).order_by(StatusEvent.at.desc()).limit(limit)).all()
    )
