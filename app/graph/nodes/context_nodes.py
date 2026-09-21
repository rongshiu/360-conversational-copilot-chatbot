# app/graph/nodes/context_nodes.py
from __future__ import annotations

from app.agents.conversation_turn_agent import decide_conversation_turn_relation
from app.graph.copilot_state import CopilotState
from app.graph.helpers.sql_guards import fact_time_period_guard_question
from app.policies.clarification_policy import (
    build_clarification_followup_query,
    history_without_active_clarification_window,
    is_prior_assistant_clarification,
    is_terminal_lookup_failure_clarify,
    looks_like_short_clarification_answer,
    parse_last_assistant_payload,
)
from app.service.access_policy import to_payload
from app.service.table_registry import PERIOD_REQUIRED_VIEWS
from app.utils.schema_utils import schema_match_confidence


class ContextNodesMixin:

    def _fact_time_period_guard_question(self, state: CopilotState, sql: str | None) -> str | None:
        return fact_time_period_guard_question(
            state,
            sql,
            fact_tables=PERIOD_REQUIRED_VIEWS,
            postgres_schema=self.settings.postgres_schema,
        )

    async def load_context(self, state: CopilotState) -> CopilotState:
        thread_id = str(state.get("thread_id") or "").strip()
        raw_history = list(state.get("history") or [])
        history = raw_history[-self.settings.chat_history_limit :]

        raw_query = state["query"]
        previous_assistant_payload = parse_last_assistant_payload(history)

        terminal_lookup_failure_abandoned = is_terminal_lookup_failure_clarify(
            previous_assistant_payload
        )

        turn_decision = await decide_conversation_turn_relation(
            current_query=raw_query,
            previous_assistant_payload=previous_assistant_payload,
        )

        reuse_previous_clarification = bool(
            turn_decision.reuse_previous_clarification
            and turn_decision.relation_to_previous_clarification == "clarification_answer"
        )

        previous_was_pending_clarification = is_prior_assistant_clarification(
            previous_assistant_payload
        )

        deterministic_clarification_reply = bool(
            previous_was_pending_clarification
            and not terminal_lookup_failure_abandoned
            and looks_like_short_clarification_answer(raw_query)
        )

        if deterministic_clarification_reply:
            reuse_previous_clarification = True

        pending_clarification_abandoned = bool(
            terminal_lookup_failure_abandoned
            or (
                previous_was_pending_clarification
                and not reuse_previous_clarification
            )
        )

        # If the pending clarification is abandoned, do not let the active
        # clarification chain leak into downstream agents through chat history.
        #
        # This is the important fix for repeated full questions after terminal
        # lookup failure. The old assistant payload may contain:
        # - selected_lookup_context
        # - lookup_matches
        # - lookup_plan with a resolved store and pending product
        #
        # Downstream agents should not see those as active context.
        downstream_history = (
            history_without_active_clarification_window(history)
            if pending_clarification_abandoned
            else history
        )

        previous_payload_for_clarification = (
            previous_assistant_payload
            if reuse_previous_clarification and not terminal_lookup_failure_abandoned
            else None
        )

        effective_query, clarification_followup, clarification_reason = build_clarification_followup_query(
            current_query=raw_query,
            history=history,
            previous_payload=previous_payload_for_clarification,
        )

        # The resolution plan carries every resolved entity forward, so entity
        # inheritance is one boolean rather than a per-scope set.
        inherits_entity_filters = bool(
            turn_decision.inherits_entity_filters and not pending_clarification_abandoned
        )

        if clarification_followup and not clarification_reason:
            clarification_reason = turn_decision.reason

        # Carry the previous resolution plan forward when this turn continues the
        # last one -- either answering its clarification, or reusing its filters
        # for a new cut of the same question.
        active_lookup_plan = None
        if not pending_clarification_abandoned and isinstance(previous_assistant_payload, dict):
            if (reuse_previous_clarification and clarification_followup) or inherits_entity_filters:
                active_lookup_plan = previous_assistant_payload.get("lookup_plan")

        # A short reply to a pending clarification is the user's option choice.
        # The resolver matches it against the options it presented.
        clarification_selected_option = (
            raw_query if (clarification_followup and reuse_previous_clarification) else None
        )

        glossary_hits = self.glossary_service.search(effective_query, limit=10)
        glossary_context = self.glossary_service.get_relevant_schema_context(effective_query, limit=24)

        schema_confidence = schema_match_confidence(effective_query, glossary_hits)

        previous_payload_for_state = (
            None if pending_clarification_abandoned else previous_assistant_payload
        )

        return {
            "query": effective_query,
            "original_query": raw_query,
            "clarification_followup": clarification_followup,
            "clarification_followup_reason": clarification_reason,
            "inherits_previous_filters": (
                False
                if pending_clarification_abandoned
                else bool(turn_decision.inherits_previous_filters)
            ),
            "inherits_entity_filters": inherits_entity_filters,
            "inherits_time_period": (
                False
                if pending_clarification_abandoned
                else bool(turn_decision.inherits_time_period)
            ),
            "contextual_rewrite": (
                None
                if pending_clarification_abandoned
                else turn_decision.contextual_rewrite
            ),
            "contextual_followup_reason": turn_decision.reason,
            "clarification_selected_option": clarification_selected_option,
            "thread_id": thread_id,
            "history": downstream_history,
            "glossary_hits": glossary_hits,
            "glossary_context": glossary_context,
            "previous_assistant_payload": previous_payload_for_state,
            "lookup_plan": active_lookup_plan,
            "schema_match_confidence": schema_confidence,
            "retry_count": state.get("retry_count", 0),
            "sql_attempts": state.get("sql_attempts", []),
        }

    async def save_turn(self, state: CopilotState) -> CopilotState:
        """Append the latest user/assistant turn into checkpoint state history.

        This is the only conversation memory path when using checkpoint-only mode.
        No chat_sessions/chat_messages tables are written.
        """
        from datetime import datetime, timezone
        import json

        now = datetime.now(timezone.utc).isoformat()
        history = list(state.get("history") or [])
        result = dict(state.get("result") or {})

        # Normalize `policies` here rather than in each terminal. Every terminal
        # edges into save_turn, so this is the one place that cannot be forgotten --
        # and it was: glossary, smalltalk, unsupported and answer_from_previous all
        # returned `null`, which a client has to special-case against `[]` for no
        # reason. setdefault, so a terminal that built its own list still wins.
        #
        # Defaulting from applied_policies rather than from [] also carries a policy
        # recorded earlier in the turn -- a money substitution is known before
        # planning, so it must survive a turn that then ends in a clarification or
        # an unsupported verdict instead of an answer.
        if result:
            result.setdefault("policies", to_payload(state.get("applied_policies") or []))

        thread_id = str(state.get("thread_id") or "")
        request_id = state.get("request_id")

        assistant_payload = dict(result)
        if thread_id:
            assistant_payload.setdefault("thread_id", thread_id)
        if request_id:
            assistant_payload.setdefault("request_id", request_id)

        query = str(state.get("original_query") or state.get("query") or "")
        if query:
            history.append(
                {
                    "role": "user",
                    "content": query,
                    "created_at": now,
                    "extra": {
                        "request_id": request_id,
                        "message_kind": "user_query",
                    },
                }
            )

        if assistant_payload:
            history.append(
                {
                    "role": "assistant",
                    "content": json.dumps(assistant_payload, ensure_ascii=False),
                    "created_at": now,
                    "extra": {
                        "request_id": request_id,
                        "message_kind": "assistant_response",
                        "type": assistant_payload.get("type"),
                    },
                }
            )

        limit = max(2, int(getattr(self.settings, "chat_history_limit", 12) or 12))
        # `result` is returned as well as `history` because the normalization above
        # mutates a copy; without this the caller reads the pre-normalized one.
        return {"history": history[-limit:], "result": result}

    async def reset_turn_state(self, state: CopilotState) -> CopilotState:
        """
        LangGraph checkpoint restores the previous full state.

        We only want long-lived memory such as history to survive.
        Per-turn execution fields must be cleared before processing the next query.
        """
        return {
            "original_query": None,
            "clarification_followup": False,
            "clarification_followup_reason": None,

            "inherits_previous_filters": False,
            "inherits_entity_filters": False,
            "inherits_time_period": False,
            "contextual_rewrite": None,
            "contextual_followup_reason": None,

            "clarification_selected_option": None,
            "out_of_scope_reason": None,
            "out_of_scope_code": None,
            "applied_policies": [],

            "glossary_hits": [],
            "glossary_context": "",
            "schema_match_confidence": "none",

            "intent": None,
            "route": None,
            "intent_reason": "",
            "is_followup": False,
            "resolved_term": None,
            "entity_phrases": None,

            "previous_assistant_payload": None,

            "lookup_matches": [],
            "lookup_context": "",
            "lookup_plan": None,
            "lookup_needs_clarification": False,
            "lookup_clarification_question": None,

            "sql": None,
            "plan_status": None,
            "plan_reason": None,
            "needs_clarification": False,
            "clarification_question": None,
            "unsupported_reason": None,
            "unsupported_cause": "not_answerable",
            "missing_slots": [],

            "validation_error": None,
            "execution_error": None,
            "retry_count": 0,
            "sql_attempts": [],

            "rows_payload": {},
            "analysis_result": {},

            "result": {},
            "error": None,
        }
