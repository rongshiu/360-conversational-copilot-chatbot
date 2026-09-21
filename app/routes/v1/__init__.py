from fastapi import APIRouter

from . import copilot

router = APIRouter(prefix="/v1")

router.include_router(copilot.router)

__all__ = ["router"]