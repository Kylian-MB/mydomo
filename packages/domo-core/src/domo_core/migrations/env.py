"""Alembic's environment for the core: asynchronous (asyncpg), over the core's own metadata.

The database URL comes from the caller when there is one (``domo_core.migrate`` puts it in
``config.attributes``), and from ``CoreSettings`` otherwise: that is the case of the ``alembic``
command line, which then reads ``DOMO_DATABASE_URL`` or the password file like the core does.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from domo_core.config import CoreSettings
from domo_core.db import Base, load_models
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None:
    # Only the command line has an ini file. Keep the loggers of the application alive.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

load_models()
target_metadata = Base.metadata


def _database_url() -> str:
    url: object = config.attributes.get("database_url")
    if isinstance(url, str):
        return url
    return CoreSettings().database_url


def run_migrations_offline() -> None:
    """Emit the SQL (``alembic upgrade head --sql``) without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # A single short-lived connection: no pool to keep around once the migration is done.
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
