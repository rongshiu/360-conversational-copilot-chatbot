from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core import settings
from app.core.logging import Logger

logger = Logger.get_logger(__name__)

_checkpoint_pool: AsyncConnectionPool | None = None


def _checkpoint_uri() -> str:
    return settings.checkpoint_psycopg_database_url or settings.psycopg_database_url


async def init_checkpoint_schema() -> None:
    """
    Initialize LangGraph checkpoint tables in the checkpoint database.

    These tables are owned by LangGraph, so they are intentionally not managed
    by Alembic. The checkpoint DB itself must already exist.
    """
    if not settings.langgraph_checkpoint_enabled:
        logger.info("LangGraph checkpoint is disabled")
        return

    pool = AsyncConnectionPool(
        conninfo=_checkpoint_uri(),
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )

    await pool.open()
    try:
        async with pool.connection() as conn:
            checkpointer = AsyncPostgresSaver(conn)
            if settings.checkpoint_setup_on_startup:
                await checkpointer.setup()
        logger.info("LangGraph Postgres checkpoint schema is ready")
    finally:
        await pool.close()


async def open_checkpoint_pool() -> None:
    global _checkpoint_pool

    if not settings.langgraph_checkpoint_enabled:
        return

    if _checkpoint_pool is not None:
        return

    _checkpoint_pool = AsyncConnectionPool(
        conninfo=_checkpoint_uri(),
        min_size=1,
        max_size=settings.checkpoint_pool_max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await _checkpoint_pool.open()


async def close_checkpoint_pool() -> None:
    global _checkpoint_pool

    if _checkpoint_pool is not None:
        await _checkpoint_pool.close()
        _checkpoint_pool = None


@asynccontextmanager
async def get_checkpoint_saver() -> AsyncIterator[AsyncPostgresSaver | None]:
    if not settings.langgraph_checkpoint_enabled:
        yield None
        return

    await open_checkpoint_pool()

    if _checkpoint_pool is None:
        yield None
        return

    async with _checkpoint_pool.connection() as conn:
        yield AsyncPostgresSaver(conn)
