from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject DATABASE_URL from the environment.  Alembic's ini file leaves it blank.
database_url = os.getenv("DATABASE_URL", "")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL must be set to run Alembic migrations.\n"
        "Example: DATABASE_URL=postgresql://user:pass@host:5432/dbname alembic upgrade head"
    )

# SQLAlchemy 2+ needs the +psycopg2 driver specifier when using psycopg2.
if database_url.startswith("postgresql://") and "+psycopg2" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

config.set_main_option("sqlalchemy.url", database_url)

# No ORM target metadata — all migrations use raw SQL via op.execute().
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url") or "",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
