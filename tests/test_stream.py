"""SSE streaming: ordering, keepalives, failure shape, and disconnect cleanup.

No database and no LLM. The graph is replaced by a stub that writes through the same
ContextVar writer analysis_agent uses, so the transport is tested on its own.

Each test names the behaviour it protects. The two that matter most are the last two:
a client that goes away must not leave the graph running, and a permission fault must
not be dressed up as a transient stream error.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from app.models.requests.copilot import PermissionContext
from app.routes.v1.copilot import _as_sse
from app.service import copilot_service as svc
from app.service.copilot_service import CustomerIntelligenceCopilot
from app.service.stream_context import get_stream_writer
from app.utils.common import SSE_KEEPALIVE, sse


def _permission() -> PermissionContext:
    return PermissionContext(principal_id="edward", role_level="HOD")


class _StubCopilot(CustomerIntelligenceCopilot):
    """Real stream(), stubbed scope resolution and graph run."""

    def __init__(self, *, run_impl=None, resolve_error: Exception | None = None):
        super().__init__(db=None)  # type: ignore[arg-type]
        self._run_impl = run_impl
        self._resolve_error = resolve_error
        self.resolve_calls = 0

    async def resolve_scope(self, permission):  # type: ignore[override]
        self.resolve_calls += 1
        if self._resolve_error is not None:
            raise self._resolve_error
        return object()

    async def run(self, query, thread_id, user_id, permission, principal=None):  # type: ignore[override]
        assert principal is not None, "stream() must pass the resolved principal"
        return await self._run_impl()


async def _collect(agen) -> list[dict[str, Any]]:
    return [item async for item in agen]


class StreamOrderingTests(unittest.IsolatedAsyncioTestCase):

    async def test_events_arrive_in_order_and_end_with_done(self) -> None:
        async def run_impl():
            writer = get_stream_writer()
            assert writer is not None, "run() must see the stream writer"
            await writer("status", {"message": "streaming answer"})
            await writer("delta", {"text": "Sales were "})
            await writer("delta", {"text": "RM 1,234.56."})
            return {
                "type": "analytics",
                "answer": "Sales were RM 1,234.56.",
                "thread_id": "t1",
                "request_id": "r1",
                "intent_reason": "because",
            }

        items = await _collect(
            _StubCopilot(run_impl=run_impl).stream(
                "q", None, user_id="u", permission=_permission()
            )
        )
        events = [item["event"] for item in items]

        self.assertEqual("status", events[0])
        self.assertEqual(["meta", "final", "done"], events[-3:])
        # Every delta precedes the terminal frames: a client that renders deltas and
        # then replaces them with `final` must never see final first.
        self.assertLess(max(i for i, e in enumerate(events) if e == "delta"),
                        events.index("final"))
        self.assertEqual(
            "Sales were RM 1,234.56.",
            "".join(i["data"]["text"] for i in items if i["event"] == "delta"),
        )

    async def test_an_analytics_answer_is_not_repeated_as_a_final_delta(self) -> None:
        """Its tokens were already streamed; re-emitting would duplicate the answer."""
        async def run_impl():
            await get_stream_writer()("delta", {"text": "streamed"})
            return {"type": "analytics", "answer": "streamed", "thread_id": "t"}

        items = await _collect(
            _StubCopilot(run_impl=run_impl).stream("q", None, user_id="u", permission=_permission())
        )
        self.assertEqual(1, sum(1 for i in items if i["event"] == "delta"))

    async def test_a_non_analytics_answer_is_emitted_as_one_delta(self) -> None:
        """clarify/out_of_scope produce a whole answer at once.

        Emitting it as a delta keeps the client's rendering path identical for every
        response type, instead of making it read `final.answer` only sometimes.
        """
        async def run_impl():
            return {"type": "clarify", "answer": "Which store?", "thread_id": "t"}

        items = await _collect(
            _StubCopilot(run_impl=run_impl).stream("q", None, user_id="u", permission=_permission())
        )
        deltas = [i for i in items if i["event"] == "delta"]
        self.assertEqual([{"text": "Which store?"}], [d["data"] for d in deltas])
        self.assertEqual("done", items[-1]["event"])

    async def test_the_scope_is_resolved_once_not_per_event(self) -> None:
        async def run_impl():
            return {"type": "clarify", "answer": "a", "thread_id": "t"}

        copilot = _StubCopilot(run_impl=run_impl)
        await _collect(copilot.stream("q", None, user_id="u", permission=_permission()))
        self.assertEqual(1, copilot.resolve_calls)


class StreamKeepaliveTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_silent_producer_still_sends_bytes(self) -> None:
        """Planning is silent for 10s+; a proxy drops a silent connection.

        Without this the client cannot distinguish a slow answer from a dead one.
        """
        async def run_impl():
            await asyncio.sleep(0.25)
            return {"type": "clarify", "answer": "a", "thread_id": "t"}

        original = svc._STREAM_HEARTBEAT_SECONDS
        svc._STREAM_HEARTBEAT_SECONDS = 0.05
        try:
            items = await _collect(
                _StubCopilot(run_impl=run_impl).stream(
                    "q", None, user_id="u", permission=_permission()
                )
            )
        finally:
            svc._STREAM_HEARTBEAT_SECONDS = original

        self.assertGreaterEqual(sum(1 for i in items if i["event"] == "keepalive"), 1)
        self.assertEqual("done", items[-1]["event"])


class StreamFailureTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_permission_error_is_raised_before_anything_is_yielded(self) -> None:
        """This is what lets the route answer 403 instead of 200 + an error event.

        The generator must not yield its opening status first: once a byte is out the
        status line is committed, and a provisioning fault would arrive as
        "ended unexpectedly. Please retry." -- advice that cannot work.
        """
        agen = _StubCopilot(
            run_impl=None, resolve_error=PermissionError("Unknown opco_code(s): NOPE")
        ).stream("q", None, user_id="u", permission=_permission())

        with self.assertRaises(PermissionError) as caught:
            await agen.__anext__()
        self.assertIn("NOPE", str(caught.exception))
        await agen.aclose()

    async def test_a_graph_failure_becomes_one_error_event_and_no_done(self) -> None:
        async def run_impl():
            await get_stream_writer()("delta", {"text": "partial"})
            raise RuntimeError("planner exploded")

        items = await _collect(
            _StubCopilot(run_impl=run_impl).stream("q", None, user_id="u", permission=_permission())
        )
        self.assertEqual("delta", items[1]["event"])
        self.assertEqual("error", items[-1]["event"])
        self.assertNotIn("done", [i["event"] for i in items])
        self.assertNotIn("planner exploded", json.dumps(items[-1]["data"]))

    async def test_a_timeout_does_not_hang_the_consumer(self) -> None:
        """The producer closes the queue on every exit path, including failure."""
        async def run_impl():
            raise asyncio.TimeoutError()

        items = await asyncio.wait_for(
            _collect(
                _StubCopilot(run_impl=run_impl).stream(
                    "q", None, user_id="u", permission=_permission()
                )
            ),
            timeout=5,
        )
        self.assertEqual("error", items[-1]["event"])


class StreamDisconnectTests(unittest.IsolatedAsyncioTestCase):

    async def test_closing_the_stream_cancels_the_graph_run(self) -> None:
        """A client hanging up must stop the work, not orphan it.

        Measured before this was handled: 13+ seconds of further LLM calls after the
        client left, and the SQL then tripped the role assertion because the request
        scope had been torn down underneath it. GeneratorExit is a BaseException, so
        the `except Exception` that was there never saw the disconnect -- the cleanup
        has to be in `finally`.
        """
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def run_impl():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"type": "clarify", "answer": "never", "thread_id": "t"}

        agen = _StubCopilot(run_impl=run_impl).stream(
            "q", None, user_id="u", permission=_permission()
        )
        first = await agen.__anext__()
        self.assertEqual("status", first["event"])
        await asyncio.wait_for(started.wait(), timeout=5)

        await agen.aclose()  # what the server does when the client goes away

        self.assertTrue(cancelled.is_set(), "the graph task was left running")

    async def test_closing_after_completion_is_harmless(self) -> None:
        async def run_impl():
            return {"type": "clarify", "answer": "a", "thread_id": "t"}

        agen = _StubCopilot(run_impl=run_impl).stream(
            "q", None, user_id="u", permission=_permission()
        )
        await _collect(agen)
        await agen.aclose()


class SseFramingTests(unittest.TestCase):

    def test_an_event_frame_is_well_formed(self) -> None:
        frame = sse("delta", {"text": "hi"})
        self.assertEqual('event: delta\ndata: {"text": "hi"}\n\n', frame)

    def test_a_newline_in_the_payload_cannot_split_the_frame(self) -> None:
        """A raw newline in `data:` would terminate the event early."""
        frame = sse("delta", {"text": "line one\nline two"})
        self.assertEqual(1, frame.count("\n\n"))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertIn("\\n", frame)

    def test_a_keepalive_is_a_comment_not_an_event(self) -> None:
        """Clients need no case for it, and an EventSource drops it silently."""
        self.assertEqual(SSE_KEEPALIVE, _as_sse({"event": "keepalive", "data": {}}))
        self.assertTrue(SSE_KEEPALIVE.startswith(":"))
        self.assertTrue(SSE_KEEPALIVE.endswith("\n\n"))

    def test_other_events_pass_through_as_frames(self) -> None:
        self.assertEqual(
            sse("done", {"ok": True}), _as_sse({"event": "done", "data": {"ok": True}})
        )


if __name__ == "__main__":
    unittest.main()
