from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.models.responses import HealthResponse
from app.utils.common import sanitize_for_json

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz():
    return JSONResponse(
        content=sanitize_for_json(
            HealthResponse(
                status="ok",
                service="agent-api",
                route="health",
            ).model_dump()
        ),
        status_code=status.HTTP_200_OK,
    )


@router.get("/copilot/healthz", response_model=HealthResponse)
async def copilot_healthz():
    return JSONResponse(
        content=sanitize_for_json(
            HealthResponse(
                status="ok",
                service="agent-api",
                route="copilot",
            ).model_dump()
        ),
        status_code=status.HTTP_200_OK,
    )
