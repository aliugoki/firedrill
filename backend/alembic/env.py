"""Alembic environment.

The database URL is assembled from the environment rather than read from
`alembic.ini`. A DSN in a checked-in config file is a DSN that eventually points
at the wrong database, and "which one did that migration just run against" is a
question nobody wants to ask.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store.schema import metadata  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def database_url() -> str:
    explicit = os.getenv("EVAC_DATABASE_URL")
    if explicit:
        return explicit

    password = os.getenv("EVAC_DB_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "EVAC_DB_PASSWORD is not set. Migrations refuse to run without it "
            "rather than falling back to a default: a password with a default "
            "is a password that ends up in production.")
    host = os.getenv("EVAC_DB_HOST", "localhost")
    port = os.getenv("EVAC_DB_PORT", "5432")
    name = os.getenv("EVAC_DB_NAME", "firedrill")
    user = os.getenv("EVAC_DB_USER", "firedrill")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata,
                      literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
