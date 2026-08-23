# backend/database.py
"""Supabase Postgres connection, session factory and declarative base."""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip().strip('"')

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .example.env to .env and fill in the "
        "Supabase connection string."
    )

engine = create_engine(
    DATABASE_URL,
    # Supabase's pooler runs pgbouncer in transaction mode, which cannot carry
    # server-side prepared statements between queries. Turning them off is what
    # stops the "prepared statement already exists" errors.
    connect_args={"prepare_threshold": None},
    pool_pre_ping=True,  # a pooled connection may have been closed under us
    pool_size=5,
    max_overflow=5,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def describe() -> str:
    """What we're connected to, with the password masked."""
    safe = DATABASE_URL
    if "@" in safe:
        head, tail = safe.split("@", 1)
        if ":" in head:
            safe = head.rsplit(":", 1)[0] + ":***@" + tail
    return f"Supabase Postgres -> {safe}"
