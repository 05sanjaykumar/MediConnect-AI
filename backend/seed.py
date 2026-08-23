# backend/seed.py
"""Populate the database with demo data.

    python seed.py           # create tables, fill if empty
    python seed.py --reset   # wipe and rebuild from scratch

Twelve doctors across six specializations, each with working hours, so the slot
engine has something real to generate against.
"""

import sys
from datetime import datetime, time

from database import Base, SessionLocal, engine
from models import Doctor, DoctorStatus, User, WorkingHour
from security import hash_password

# name, specialization, qualification, fee, slot_minutes, room, status
DOCTORS = [
    ("Dr. Anitha Sharma",    "Cardiology",       "MD, DM (Cardiology)",   800, 20, "C-101", "available"),
    ("Dr. Rajesh Menon",     "Cardiology",       "MBBS, MD",              750, 20, "C-102", "available"),
    ("Dr. Priya Nair",       "Dermatology",      "MBBS, MD (Derm)",       600, 15, "D-201", "available"),
    ("Dr. Karthik Iyer",     "Dermatology",      "MBBS, DDVL",            550, 15, "D-202", "on_break"),
    ("Dr. Sneha Reddy",      "Orthopedics",      "MS (Ortho)",            700, 30, "O-301", "available"),
    ("Dr. Vikram Choudhary", "Orthopedics",      "MBBS, MS",              700, 30, "O-302", "in_surgery"),
    ("Dr. Meera Krishnan",   "Pediatrics",       "MD (Pediatrics)",       500, 15, "P-401", "available"),
    ("Dr. Arjun Pillai",     "Pediatrics",       "MBBS, DCH",             500, 15, "P-402", "available"),
    ("Dr. Lakshmi Venkat",   "General Medicine", "MBBS, MD",              400, 15, "G-501", "available"),
    ("Dr. Suresh Babu",      "General Medicine", "MBBS",                  350, 15, "G-502", "busy"),
    ("Dr. Fatima Sheikh",    "ENT",              "MS (ENT)",              600, 20, "E-601", "available"),
    ("Dr. Nikhil Verma",     "ENT",              "MBBS, DLO",             550, 20, "E-602", "off_duty"),
]

PATIENTS = [
    ("Ananya Kumari",  "9840012345"),
    ("Sanjay Kumar",   "9840012346"),
    ("Riya Senju",     "9840012347"),
    ("Ravi Subramani", "9840012348"),
    ("Divya Prasad",   "9840012349"),
]

# Most doctors: morning and evening blocks, Monday to Saturday.
MORNING = (time(9, 0), time(13, 0))
EVENING = (time(14, 0), time(17, 0))
WEEKDAYS = [0, 1, 2, 3, 4, 5]  # Mon-Sat

DEFAULT_PASSWORD = "demo1234"


def reset():
    print("Dropping all tables...")
    Base.metadata.drop_all(bind=engine)


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    if db.query(User).count() > 0:
        print("Database already has data. Use --reset to rebuild.")
        db.close()
        return

    # ------------------------------------------------------------------ admin
    admin = User(
        name="Hospital Admin",
        phone="9840000000",
        email="admin@mediconnect.local",
        password_hash=hash_password(DEFAULT_PASSWORD),
        role="admin",
    )
    db.add(admin)

    # --------------------------------------------------------------- patients
    for name, phone in PATIENTS:
        db.add(
            User(
                name=name,
                phone=phone,
                email=f"{phone}@example.com",
                password_hash=hash_password(DEFAULT_PASSWORD),
                role="patient",
            )
        )

    # ---------------------------------------------------------------- doctors
    for index, (name, spec, qual, fee, minutes, room, status) in enumerate(DOCTORS):
        user = User(
            name=name,
            phone=f"98400{20000 + index}",
            email=f"{name.split()[1].lower()}@mediconnect.local",
            password_hash=hash_password(DEFAULT_PASSWORD),
            role="doctor",
        )
        db.add(user)
        db.flush()  # need user.id

        doctor = Doctor(
            user_id=user.id,
            specialization=spec,
            qualification=qual,
            consultation_fee=fee,
            slot_minutes=minutes,
            room=room,
        )
        db.add(doctor)
        db.flush()  # need doctor.id

        # Every other doctor works evenings too, so availability isn't uniform.
        blocks = [MORNING, EVENING] if index % 2 == 0 else [MORNING]
        for weekday in WEEKDAYS:
            for start, end in blocks:
                db.add(
                    WorkingHour(
                        doctor_id=doctor.id,
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                    )
                )

        db.add(
            DoctorStatus(
                doctor_id=doctor.id,
                status=status,
                updated_at=datetime.now(),
            )
        )

    db.commit()

    doctors = db.query(Doctor).count()
    hours = db.query(WorkingHour).count()
    users = db.query(User).count()
    db.close()

    print(f"Seeded {doctors} doctors, {hours} working-hour blocks, {users} users.")
    print(f"Every account uses the password: {DEFAULT_PASSWORD}")
    print("Admin login: 9840000000")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    seed()
