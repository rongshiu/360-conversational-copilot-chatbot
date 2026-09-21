from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import Logger
from app.db.checkpoint import close_checkpoint_pool, init_checkpoint_schema, open_checkpoint_pool
from app.service.mlflow_observability import configure_mlflow

logger = Logger.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # app.state.postgres_schema_sql used to be rendered from the v2 ORM
        # metadata here. Nothing read it, and the v3 schema is not defined as ORM
        # models, so it is gone. Planner schema context comes from
        # ci_meta.copilot_glossary, scoped per request.
        configure_mlflow()
        await init_checkpoint_schema()
        await open_checkpoint_pool()
        logger.info("Application startup completed")
        yield
    except Exception:
        logger.exception("Application startup failed")
        raise
    finally:
        await close_checkpoint_pool()
        logger.info("Application shutdown completed")
