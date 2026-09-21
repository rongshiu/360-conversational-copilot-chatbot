# app/service/copilot_usage_service.py
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional


_current_usage: ContextVar[Optional["CopilotUsageTracker"]] = ContextVar(
    "current_copilot_usage",
    default=None,
)


def _now_ms() -> int:
    return int(time.perf_counter() * 1000)


@dataclass
class CopilotUsageTracker:
    """
    Per-request runtime tracker only.

    This no longer persists to an external observability store. It is kept so existing LLM/SQL code can
    attach request_id, latency, and token counts to the in-flight response. A
    future MLflow tracing integration can replace or consume this object.
    """

    request_id: str
    user_id: str
    thread_id: str
    query: str

    started_ms: int = field(default_factory=_now_ms)

    response_type: Optional[str] = None
    status: str = "success"
    error_message: Optional[str] = None

    total_latency_ms: int = 0
    graph_latency_ms: int = 0
    llm_latency_ms: int = 0
    sql_latency_ms: int = 0

    llm_call_count: int = 0
    sql_call_count: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    rows_returned: int = 0
    sql: Optional[str] = None
    intent_reason: Optional[str] = None

    user_message_id: Optional[str] = None
    assistant_message_id: Optional[str] = None

    assistant_response: dict[str, Any] = field(default_factory=dict)
    spans: list[dict[str, Any]] = field(default_factory=list)

    def finish(self) -> None:
        self.total_latency_ms = max(0, _now_ms() - self.started_ms)

    def add_llm_call(
        self,
        *,
        name: str,
        latency_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        model: Optional[str] = None,
    ) -> None:
        self.llm_call_count += 1
        self.llm_latency_ms += latency_ms
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += total_tokens

        self.spans.append(
            {
                "name": name,
                "type": "llm",
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "model": model,
            }
        )

    def add_sql_call(
        self,
        *,
        name: str,
        latency_ms: int,
        rows_returned: int = 0,
        sql: Optional[str] = None,
    ) -> None:
        self.sql_call_count += 1
        self.sql_latency_ms += latency_ms
        self.rows_returned += rows_returned

        if sql:
            self.sql = sql

        self.spans.append(
            {
                "name": name,
                "type": "sql",
                "latency_ms": latency_ms,
                "rows_returned": rows_returned,
                "sql_preview": sql[:1000] if sql else None,
            }
        )

    def add_graph_latency(self, latency_ms: int) -> None:
        self.graph_latency_ms += latency_ms
        self.spans.append(
            {
                "name": "copilot_graph",
                "type": "graph",
                "latency_ms": latency_ms,
            }
        )

    def set_assistant_response(self, result: dict[str, Any]) -> None:
        self.assistant_response = result or {}
        self.response_type = result.get("type")
        self.intent_reason = result.get("intent_reason")
        self.sql = result.get("sql") or self.sql


def start_usage_tracking(
    *,
    user_id: str,
    thread_id: str,
    query: str,
) -> CopilotUsageTracker:
    tracker = CopilotUsageTracker(
        request_id=str(uuid.uuid4()),
        user_id=user_id,
        thread_id=thread_id,
        query=query,
    )
    _current_usage.set(tracker)
    return tracker


def get_usage_tracker() -> Optional[CopilotUsageTracker]:
    return _current_usage.get()


def clear_usage_tracking() -> None:
    _current_usage.set(None)


def extract_gemini_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)

    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    input_tokens = (
        getattr(usage, "prompt_token_count", None)
        or getattr(usage, "input_token_count", None)
        or 0
    )

    output_tokens = (
        getattr(usage, "candidates_token_count", None)
        or getattr(usage, "output_token_count", None)
        or 0
    )

    total_tokens = (
        getattr(usage, "total_token_count", None)
        or input_tokens + output_tokens
    )

    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int(total_tokens or 0),
    }
