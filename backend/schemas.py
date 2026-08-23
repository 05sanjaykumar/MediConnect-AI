# backend/schemas.py
"""Request and response shapes. Pydantic v2."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------------ auth


class LoginRequest(BaseModel):
    phone: str
    password: str


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(min_length=6)
    email: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    name: str
    role: str


# --------------------------------------------------------------------- doctors


class DoctorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    specialization: str
    qualification: str
    consultation_fee: float
    slot_minutes: int
    room: str | None
    status: str
    is_bookable: bool


class StatusUpdate(BaseModel):
    status: Literal["available", "busy", "in_surgery", "on_break", "off_duty"]
    note: str | None = None


class StatusOut(BaseModel):
    doctor_id: int
    doctor_name: str
    status: str
    note: str | None
    updated_at: datetime
    blocked_slots: int = 0


# ----------------------------------------------------------------------- slots


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    doctor_id: int
    start_at: datetime
    end_at: datetime
    state: str


class DoctorSlots(BaseModel):
    doctor_id: int
    doctor_name: str
    specialization: str
    status: str
    date: str
    slots: list[SlotOut]


# ---------------------------------------------------------------- appointments


class BookRequest(BaseModel):
    slot_id: int
    patient_id: int | None = None  # admins may book on someone's behalf
    reason: str | None = None
    created_via: Literal["web", "voice"] = "web"


class RescheduleRequest(BaseModel):
    new_slot_id: int


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slot_id: int
    patient_id: int
    patient_name: str
    doctor_id: int
    doctor_name: str
    specialization: str
    start_at: datetime
    end_at: datetime
    status: str
    created_via: str
    reason: str | None
    created_at: datetime


class Alternate(BaseModel):
    """A suggested replacement when the wanted slot isn't available."""

    doctor_id: int
    doctor_name: str
    specialization: str
    slot_id: int
    start_at: datetime
    score: float
    why: str
