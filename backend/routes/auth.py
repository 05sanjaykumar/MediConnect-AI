# backend/routes/auth.py
"""Sign in, register, and look up who you are."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Doctor, User
from schemas import LoginRequest, RegisterRequest, TokenResponse
from security import create_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.phone == body.phone).first()
    if user is None or not verify_password(body.password, user.password_hash):
        # Deliberately vague: don't reveal which half was wrong.
        raise HTTPException(status_code=401, detail="Wrong phone number or password.")

    return TokenResponse(
        access_token=create_token(user.id, user.role, user.name),
        user_id=user.id,
        name=user.name,
        role=user.role,
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.phone == body.phone).first():
        raise HTTPException(status_code=409, detail="That phone number is already registered.")

    user = User(
        name=body.name,
        phone=body.phone,
        email=body.email,
        password_hash=hash_password(body.password),
        role="patient",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_token(user.id, user.role, user.name),
        user_id=user.id,
        name=user.name,
        role=user.role,
    )


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = {"id": user.id, "name": user.name, "phone": user.phone, "role": user.role}
    if user.role == "doctor":
        doctor = db.query(Doctor).filter(Doctor.user_id == user.id).first()
        if doctor:
            profile["doctor_id"] = doctor.id
            profile["specialization"] = doctor.specialization
    return profile
