# backend/database.py
"""SQLite connection, session factory and declarative base."""

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "mediconnect.db"))
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    # FastAPI serves requests from a threadpool, so a connection may be used
    # from a different thread than the one that created it.
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    """Turn on the two pragmas we actually rely on."""
    cursor = dbapi_connection.cursor()
    # SQLite ignores REFERENCES clauses unless this is on.
    cursor.execute("PRAGMA foreign_keys=ON")
    # Write-ahead logging lets readers continue during a write, which matters
    # for the concurrent-booking test.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


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


def init_db():
    """Create any missing tables. Safe to call on every startup."""
    import models  # noqa: F401  (registers the mappers)

    Base.metadata.create_all(bind=engine)
