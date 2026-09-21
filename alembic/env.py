import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core import settings
from app.db.postgres import Base
from app.models.schemas import *  # noqa: F401,F403

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic is running through async SQLAlchemy here, so force asyncpg URL.
# POSTGRES_URI remains the canonical env var; DATABASE_URL is only backward compatible.
config.set_main_option("sqlalchemy.url", settings.async_database_url)

target_metadata = Base.metadata


def get_postgres_schema() -> str:
    return settings.postgres_schema or "public"


def run_migrations_offline() -> None:
    """Run migrations in offline mode."""
    schema = get_postgres_schema()
    url = config.get_main_option("sqlalchemy.url")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=schema,
    )

    with context.begin_transaction():
        context.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        context.execute(f'SET search_path TO "{schema}"')
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    schema = get_postgres_schema()

    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    connection.execute(text(f'SET search_path TO "{schema}"'))
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema=schema,
        compare_type=True,
        compare_server_default=True,
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