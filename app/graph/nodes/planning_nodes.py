# app/graph/nodes/planning_nodes.py
from __future__ import annotations

from app.agents.answer_agent import build_clarify_response, build_unsupported_response
from app.agents.clarify_agent import generate_clarification
from app.agents.planner_agent import plan_sql
from app.graph.copilot_state import CopilotState
from app.service.entity_resolution.models import (
    public_lookup_context,
    public_lookup_plan,
    public_lookup_slots,
)
from app.service.access_policy import denial, to_payload
from app.service.stream_context import emit_status
from app.utils.common import sanitize_for_json
from app.utils.schema_utils import generic_fallback_clarification


# Which planner refusal reports which policy. A cause absent from this map reports
# none -- "not_answerable" is a schema limit that no permission grant would lift, so
# there is no policy to name and a client should not offer "request access".
#
# "out_of_scope" -> "opco_out_of_scope" was the second entry, and it outlived the
# thing it described. Access is not scoped by OpCo or category, so the cause can
# never occur; the code it produced is absent from PolicyNotice's union and has no
# ENFORCED_BY entry, so a client branching on `code` received a value the contract
# said could not be sent, with an empty `enforced_by` beside it.
UNSUPPORTED_CAUSE_POLICY: dict[str, str] = {
    "customer_identity": "customer_identity",
}


class PlanningNodesMixin:

    async def make_plan(self, state: CopilotState) -> CopilotState:
        await emit_status(
            "replanning the query" if state.get("retry_count") else "planning the query"
        )
        retry_reason = (
            state.get("validation_error")
            or state.get("execution_error")
            or state.get("plan_reason")
        )

        previous_payload = state.get("previous_assistant_payload") or {}

        is_retry = bool(state.get("validation_error") or state.get("execution_error"))

        previous_sql = (
            state.get("sql")
            if is_retry and state.get("sql")
            else previous_payload.get("sql")
        )

        previous_rows = previous_payload.get("rows") or []

        plan = await plan_sql(
            state["query"],
            chat_history=state.get("history"),
            glossary_context=state.get("glossary_context", ""),
            resolved_term=state.get("resolved_term"),
            previous_sql=previous_sql,
            previous_rows=previous_rows,
            retry_reason=retry_reason,
            schema_match_confidence=state.get("schema_match_confidence"),
            lookup_context=state.get("lookup_context"),
            is_followup=bool(state.get("is_followup")),
            principal=state.get("principal") or self.principal,
        )

        # An invented period is refused, not executed. The SQL is well-formed and
        # carries a period filter, so nothing downstream can tell that the period is
        # not the one asked for -- only the model that read the question knows, which
        # is why it reports `period_source` and this decides.
        #
        # The clarification offers the period it would have used, so the user answers
        # in one step instead of guessing what is acceptable.
        if plan.status == "answerable" and plan.sql and plan.period_source == "assumed":
            assumed = (plan.period_label or "").strip()
            offer = (
                f' I can use {assumed} -- reply "{assumed}" to confirm -- or give '
                "another period."
                if assumed
                else ""
            )
            return {
                "sql": None,
                "plan_status": "needs_clarification",
                "plan_reason": (
                    "The question did not state a time period, so the planner chose "
                    f"one ({assumed or 'unspecified'}). Every serving table is "
                    "period-grained; answering with an assumed period would report a "
                    "real number for a question nobody asked."
                ),
                "needs_clarification": True,
                "clarification_question": (
                    "Which time period would you like? Every table here is "
                    "period-grained (daily for sales, monthly for customers), so "
                    f"there is no all-time total.{offer}"
                ),
                "unsupported_reason": None,
                "unsupported_cause": "not_answerable",
                "missing_slots": ["time_period"],
                "sql_attempts": list(state.get("sql_attempts", [])) + [plan.sql],
                "validation_error": None,
                "execution_error": None,
            }

        fact_time_guard_question = self._fact_time_period_guard_question(state, plan.sql)

        if plan.status == "answerable" and plan.sql and fact_time_guard_question:
            return {
                "sql": None,
                "plan_status": "needs_clarification",
                "plan_reason": (
                    "SQL planning produced a query with no period filter. Every serving "
                    "table is period-grained, so execution is blocked rather than "
                    "silently scanning all history."
                ),
                "needs_clarification": True,
                "clarification_question": fact_time_guard_question,
                "unsupported_reason": None,
                "unsupported_cause": "not_answerable",
                "missing_slots": ["time_period"],
                "sql_attempts": list(state.get("sql_attempts", [])) + [plan.sql],
                "validation_error": None,
                "execution_error": None,
            }

        attempts = list(state.get("sql_attempts", []))

        if plan.sql:
            attempts.append(plan.sql)

        return {
            "sql": plan.sql,
            "plan_status": plan.status,
            "plan_reason": plan.reason,
            "needs_clarification": plan.needs_clarification,
            "clarification_question": plan.clarification_question,
            "unsupported_reason": plan.unsupported_reason,
            "unsupported_cause": plan.unsupported_cause,
            # Feeds the refusal terminal when route_after_plan sends an identity
            # question there. Set from the plan rather than restated, so the caller
            # reads the planner's own wording for why it refused.
            "out_of_scope_reason": (
                plan.unsupported_reason
                if plan.unsupported_cause == "customer_identity"
                else None
            ),
            "out_of_scope_code": (
                "customer_identity" if plan.unsupported_cause == "customer_identity" else None
            ),
            "missing_slots": plan.missing_slots,
            "sql_attempts": attempts,
            "validation_error": None,
            "execution_error": None,
        }

    def route_after_plan(self, state: CopilotState) -> str:
        status = state.get("plan_status")
        if status == "unsupported":
            # An identity question is a refusal, not a gap in the schema's coverage
            # of a legitimate request, so it goes to the refusal terminal -- the same
            # one the old regex guard used. Routing it here keeps the response
            # identical to what that guard produced, so removing the regex changed
            # which component decides and nothing the caller sees.
            if state.get("unsupported_cause") == "customer_identity":
                return "out_of_scope"
            return "unsupported_from_plan"
        if status == "needs_clarification" or not state.get("sql"):
            return "clarify_from_plan"
        return "validate_sql"

    async def unsupported_from_plan(self, state: CopilotState) -> CopilotState:
        reason = " | ".join(
            x
            for x in [
                state.get("intent_reason"),
                state.get("plan_reason"),
                state.get("unsupported_reason"),
            ]
            if x
        )

        answer = state.get("unsupported_reason") or (
            "I can’t answer that from the currently available customer intelligence tables."
        )

        result = build_unsupported_response(answer=answer, reason=reason)

        # A refusal the planner made on scope grounds is an authorization event and
        # is reported as one, even though `type` stays "unsupported". Without this a
        # client cannot tell "you lack access to that OpCo" from "no table can answer
        # this" -- the first is worth requesting access for, the second never will be.
        # A substitution recorded earlier did not happen: the planner refused rather
        # than answering with the volume equivalent. Reporting both would hand the
        # caller a response that says it substituted a measure AND that it returned
        # nothing, which cannot both be true.
        policies = [
            p for p in (state.get("applied_policies") or []) if p.get("effect") != "substituted"
        ]
        code = UNSUPPORTED_CAUSE_POLICY.get(state.get("unsupported_cause") or "")
        if code:
            policies.insert(
                0,
                denial(
                    code,
                    state.get("unsupported_reason") or "That is outside your access scope.",
                    state.get("principal") or self.principal,
                ),
            )
        result["policies"] = to_payload(policies)

        return {"result": result}

    async def clarify_from_plan(self, state: CopilotState) -> CopilotState:
        if "time_period" in (state.get("missing_slots") or []) and state.get("clarification_question"):
            reason = " | ".join(
                x
                for x in [
                    state.get("intent_reason"),
                    state.get("plan_reason"),
                    "Monthly fact-table SQL requires an explicit reporting period.",
                ]
                if x
            )

            result = build_clarify_response(
                state["clarification_question"],
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

            return {"result": result}

        try:
            clarification = await generate_clarification(
                state["query"],
                chat_history=state.get("history"),
                glossary_hits=state.get("glossary_hits"),
                glossary_context=state.get("glossary_context", ""),
                resolved_term=state.get("resolved_term"),
                intent_reason=state.get("intent_reason"),
                plan_reason=state.get("plan_reason"),
                planner_question=state.get("clarification_question"),
            )
            question = clarification.clarification_question
            reason = " | ".join(
                x
                for x in [
                    state.get("intent_reason"),
                    state.get("plan_reason"),
                    clarification.reason,
                ]
                if x
            )
        except Exception:
            question = state.get("clarification_question") or generic_fallback_clarification(state)
            reason = " | ".join(
                x for x in [state.get("intent_reason"), state.get("plan_reason")] if x
            )

        result = build_clarify_response(question, reason=reason)

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

        return {"result": result}
