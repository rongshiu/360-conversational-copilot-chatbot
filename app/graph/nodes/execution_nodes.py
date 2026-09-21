# app/graph/nodes/execution_nodes.py
from __future__ import annotations

from app.agents.analysis_agent import analyze_sql_result
from app.agents.answer_agent import build_analytics_response, build_error_response
from app.service.sql_executor import classify_sql_error, run_sql
from app.service.query_shape_guard import validate_query_shape
from app.agents.sql_validator_agent import validate_sql
from app.graph.copilot_state import CopilotState
from app.service.access_policy import to_payload
from app.service.entity_resolution.models import (
    public_lookup_context,
    public_lookup_plan,
    public_lookup_slots,
)
from app.utils.common import sanitize_for_json
from app.service.stream_context import emit_status
from app.core.logging import Logger

logger = Logger.get_logger(__name__)


class ExecutionNodesMixin:

    async def validate_sql_node(self, state: CopilotState) -> CopilotState:
        await emit_status("checking the query")
        validation = validate_sql(state.get("sql"))
        if not validation.is_valid:
            return {
                "validation_error": validation.feedback,
                "sql": validation.normalized_sql or state.get("sql"),
            }

        shape_validation = validate_query_shape(
            query=state["query"],
            sql=validation.normalized_sql,
        )
        if not shape_validation.is_valid:
            # Everything the guard still catches is a fixable SQL shape, so it comes
            # back as retry feedback. The one refusal it used to raise -- an identity
            # question -- is classified by the planner now; see validate_query_shape.
            return {
                "validation_error": shape_validation.feedback,
                "sql": validation.normalized_sql,
            }

        return {"sql": validation.normalized_sql, "validation_error": None}

    def route_after_validate(self, state: CopilotState) -> str:
        if state.get("out_of_scope_reason"):
            return "out_of_scope"
        if not state.get("validation_error"):
            return "execute_sql"
        if state.get("retry_count", 0) < self.settings.max_sql_retries:
            return "retry_sql"
        return "execute_error"

    async def execute_sql_node(self, state: CopilotState) -> CopilotState:
        await emit_status("running the query")
        try:
            rows_payload = await run_sql(state["sql"], db=self.db)
            logger.info(
                "rows_payload columns=%s row_count=%s error=%s raw_text_preview=%s full_payload=%s",
                rows_payload.get("columns"),
                len(rows_payload.get("rows") or []),
                rows_payload.get("error"),
                (rows_payload.get("raw_text") or "")[:1000],
                rows_payload,
            )
            if rows_payload.get("error"):
                return {
                    "execution_error": classify_sql_error(rows_payload.get("error", "")),
                    "rows_payload": rows_payload,
                }
            return {"rows_payload": rows_payload, "execution_error": None}
        except Exception as exc:
            return {"execution_error": classify_sql_error(str(exc))}

    def route_after_execute(self, state: CopilotState) -> str:
        if not state.get("execution_error"):
            return "analyze_result"
        if state.get("retry_count", 0) < self.settings.max_sql_retries:
            return "retry_sql"
        return "execute_error"

    async def retry_sql(self, state: CopilotState) -> CopilotState:
        return {"retry_count": state.get("retry_count", 0) + 1}

    async def execute_error(self, state: CopilotState) -> CopilotState:
        reason = " | ".join(
            part
            for part in [
                state.get("intent_reason"),
                state.get("plan_reason"),
                state.get("validation_error"),
                state.get("execution_error"),
            ]
            if part
        )

        if state.get("execution_error"):
            answer = (
                "I tried a couple of times but couldn't get an answer to that one. "
                "Please try again, or rephrase the question — sometimes narrowing the period or filters helps."
            )
        else:
            answer = (
                "I couldn't turn that question into a query I'm confident running, even after retrying. "
                "Could you rephrase it, or be a bit more specific about the metric, period, or filters you want?"
            )

        result = build_error_response(
            answer=answer,
            sql=state.get("sql"),
            reason=reason,
        )

        result["lookup_matches"] = sanitize_for_json(
            public_lookup_slots(state.get("lookup_matches") or [])
        )
        result["lookup_context"] = public_lookup_context(
            state.get("lookup_plan"),
            state.get("lookup_context") or "",
        )

        if state.get("lookup_plan"):
            result["lookup_plan"] = sanitize_for_json(
                public_lookup_plan(state.get("lookup_plan"))
            )

        result["policies"] = to_payload(state.get("applied_policies") or [])

        return {"result": result}

    async def analyze_result_node(self, state: CopilotState) -> CopilotState:
        await emit_status("interpreting the result")
        reason = " | ".join(
            part for part in [state.get("intent_reason"), state.get("plan_reason")] if part
        )
        display_lookup_context = public_lookup_context(
            state.get("lookup_plan"),
            state.get("lookup_context") or "",
        )

        analysis_result = await analyze_sql_result(
            query=state["query"],
            sql=state["sql"],
            rows_payload=state.get("rows_payload", {}),
            intent_reason=state.get("intent_reason"),
            plan_reason=state.get("plan_reason"),
            lookup_context=display_lookup_context,
        )

        return {
            "analysis_result": analysis_result,
            "plan_reason": reason or state.get("plan_reason"),
        }

    async def synthesize(self, state: CopilotState) -> CopilotState:
        reason = " | ".join(
            part for part in [state.get("intent_reason"), state.get("plan_reason")] if part
        )

        result = await build_analytics_response(
            state["query"],
            state["sql"],
            state.get("rows_payload", {}),
            reason=reason,
            analysis_result=state.get("analysis_result", {}),
        )

        # Expose this so you can verify whether resolver actually ran.
        # This is useful during UAT/debugging and prevents the response from
        # looking like the planner silently guessed product_line/store_name.
        result["lookup_matches"] = sanitize_for_json(
            public_lookup_slots(state.get("lookup_matches") or [])
        )
        result["lookup_context"] = public_lookup_context(
            state.get("lookup_plan"),
            state.get("lookup_context") or "",
        )
        if state.get("lookup_plan"):
            result["lookup_plan"] = sanitize_for_json(
                public_lookup_plan(state.get("lookup_plan"))
            )

        result["policies"] = to_payload(state.get("applied_policies") or [])

        return {"result": result}
