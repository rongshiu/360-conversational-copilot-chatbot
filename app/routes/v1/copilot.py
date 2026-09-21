from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import Logger
from app.db.postgres import get_db
from app.models.requests import CopilotAskRequest
from app.models.requests.copilot import PermissionContext, RoleLevel
from app.models.responses.copilot import CopilotResponse
from app.utils.common import SSE_KEEPALIVE, sanitize_for_json, require_user_id, sse
from app.service.copilot_service import CustomerIntelligenceCopilot

logger = Logger.get_logger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])


def _as_sse(item: dict) -> str:
    """Render one service item as an SSE frame.

    Keepalives become comments rather than events: the client needs no case for
    them, and an EventSource drops them silently.
    """
    if item.get("event") == "keepalive":
        return SSE_KEEPALIVE
    return sse(item["event"], item["data"])


# response_model documents the payload; it does not filter it. The handler returns a
# JSONResponse, which FastAPI passes through untouched, so this is OpenAPI only --
# and it is worth having precisely because nothing else forces the two to agree.
@router.post("/ask", response_model=CopilotResponse)
async def ask(
    payload: CopilotAskRequest,
    db: AsyncSession = Depends(get_db),
    x_user_id: str | None = Header(default=None),
):
    user_id = require_user_id(x_user_id)
    try:
        result = await CustomerIntelligenceCopilot(db).run(
            payload.query,
            payload.thread_id,
            user_id=user_id,
            permission=payload.permission,
        )
        return JSONResponse(content=sanitize_for_json(result), status_code=status.HTTP_200_OK)
    except PermissionError as exc:
        # Principal resolution no longer raises this -- the only field left in the
        # permission block is role_level, which pydantic validates at the edge as a
        # 422. The handler stays because a PermissionError from anywhere else in
        # the request is still a provisioning fault in the calling service rather
        # than a user mistake, and 403 is the right shape for it.
        logger.warning("Permission resolution rejected a request: %s", exc)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.post("/ask/stream")
async def ask_stream(
    payload: CopilotAskRequest,
    db: AsyncSession = Depends(get_db),
    x_user_id: str | None = Header(default=None),
):
    user_id = require_user_id(x_user_id)
    service = CustomerIntelligenceCopilot(db)

    stream = service.stream(
        payload.query,
        payload.thread_id,
        user_id=user_id,
        permission=payload.permission,
    )

    # Pull the first item before handing the generator to StreamingResponse.
    #
    # An async generator runs no code until it is iterated, and `stream` resolves the
    # permission block before its first yield. Priming it here is what lets a bad
    # permission block be a 403 with the real reason, exactly as on /ask. Handled
    # inside the response body it could only ever be a 200 carrying an error event,
    # which said "ended unexpectedly. Please retry." for a provisioning fault that no
    # amount of retrying will fix.
    try:
        first = await stream.__anext__()
    except PermissionError as exc:
        await stream.aclose()
        logger.warning("Permission resolution rejected a stream request: %s", exc)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except StopAsyncIteration:
        first = None

    async def event_generator() -> AsyncIterator[str]:
        try:
            if first is not None:
                yield _as_sse(first)
            async for item in stream:
                yield _as_sse(item)
        except Exception:
            # The service already converts its own failures into an error event, so
            # reaching here means the transport itself broke. Say so once and stop.
            logger.exception("Copilot stream endpoint failed")
            yield sse("error", {"message": "The copilot stream ended unexpectedly. Please retry."})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Nginx and friends buffer a proxied response by default, which holds
            # token deltas back until the whole answer is done and defeats the point.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    db: AsyncSession = Depends(get_db),
    x_user_id: str | None = Header(default=None),
    principal_id: str = Query(
        min_length=1,
        max_length=255,
        description="Same principal_id the turns were asked under.",
    ),
    role_level: RoleLevel = Query(
        description=(
            "Same role_level the turns were asked under. Checkpoints are "
            "namespaced by role, so history written as HOD is not readable as "
            "EXEC -- a different role returns an empty list, not a 403."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    user_id = require_user_id(x_user_id)
    try:
        items = await CustomerIntelligenceCopilot(db).get_thread_messages(
            thread_id=thread_id,
            user_id=user_id,
            permission=PermissionContext(
                principal_id=principal_id,
                role_level=role_level,
            ),
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            content=sanitize_for_json({"messages": items}),
            status_code=status.HTTP_200_OK,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
