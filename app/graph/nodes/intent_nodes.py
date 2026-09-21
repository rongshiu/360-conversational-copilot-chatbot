# app/graph/nodes/intent_nodes.py
from __future__ import annotations

from app.agents.intent_agent import detect_intent
from app.graph.copilot_state import CopilotState


class IntentNodesMixin:

    async def classify_intent(self, state: CopilotState) -> CopilotState:
        # Critical resolver fix:
        # If the previous assistant turn was a clarification and the current user reply
        # is a short clarification answer like:
        # - "kids"
        # - "10"
        # - "grocery"
        # - "2026"
        #
        # do NOT let the intent LLM decide freely. Route back to analytics planning,
        # which will run resolve_lookup first.
        #
        # Without this, short replies can fall into generic clarify and lose the
        # resolver's numbered candidate list.
        if state.get("clarification_followup"):
            # The router is skipped here, so it cannot say what this turn names --
            # and the query it would have read is a MERGED one, carrying the
            # original question plus the copilot's own instruction lines. Guessing
            # over that is how a "yes" produced a seafood filter.
            #
            # Only the words the user just typed are open to guessing. Everything
            # the original question named was resolved, or asked about, last turn
            # and is carried forward in the resolution plan.
            return {
                "intent": "analytics",
                "route": "plan_sql",
                "intent_reason": state.get("clarification_followup_reason")
                or "User answered a pending clarification; proceed with analytics SQL planning.",
                "is_followup": True,
                "resolved_term": None,
                "entity_phrases": [str(state.get("original_query") or "")],
            }

        decision = await detect_intent(
            state["query"],
            chat_history=state.get("history"),
            glossary_hits=state.get("glossary_hits"),
        )

        return {
            "intent": decision.intent,
            "route": decision.route,
            "intent_reason": decision.reason,
            "is_followup": decision.is_followup,
            "resolved_term": decision.resolved_term,
            # Bounds the entity resolver's fuzzy pass. None means the router had no
            # opinion; [] means it read the question and found nothing named.
            "entity_phrases": decision.entity_phrases,
        }

    def route_after_intent(self, state: CopilotState) -> str:
        route = state.get("route")
        if route == "answer_from_previous":
            return "answer_from_previous"
        if route == "plan_sql":
            return "resolve_lookup"
        if route == "glossary":
            return "glossary"
        if route == "smalltalk":
            return "smalltalk"
        return "clarify"
