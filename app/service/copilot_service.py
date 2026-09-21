# app/service/copilot_service.py
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import Any, AsyncIterator, Dict, Optional
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.copilot_graph import build_copilot_graph
from app.core import settings
from app.db.checkpoint import get_checkpoint_saver
from app.db.session_scope import copilot_scope
from app.models.requests.copilot import PermissionContext
from app.service.access_log_service import write_access_log
from app.service.principal_service import PrincipalContext, resolve_principal
from app.service.entity_resolution.models import redact_internal_store_ids
from app.service.copilot_usage_service import (
    _now_ms,
    clear_usage_tracking,
    start_usage_tracking,
)
from app.service.stream_context import reset_stream_writer, set_stream_writer
from app.service.mlflow_observability import mlflow_span, safe_dict, update_current_trace
from app.utils.common import sanitize_for_json
from app.core.logging import Logger

logger = Logger.get_logger(__name__)


# Sentinel that closes the stream queue. Identity-compared, so it can never collide
# with a real event.
_STREAM_END: dict = {"__stream_end__": True}

# Emitted when nothing has been produced for this long. The route turns it into an
# SSE comment, which every conforming client ignores.
_STREAM_KEEPALIVE: dict = {"event": "keepalive", "data": {}}
_STREAM_HEARTBEAT_SECONDS = 10.0

# Deep enough that token deltas never block on a healthy client, shallow enough that
# a stalled one cannot grow the queue without bound.
_STREAM_QUEUE_MAX = 512


def _checkpoint_thread_id(user_id: str, thread_id: str, principal=None) -> str:
    """Namespace checkpoint storage by user AND authorization scope.

    The scope is part of the key, not just the user, because conversation history
    contains ANSWERS -- rows and SQL from previous turns. `answer_from_previous`
    replays those through the LLM without running any SQL, so row-level security
    never gets a chance to apply.

    Namespacing by user alone was a cross-tenant leak: the same user id with a
    different permission block, on the same thread_id, replayed the earlier
    scope's data verbatim. An NorthCo Credit caller received NorthCo Mart's customer
    count because an NorthCo Mart caller had asked the same question in that thread.

    Including the scope means a changed grant is simply a different conversation.
    A user legitimately re-scoped (say EXEC -> HOD) loses their thread history,
    which is the correct trade: that history was produced under a scope they no
    longer have.
    """
    safe_user = (user_id or "anonymous").strip()
    safe_thread = (thread_id or "").strip()

    if principal is None:
        return f"{safe_user}:{safe_thread}"

    # Hashing is now belt and braces -- scope_signature() is just the role level,
    # so this is one of two short strings. It stayed because a raw signature used
    # to carry hundreds of category keys, and because changing the key format
    # would orphan every existing thread.
    scope = hashlib.sha256(principal.scope_signature().encode("utf-8")).hexdigest()[:16]
    return f"{safe_user}:{scope}:{safe_thread}"


# def _derive_title_from_query(query: str, max_len: int = 80) -> str:
#     text = " ".join((query or "").strip().split())
#     if not text:
#         return "New chat"
#     if len(text) <= max_len:
#         return text
#     return text[: max_len - 3].rstrip() + "..."


class CustomerIntelligenceCopilot:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def resolve_scope(self, permission: PermissionContext) -> PrincipalContext:
        """Build the caller's context from the permission block.

        No longer touches the database, and no longer opens system_scope() to do
        it. That round-trip existed because expanding a category grant means
        reading the whole product hierarchy, which cannot run from inside the scope
        being resolved -- and there is no scope to resolve now, only a role level
        that PermissionContext has already validated.

        Kept async and kept separate from run() so the streaming path still has a
        place to fail before it starts writing a response body: once the first SSE
        byte is out the status line is already 200, and an error event the client
        has to special-case is a worse outcome than a 403.
        """
        return resolve_principal(permission)

    async def run(
        self,
        query: str,
        thread_id: Optional[str],
        user_id: str,
        permission: PermissionContext,
        principal: Optional[PrincipalContext] = None,
    ) -> Dict[str, Any]:
        effective_thread_id = (thread_id or str(uuid4())).strip()

        # A PermissionError here propagates to the route as a 403; it is a
        # provisioning fault, not a question the copilot should try to answer. The
        # streaming path passes an already-resolved principal so that error can be a
        # real status code rather than an event inside a 200 body.
        if principal is None:
            principal = await self.resolve_scope(permission)

        tracker = start_usage_tracking(
            user_id=user_id,
            thread_id=effective_thread_id,
            query=query,
        )

        try:
            with mlflow_span(
                "copilot.request",
                inputs={
                    "query": query,
                    "thread_id": effective_thread_id,
                },
                attributes={
                    "request_id": tracker.request_id,
                    "user_id_present": bool(user_id),
                    "api_timeout_seconds": settings.api_timeout_seconds,
                },
            ) as root_span:
                update_current_trace(
                    request_id=tracker.request_id,
                    user_id=user_id,
                    thread_id=effective_thread_id,
                    query=query,
                    route="POST /v1/copilot/ask",
                    extra_metadata={
                        "llm_provider": settings.llm_provider,
                        "checkpoint_enabled": settings.langgraph_checkpoint_enabled,
                    },
                )

                async with get_checkpoint_saver() as checkpointer, copilot_scope(
                    self.db, principal
                ):
                    graph = build_copilot_graph(
                        self.db,
                        user_id=user_id,
                        principal=principal,
                        checkpointer=checkpointer,
                    )

                    graph_started_ms = _now_ms()
                    with mlflow_span(
                        "langgraph.ainvoke",
                        inputs={
                            "thread_id": effective_thread_id,
                            "query": query,
                        },
                        attributes={
                            "checkpoint_thread_id": _checkpoint_thread_id(
                                user_id, effective_thread_id, principal
                            ),
                        },
                    ) as graph_span:
                        final_state = await asyncio.wait_for(
                            graph.ainvoke(
                                {
                                    "query": query,
                                    "thread_id": effective_thread_id,
                                    "request_id": tracker.request_id,
                                    "principal": principal,
                                },
                                config={
                                    "configurable": {
                                        "thread_id": _checkpoint_thread_id(
                                            user_id, effective_thread_id, principal
                                        ),
                                        "user_id": user_id,
                                    }
                                },
                            ),
                            timeout=settings.api_timeout_seconds,
                        )
                        if graph_span is not None:
                            graph_span.set_outputs(
                                safe_dict(
                                    {
                                        "result_type": (final_state.get("result") or {}).get("type"),
                                        "intent_reason": (final_state.get("result") or {}).get("intent_reason"),
                                    },
                                    limit=1000,
                                )
                            )

                tracker.add_graph_latency(max(0, _now_ms() - graph_started_ms))

                result = dict(final_state["result"])
                result["thread_id"] = effective_thread_id
                result["request_id"] = tracker.request_id
                result = redact_internal_store_ids(result, final_state.get("lookup_plan"))
                result = sanitize_for_json(result)

                tracker.set_assistant_response(result)
                tracker.finish()

                await write_access_log(
                    self.db,
                    principal=principal,
                    mlflow_trace_id=getattr(root_span, "trace_id", None),
                    denied_reason=result.get("denied_reason"),
                )

                if root_span is not None:
                    root_span.set_outputs(
                        safe_dict(
                            {
                                "type": result.get("type"),
                                "answer_preview": result.get("answer"),
                                "row_count": len(result.get("rows") or []),
                                "has_sql": bool(result.get("sql")),
                                "status": tracker.status,
                                "total_latency_ms": tracker.total_latency_ms,
                                "llm_call_count": tracker.llm_call_count,
                                "sql_call_count": tracker.sql_call_count,
                                "input_tokens": tracker.input_tokens,
                                "output_tokens": tracker.output_tokens,
                                "total_tokens": tracker.total_tokens,
                            },
                            limit=1000,
                        )
                    )

                return result

        except Exception as exc:
            tracker.status = "error"

            # Log the full exception internally for diagnosis, but never surface raw
            # exception text (SQL, stack details, connection strings) to the client.
            logger.exception(
                "Copilot request failed (request_id=%s, thread_id=%s)",
                tracker.request_id,
                effective_thread_id,
            )

            if isinstance(exc, asyncio.TimeoutError):
                error_message = (
                    f"Copilot request timed out after {settings.api_timeout_seconds} seconds. "
                    "The question may have produced an expensive SQL query or a slow model call. "
                    "Please retry with a narrower period or filter."
                )
            else:
                error_message = (
                    "The copilot hit an unexpected error while answering. "
                    f"Please retry, and quote request id {tracker.request_id} if it persists."
                )

            tracker.error_message = str(exc)

            error_result = {
                "type": "error",
                "answer": error_message,
                "chart": None,
                "rows": [],
                "sql": tracker.sql,
                "glossary_matches": [],
                "intent_reason": tracker.intent_reason,
                "thread_id": effective_thread_id,
                "request_id": tracker.request_id,
            }
            tracker.set_assistant_response(error_result)

            return sanitize_for_json(error_result)

        finally:
            clear_usage_tracking()

    async def stream(
        self,
        query: str,
        thread_id: Optional[str],
        user_id: str,
        permission: PermissionContext,
    ) -> AsyncIterator[dict]:
        """Stream one turn as a sequence of {event, data} items.

        The graph is not itself an async generator -- it returns a single result --
        so the LLM token deltas come out through a queue: analysis_agent writes to
        the ContextVar-scoped writer, and this loop forwards whatever appears to the
        route the moment it appears.

        Three things this has to get right, each of which was wrong before:

        1. The scope is resolved BEFORE the first yield, so a bad permission block is
           still a 403 (see resolve_scope). Nothing is yielded until it succeeds, and
           the route primes the generator to catch it.

        2. Termination is a sentinel, not polling. The old loop woke every 250ms to
           re-check task.done(), which is both a busy-wait and a race: an item put
           between the last check and the drain depended on the drain to catch it.
           The producer now closes the queue itself, so the consumer blocks until
           there is something to send.

        3. The producer is cancelled when this generator is closed -- which is what
           happens when the client disconnects. Without that, GeneratorExit (a
           BaseException, so `except Exception` never saw it) left the graph running:
           measured 13+ seconds of further LLM calls, and its SQL then failed the
           role assertion because the scope had been torn down with the request. The
           guard caught it, but the run still burned every retry for a client that
           had gone.
        """
        # Before the first yield, so this can still be a 403.
        principal = await self.resolve_scope(permission)

        # Bounded: a slow client must not let the queue grow without limit. A full
        # queue backpressures the token loop instead, which is the correct place to
        # slow down.
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX)

        async def writer(event: str, data: dict[str, Any]) -> None:
            await queue.put({"event": event, "data": sanitize_for_json(data)})

        async def produce() -> dict[str, Any]:
            token = set_stream_writer(writer)
            try:
                return await self.run(
                    query,
                    thread_id,
                    user_id=user_id,
                    permission=permission,
                    principal=principal,
                )
            finally:
                reset_stream_writer(token)
                # Unblocks the consumer on every exit path: success, failure and
                # cancellation. Shielded because a cancelled task still has to
                # deliver this one item or the consumer waits for a heartbeat that
                # will never be followed by anything.
                with contextlib.suppress(Exception):
                    queue.put_nowait(_STREAM_END)

        task = asyncio.create_task(produce())

        try:
            yield {"event": "status", "data": {"message": "understanding query"}}

            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_STREAM_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Planning and execution can be silent for 10s+. A proxy with an
                    # idle timeout drops a silent connection, and a client cannot
                    # tell a slow answer from a dead one.
                    yield _STREAM_KEEPALIVE
                    continue

                if item is _STREAM_END:
                    break

                yield item

            # Re-raises whatever the graph raised, including a timeout.
            result = await task

            yield {
                "event": "meta",
                "data": {
                    "thread_id": result.get("thread_id"),
                    "request_id": result.get("request_id"),
                    "type": result.get("type"),
                    "intent_reason": result.get("intent_reason"),
                },
            }

            # Analytics answers already emitted real LLM token deltas from
            # analysis_agent. Non-analytics routes produce a whole answer at once,
            # so emit those as a single delta to keep the client's rendering path
            # identical for every response type.
            if result.get("type") != "analytics":
                answer = result.get("answer") or ""
                if answer:
                    yield {"event": "delta", "data": {"text": answer}}

            yield {"event": "final", "data": result}
            yield {"event": "done", "data": {"ok": True}}

        except Exception:
            logger.exception("Copilot stream failed")
            yield {
                "event": "error",
                "data": {
                    "message": "The copilot stream ended unexpectedly. Please retry."
                },
            }

        finally:
            # Also runs on GeneratorExit, which is how a client disconnect arrives.
            # Nothing may be yielded from here -- the response is already gone.
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def get_thread_messages(
        self,
        thread_id: str,
        user_id: str,
        permission: PermissionContext,
        limit: int = 100,
        offset: int = 0,
    ):
        """Return checkpoint-stored history for a known thread_id.

        Pure checkpoint mode does not maintain chat_sessions/chat_messages, so
        this endpoint can retrieve a known thread only. It cannot list, rename,
        or delete threads without a separate product metadata table.

        The permission block is required for the same reason /ask needs one, and
        it is not about running SQL: checkpoint keys are namespaced by
        authorization scope (see _checkpoint_thread_id), and stored turns carry
        the rows and SQL of previous answers. Reading history under the wrong
        role would replay an HOD answer -- revenue included -- into an EXEC
        session, with no query for the persona views to filter. Callers get the
        history their own role produced, or nothing.
        """
        effective_thread_id = (thread_id or "").strip()
        if not effective_thread_id:
            return []

        principal = await self.resolve_scope(permission)

        async with get_checkpoint_saver() as checkpointer:
            # No serving SQL runs here, so the graph is built without a scope --
            # but the principal still selects WHICH checkpoint namespace is read.
            graph = build_copilot_graph(
                self.db,
                user_id=user_id,
                principal=None,
                checkpointer=checkpointer,
            )
            snapshot = await graph.aget_state(
                config={
                    "configurable": {
                        "thread_id": _checkpoint_thread_id(
                            user_id, effective_thread_id, principal
                        ),
                        "user_id": user_id,
                    }
                }
            )

        values = getattr(snapshot, "values", None) or {}
        history = list(values.get("history") or [])
        sliced = history[offset : offset + limit]

        items = []
        for idx, item in enumerate(sliced, start=offset):
            row = dict(item or {})
            row.setdefault("id", f"{effective_thread_id}:{idx}")
            items.append(row)
        return items
