# app/service/stream_context.py
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional

StreamWriter = Callable[[str, dict[str, Any]], Awaitable[None]]

_current_stream_writer: ContextVar[Optional[StreamWriter]] = ContextVar(
    "current_copilot_stream_writer",
    default=None,
)


def set_stream_writer(writer: Optional[StreamWriter]):
    return _current_stream_writer.set(writer)


def reset_stream_writer(token) -> None:
    _current_stream_writer.reset(token)


def get_stream_writer() -> Optional[StreamWriter]:
    return _current_stream_writer.get()

async def emit_status(message: str) -> None:
    """Report progress to a streaming client, if this request is streaming.

    A no-op for /ask. Graph nodes call this so the SSE client is not left with a
    silent connection through planning and execution -- that dead air was long
    enough (13s+ on a cold plan) to look like a hang and to trip an intermediary's
    idle timeout.
    """
    writer = get_stream_writer()
    if writer is None:
        return
    await writer("status", {"message": message})
