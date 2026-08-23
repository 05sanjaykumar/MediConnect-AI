# backend/deps.py
"""Shared FastAPI dependencies: current user and role gates."""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import Doctor, User
from security import decode_token


def get_current_user(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(authorization.removeprefix("Bearer ").strip())
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please sign in again.",
        )

    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="That account no longer exists.")
    return user


def require_role(*allowed: str):
    """Dependency factory: require_role('admin') or require_role('doctor', 'admin')."""

    def check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action is for {' or '.join(allowed)} accounts.",
            )
        return user

    return check


def current_doctor(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Doctor:
    """The Doctor record belonging to the signed-in doctor account."""
    if user.role != "doctor":
        raise HTTPException(status_code=403, detail="This action is for doctor accounts.")
    doctor = db.query(Doctor).filter(Doctor.user_id == user.id).first()
    if doctor is None:
        raise HTTPException(status_code=404, detail="No doctor profile for this account.")
    return doctor
