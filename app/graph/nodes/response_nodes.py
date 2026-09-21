# app/graph/nodes/response_nodes.py
from __future__ import annotations

from app.agents.answer_agent import _make_chart, build_glossary_response
from app.agents.answer_from_previous_agent import answer_from_previous
from app.agents.glossary_agent import explain_glossary_term
from app.agents.smalltalk_agent import generate_smalltalk_answer
from app.graph.copilot_state import CopilotState
from app.service.access_policy import denial, to_payload
from app.utils.chart_utils import (
    build_points_from_rows,
    looks_like_point_list,
    normalize_point_list,
    resolve_line_chart,
)
from app.utils.common import sanitize_for_json
from app.core.logging import Logger

logger = Logger.get_logger(__name__)


class ResponseNodesMixin:

    async def answer_from_previous_node(self, state: CopilotState) -> CopilotState:
        response = await answer_from_previous(
            state["query"],
            previous_payload=state.get("previous_assistant_payload"),
            chat_history=state.get("history"),
        )

        if response.should_replan:
            return {
                "route": "plan_sql",
                "plan_reason": response.replan_reason or "Follow-up requires a fresh SQL query.",
            }

        previous_payload = state.get("previous_assistant_payload") or {}
        rows = sanitize_for_json(previous_payload.get("rows") or [])

        chart = None

        if response.chart_type == "table":
            if rows:
                chart = {
                    "chart_type": "table",
                    "title": response.chart_title,
                    "columns": response.columns or [],
                    "data": rows,
                }

        elif response.chart_type in {"bar_chart", "donut_chart"}:
            raw_chart_data = response.chart_data or []

            if looks_like_point_list(raw_chart_data):
                points = normalize_point_list(raw_chart_data, limit=20)
            else:
                points = build_points_from_rows(
                    rows,
                    label_field=response.label_field,
                    value_field=response.value_field,
                    limit=20,
                )

            # Route through the shared chart builder so bar/donut charts always
            # carry value_format and per-point percentage, exactly like the fresh
            # analytics path (build_analytics_response). Keeping a second hand-built
            # dict here is what made value_format inconsistent across turns.
            chart = _make_chart(
                response.chart_type,
                response.chart_title,
                points,
                x_axis=response.x_axis,
                y_axis=response.y_axis,
                label_field=response.label_field,
                value_field=response.value_field,
            )

        elif response.chart_type == "line_chart":
            resolved_x_field, resolved_series, resolved_data = resolve_line_chart(
                chart_data=response.chart_data or [],
                fallback_rows=rows,
                x_field=getattr(response, "x_field", None),
                series=response.series or [],
                limit=50,
            )

            if resolved_x_field and resolved_series and resolved_data:
                chart = {
                    "chart_type": "line_chart",
                    "title": response.chart_title,
                    "x_field": resolved_x_field,
                    "x_axis": response.x_axis,
                    "y_axis": response.y_axis,
                    "series": resolved_series,
                    "data": sanitize_for_json(resolved_data),
                }

        result = {
            "type": "analytics",
            "answer": response.answer,
            "chart": sanitize_for_json(chart),
            "rows": rows,
            "sql": previous_payload.get("sql"),
            "glossary_matches": [],
            "intent_reason": state.get("intent_reason"),
        }
        return {"result": sanitize_for_json(result)}

    def route_after_answer_from_previous(self, state: CopilotState) -> str:
        if state.get("route") == "plan_sql" and not state.get("result"):
            return "resolve_lookup"
        return "end_answer_from_previous"

    async def glossary_answer(self, state: CopilotState) -> CopilotState:
        try:
            answer = await explain_glossary_term(
                state["query"],
                matches=state.get("glossary_hits", []),
                glossary_context=state.get("glossary_context", ""),
                intent_reason=state.get("intent_reason"),
            )

            result = {
                "type": "glossary",
                "answer": answer,
                "chart": None,
                "rows": [],
                "sql": None,
                "glossary_matches": [],
                "intent_reason": state.get("intent_reason"),
            }
            return {"result": result}

        except Exception:
            result = build_glossary_response(
                matches=state.get("glossary_hits", []),
                reason=state.get("intent_reason"),
            )
            return {"result": result}

    async def out_of_scope_answer(self, state: CopilotState) -> CopilotState:
        """Terminal for authorization refusals, as distinct from errors.

        Three things can put a request here:
          - an entity outside the caller's OpCo grant
          - a request to identify individual customers, which no grain answers
          - a revenue metric requested by an EXEC caller, when no volume measure
            answers the same question

        None of these are failures, and none should surface as an error or as an
        empty result. An empty table would read as "there is no data", which is a
        materially wrong answer -- the data exists, the caller may not see it.

        The refusal names the scope but never the excluded entity. Naming it would
        confirm that an out-of-scope store or category exists, which is the exact
        disclosure the scoped entity dictionary is built to prevent.

        The same refusal also leaves as a structured `policies` entry, so a client
        can distinguish it from an empty result without parsing the prose.
        """
        principal = state.get("principal") or self.principal
        reason = state.get("out_of_scope_reason") or "That is outside your access scope."

        scope_line = ""
        if principal is not None:
            scope_line = f"\n\nYour current access: {principal.describe_scope()}."

        suggestion = ""
        if principal is not None and not principal.can_see_money:
            suggestion = (
                "\n\nI can still answer this using volume measures such as "
                "transactions, units, or customer counts if that is useful."
            )

        # The refusal itself, first in the list, followed by anything recorded
        # earlier in the turn.
        #
        # Only when a code is set. The default used to be "opco_out_of_scope",
        # which named a control that no longer exists -- so a refusal that reached
        # here without a code reported a policy nothing had applied. Every route
        # into this terminal sets one (customer_identity from the planner,
        # role_money_withheld from the lookup guard); if a future one does not, the
        # caller gets the prose refusal and no machine-readable claim about why,
        # which is the honest outcome.
        code = state.get("out_of_scope_code")
        policies = [denial(code, reason, principal)] if code else []
        policies.extend(state.get("applied_policies") or [])

        result = {
            "type": "out_of_scope",
            "answer": f"{reason}{scope_line}{suggestion}",
            "chart": None,
            "rows": [],
            "sql": None,
            "glossary_matches": [],
            "intent_reason": state.get("intent_reason"),
            "answer_source": "out_of_scope",
            "denied_reason": reason,
            "policies": to_payload(policies),
        }
        return {"result": sanitize_for_json(result)}

    async def smalltalk_answer(self, state: CopilotState) -> CopilotState:
        try:
            answer = await generate_smalltalk_answer(
                state["query"],
                chat_history=state.get("history"),
                glossary_hits=state.get("glossary_hits"),
                glossary_service=self.glossary_service,
                intent_reason=state.get("intent_reason"),
            )
        except Exception as exc:
            logger.warning("Smalltalk LLM answer failed; using safe fallback: %s", exc)
            answer = (
                "I can help with Customer Intelligence questions grounded in the configured Customer 360 "
                "serving tables and glossary. You can ask about customer counts, monthly trends, store or "
                "category performance, opco comparisons, payment/time-bucket behavior, customer drilldowns, "
                "or glossary definitions."
            )

        result = {
            "type": "smalltalk",
            "answer": answer,
            "chart": None,
            "rows": [],
            "sql": None,
            "glossary_matches": [],
            "intent_reason": state.get("intent_reason"),
        }
        return {"result": sanitize_for_json(result)}
