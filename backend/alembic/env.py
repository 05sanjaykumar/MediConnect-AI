# backend/alembic/env.py
"""Alembic environment.

Reads the connection string from .env rather than alembic.ini, so there is only
one place secrets live. Migrations use DIRECT_DATABASE_URL (Supabase session
mode, port 5432) because DDL cannot run over the transaction pooler.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Make the backend package importable so `import models` works.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from database import Base  # noqa: E402
import models  # noqa: E402,F401  (imported for its side effect: registers tables)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

MIGRATION_URL = (
    os.getenv("DIRECT_DATABASE_URL", "").strip().strip('"')
    or os.getenv("DATABASE_URL", "").strip().strip('"')
)
if not MIGRATION_URL:
    raise RuntimeError("Set DIRECT_DATABASE_URL (or DATABASE_URL) in backend/.env")

config.set_main_option("sqlalchemy.url", MIGRATION_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — useful for review."""
    context.configure(
        url=MIGRATION_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,        # notice column type changes
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
