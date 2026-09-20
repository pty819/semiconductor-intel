"""Alembic environment: async engine, URL wired to intel.settings.Settings.

Migrations run as a migration/admin role in a one-off process (spec 10 §4:
PostgreSQL migration 以一次性管理进程执行，应用角色不能做DDL). The URL comes
from Settings (INTEL_DATABASE_URL or .env) unless alembic.ini/-x overrides.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from intel.settings import Settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Empty/absent URL in alembic.ini → runtime settings. Offline mode (--sql)
# never connects, so this stays side-effect free.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().database_url)

# Importing the model modules registers every table on Base.metadata.
import intel.db.models  # noqa: F401
from intel.db import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
