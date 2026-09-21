# app/db/postgres.py
from __future__ import annotations

import ssl
from contextlib import contextmanager
from typing import Any, AsyncGenerator, Generator

import psycopg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


def _ssl_context_for_sslmode(sslmode: str) -> ssl.SSLContext | bool:
    """
    Convert libpq-style sslmode into asyncpg-compatible SSL config.

    asyncpg does not support `sslmode=require` directly.
    """
    sslmode = sslmode.lower().strip()

    if sslmode == "disable":
        return False

    postgres_ssl_root_cert = getattr(settings, "postgres_ssl_root_cert", None)

    if sslmode == "require":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if sslmode == "verify-ca":
        if postgres_ssl_root_cert:
            context = ssl.create_default_context(cafile=postgres_ssl_root_cert)
        else:
            context = ssl.create_default_context()

        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    if sslmode == "verify-full":
        if postgres_ssl_root_cert:
            return ssl.create_default_context(cafile=postgres_ssl_root_cert)

        return ssl.create_default_context()

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _normalize_asyncpg_database_url(database_url: str) -> tuple[str, dict[str, Any]]:
    connect_args: dict[str, Any] = {
        "command_timeout": settings.sql_timeout_seconds,
        "server_settings": {
            "application_name": "customer_intelligence_copilot",
        },
    }

    url = make_url(database_url)
    query = dict(url.query)

    sslmode = query.pop("sslmode", None)
    if sslmode is not None:
        connect_args["ssl"] = _ssl_context_for_sslmode(str(sslmode))

    cleaned_url = url.set(query=query)

    # Important:
    # Do not use str(cleaned_url), because SQLAlchemy masks passwords as ***.
    normalized_url = cleaned_url.render_as_string(hide_password=False)

    return normalized_url, connect_args


NORMALIZED_DATABASE_URL, CONNECT_ARGS = _normalize_asyncpg_database_url(
    settings.async_database_url
)

engine = create_async_engine(
    NORMALIZED_DATABASE_URL,
    pool_pre_ping=True,
    connect_args=CONNECT_ARGS,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


@contextmanager
def get_pg_conn() -> Generator[psycopg.Connection[Any], None, None]:
    """
    Sync psycopg connection for scripts, COPY, bulk loads, and operational jobs.

    Use this instead of psycopg.connect(...) directly.
    """
    conn = psycopg.connect(settings.psycopg_database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()