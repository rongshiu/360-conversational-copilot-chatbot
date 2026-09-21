# app/agents/conversation_turn_agent.py
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.service.llm_service import get_llm
from app.policies.clarification_policy import is_terminal_lookup_failure_clarify
from app.core.logging import Logger

logger = Logger.get_logger(__name__)


class ConversationTurnDecision(BaseModel):
    relation_to_previous_clarification: Literal[
        "new_question",
        "clarification_answer",
        "not_applicable",
    ] = Field(
        description=(
            "Whether the current user message should answer the previous assistant "
            "clarification or start a new standalone question."
        )
    )

    reuse_previous_clarification: bool = Field(
        default=False,
        description=(
            "True only when the current user message directly answers the immediately "
            "previous assistant clarification."
        ),
    )

    inherits_previous_filters: bool = Field(
        default=False,
        description=(
            "True when the current user message explicitly refers to prior conversation "
            "context and should reuse previously resolved filters or cohort constraints."
        ),
    )

    inherits_entity_filters: bool = Field(
        default=False,
        description=(
            "True when the new question continues the previous one and should keep "
            "its already-resolved store/product/category entities. The resolution "
            "plan carries every resolved entity forward, so this is a single flag "
            "rather than a list of scopes to inherit."
        ),
    )

    inherits_time_period: bool = Field(
        default=False,
        description=(
            "True when the current user message explicitly asks to reuse the previous "
            "reporting period."
        ),
    )

    contextual_rewrite: str | None = Field(
        default=None,
        description=(
            "Optional short rewrite of the current query with contextual references "
            "made explicit, without inventing unresolved values."
        ),
    )

    reason: str = Field(description="Short reason for the decision.")


def _compact_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}

    return {
        "type": payload.get("type"),
        "answer": payload.get("answer"),
        "intent_reason": payload.get("intent_reason"),
        "lookup_plan": payload.get("lookup_plan"),
        "lookup_matches": payload.get("lookup_matches"),
        "lookup_context": payload.get("lookup_context"),
        "sql": payload.get("sql"),
    }


async def decide_conversation_turn_relation(
    *,
    current_query: str,
    previous_assistant_payload: dict | None,
) -> ConversationTurnDecision:
    """
    LLM-first decision for stale clarification prevention and contextual follow-up recovery.

    Key rule:
    - If the previous assistant asked a clarification but the current user message is a
      new standalone question, the pending clarification is abandoned.
    - Abandoned clarification state must not leak old lookup values into the new turn.
    """

    if not isinstance(previous_assistant_payload, dict):
        return ConversationTurnDecision(
            relation_to_previous_clarification="not_applicable",
            reuse_previous_clarification=False,
            inherits_previous_filters=False,
            inherits_entity_filters=False,
            inherits_time_period=False,
            contextual_rewrite=None,
            reason="There is no previous assistant payload.",
        )

    if is_terminal_lookup_failure_clarify(previous_assistant_payload):
        return ConversationTurnDecision(
            relation_to_previous_clarification="new_question",
            reuse_previous_clarification=False,
            inherits_previous_filters=False,
            inherits_entity_filters=False,
            inherits_time_period=False,
            contextual_rewrite=None,
            reason=(
                "Previous clarification was a terminal lookup failure, so pending "
                "lookup clarification state was cleared. The next user message must "
                "start as a new question instead of answering the failed lookup."
            ),
        )

    prompt = f"""
You are the conversation-state controller for NorthCo Customer Intelligence Copilot.

Your job:
Decide whether the current user message is:
1. directly answering the immediately previous assistant clarification, or
2. asking a new question.

Return JSON only:
{{
  "relation_to_previous_clarification": "new_question" | "clarification_answer" | "not_applicable",
  "reuse_previous_clarification": true | false,
  "inherits_previous_filters": true | false,
  "inherits_entity_filters": true | false,
  "inherits_time_period": true | false,
  "contextual_rewrite": "short rewrite or null",
  "reason": "short reason"
}}

Hard rule:
reuse_previous_clarification must be true ONLY when the current user message directly answers
the immediately previous assistant clarification.

A clarification answer is usually a short selection, short value, direct correction, or missing
detail requested by the previous assistant.

A full standalone analytics question is always a new_question, even if it is identical or very
similar to an earlier question in the same thread.

Do not treat a repeated full question as a clarification answer.
Do not reuse previous selected lookup values just because the current full question is similar
or identical to the earlier question that caused the clarification.
Do not inherit pending lookup state from a previous clarification when the user asks a full
standalone analytics question again.

If the user asks a full standalone analytics question, then:
- relation_to_previous_clarification = "new_question"
- reuse_previous_clarification = false
- inherits_previous_filters = false, unless the user explicitly says same/previous/that/this/there/those
- inherits_entity_filters = false
- inherits_time_period = false, unless the user explicitly asks for the same period

Examples of clarification_answer:
Previous assistant: "Which month, quarter, or year should I use?"
User: "2026"
=> clarification_answer, reuse_previous_clarification=true

Previous assistant: "Which exact store do you mean? 1. NorthCo Mart Veldra Selbycross 2. NorthCo Mall Veldra"
User: "1"
=> clarification_answer, reuse_previous_clarification=true

Previous assistant: "Which product category do you mean?"
User: "Hardline"
=> clarification_answer, reuse_previous_clarification=true

Examples of new_question:
Previous assistant asked anything about FOOD.
User: "How many members have never bought a Hardline product?"
=> new_question, reuse_previous_clarification=false, inherits_previous_filters=false

Previous assistant asked anything about a store.
User: "How many customers visited NorthCo Veldra in Dec 2026?"
=> new_question, reuse_previous_clarification=false, inherits_previous_filters=false

Previous assistant asked anything about grocery.
User: "What percentage of members bought both Food and Hardline?"
=> new_question, reuse_previous_clarification=false, inherits_previous_filters=false

Contextual follow-up examples:
Previous assistant answered for NorthCo Mart Veldra.
User: "What about the same store in 2026?"
=> new_question, reuse_previous_clarification=false, inherits_previous_filters=true, inherits_entity_filters=true, inherits_time_period=false

Previous assistant answered for Hardline in 2026.
User: "What about the same product last year?"
=> new_question, reuse_previous_clarification=false, inherits_previous_filters=true, inherits_entity_filters=true, inherits_time_period=false

Previous assistant answered for a selected store.
User: "What about there?"
=> new_question, reuse_previous_clarification=false, inherits_previous_filters=true, inherits_entity_filters=true

Important:
- Do not inherit product/store filters just because they appear in the previous assistant payload.
- Inherit only when the current user message explicitly refers back to prior context.
- A full standalone question cancels any pending clarification.
- A repeated full standalone question also cancels any pending clarification.
- Similar wording to an earlier question is not enough reason to reuse previous clarification state.
- Never carry an old pending lookup value into a new full question.

Previous assistant payload:
{json.dumps(_compact_payload(previous_assistant_payload), ensure_ascii=False)}

Current user message:
{current_query}
""".strip()

    try:
        decision = await get_llm().generate_json(prompt, ConversationTurnDecision)

        previous_was_clarify = previous_assistant_payload.get("type") == "clarify"

        if not previous_was_clarify:
            decision.relation_to_previous_clarification = "not_applicable"
            decision.reuse_previous_clarification = False

        if decision.relation_to_previous_clarification != "clarification_answer":
            decision.reuse_previous_clarification = False

        if decision.reuse_previous_clarification:
            decision.relation_to_previous_clarification = "clarification_answer"

        # Only inherit entities when the model explicitly says filters carry over.
        if not decision.inherits_previous_filters:
            decision.inherits_entity_filters = False

        if decision.inherits_entity_filters or decision.inherits_time_period:
            decision.inherits_previous_filters = True

        return decision

    except Exception as exc:
        logger.warning("Conversation turn LLM decision failed: %s", exc)

        # Safe default:
        # Do not reuse stale clarification/filter state if the controller fails.
        return ConversationTurnDecision(
            relation_to_previous_clarification="new_question",
            reuse_previous_clarification=False,
            inherits_previous_filters=False,
            inherits_entity_filters=False,
            inherits_time_period=False,
            contextual_rewrite=None,
            reason=f"Conversation turn decision failed, so stale state was not reused: {exc}",
        )