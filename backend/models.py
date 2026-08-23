# backend/models.py
"""Database tables for MediConnect AI.

Six tables. The one line that matters most is the partial unique index at the
bottom of Appointment: it makes double-booking impossible at the storage layer,
no matter what the application code does.
"""

from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

# ---------------------------------------------------------------- vocabularies

ROLES = ("patient", "doctor", "admin")

# The five live states a doctor can be in.
STATUSES = ("available", "busy", "in_surgery", "on_break", "off_duty")

# Statuses that stop new bookings from being made against a doctor.
BLOCKING_STATUSES = ("in_surgery", "off_duty", "on_break")

SLOT_STATES = ("open", "booked", "blocked")
APPOINTMENT_STATUSES = ("confirmed", "completed", "cancelled", "no_show")
CHANNELS = ("web", "voice")


# ---------------------------------------------------------------------- tables


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="patient")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    doctor: Mapped["Doctor"] = relationship(back_populates="user", uselist=False)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.name} ({self.role})>"


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    specialization: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    qualification: Mapped[str] = mapped_column(String(120), default="MBBS")
    consultation_fee: Mapped[float] = mapped_column(Float, default=500.0)
    slot_minutes: Mapped[int] = mapped_column(Integer, default=15)
    room: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["User"] = relationship(back_populates="doctor")
    working_hours: Mapped[list["WorkingHour"]] = relationship(
        back_populates="doctor", cascade="all, delete-orphan"
    )
    status: Mapped["DoctorStatus"] = relationship(
        back_populates="doctor", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def name(self) -> str:
        return self.user.name

    def __repr__(self) -> str:
        return f"<Doctor {self.id} {self.specialization}>"


class WorkingHour(Base):
    """The rule slots are generated from. Never edited by hand per-day."""

    __tablename__ = "working_hours"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    doctor: Mapped["Doctor"] = relationship(back_populates="working_hours")

    def __repr__(self) -> str:
        return f"<WorkingHour d{self.doctor_id} wd{self.weekday} {self.start_time}-{self.end_time}>"


class DoctorStatus(Base):
    """One row per doctor, overwritten in place. History lives in status_events."""

    __tablename__ = "doctor_status"

    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="available")
    note: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    doctor: Mapped["Doctor"] = relationship(back_populates="status")

    @property
    def blocks_booking(self) -> bool:
        return self.status in BLOCKING_STATUSES

    def __repr__(self) -> str:
        return f"<DoctorStatus d{self.doctor_id} {self.status}>"


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (
        # Generating slots twice for the same day can never create duplicates.
        UniqueConstraint("doctor_id", "start_at", name="uq_slot_doctor_start"),
        Index("ix_slot_lookup", "doctor_id", "start_at", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    doctor: Mapped["Doctor"] = relationship()

    def __repr__(self) -> str:
        return f"<Slot {self.id} d{self.doctor_id} {self.start_at:%d-%b %H:%M} {self.state}>"


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        # THE guard. A slot can have many cancelled appointments in its history
        # but only ever one confirmed. Enforced by SQLite, not by our code.
        Index(
            "uq_one_confirmed_per_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("status = 'confirmed'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    created_via: Mapped[str] = mapped_column(String(8), nullable=False, default="web")
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)

    slot: Mapped["Slot"] = relationship()
    patient: Mapped["User"] = relationship(foreign_keys=[patient_id])
    doctor: Mapped["Doctor"] = relationship()

    def __repr__(self) -> str:
        return f"<Appointment {self.id} slot{self.slot_id} {self.status}>"


class StatusEvent(Base):
    """Append-only log of every status change.

    This is also the dataset we measure propagation latency from: `at` is t0,
    and the websocket delivery timestamp is t1.
    """

    __tablename__ = "status_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    blocked_slots: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    def __repr__(self) -> str:
        return f"<StatusEvent d{self.doctor_id} {self.from_status}->{self.to_status}>"
