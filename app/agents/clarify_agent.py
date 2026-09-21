# app/agents/clarify_agent.py
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field

from app.service.llm_service import get_llm


class ClarificationDecision(BaseModel):
    clarification_question: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    missing_slots: list[str] = Field(default_factory=list)


def _history_text(chat_history: list[dict] | None, limit: int = 8) -> str:
    lines: list[str] = []
    for item in (chat_history or [])[-limit:]:
        role = item.get("role", "unknown")
        content = item.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "None"


def _last_assistant_payload(chat_history: list[dict] | None) -> str:
    if not chat_history:
        return "None"

    for item in reversed(chat_history):
        if item.get("role") != "assistant":
            continue
        content = item.get("content", "")
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return content or "None"

    return "None"


async def generate_clarification(
    query: str,
    *,
    chat_history: list[dict] | None = None,
    glossary_hits: list[dict] | None = None,
    glossary_context: str = "",
    resolved_term: Optional[str] = None,
    intent_reason: Optional[str] = None,
    plan_reason: Optional[str] = None,
    planner_question: Optional[str] = None,
) -> ClarificationDecision:
    glossary_preview = json.dumps(glossary_hits[:8] if glossary_hits else [], ensure_ascii=False, indent=2)

    prompt = f"""
You are the clarification generator for a CUSTOMER INTELLIGENCE copilot.

Return JSON:
{{
  "clarification_question": "one short precise question to ask the user",
  "reason": "why clarification is needed",
  "missing_slots": ["metric", "time_period", "grouping", "field", "filter"]
}}

Goal:
- Ask the MINIMUM clarification needed to proceed.
- Ask exactly ONE concise question.
- Do not ask multiple stacked questions unless absolutely necessary.
- Do not hardcode domain-specific canned wording.
- Infer as much as possible from history, glossary hits, prior assistant reply, and planner context.
- Prefer business-friendly wording.
- If the planner already produced a good clarification_question, refine it only if needed.
- If the user is continuing a prior analytics thread, assume that context unless it is genuinely ambiguous.
- If ambiguity is about field choice, use glossary candidates.
- If ambiguity is about metric/time/filter/grouping, ask only for the missing piece.
- IMPORTANT: ask a clarification question only when the request is actually answerable from the available tables after the user provides one missing detail.
- Do NOT try to rescue an unsupported request by inventing a clarification question.
- If the planner context suggests missing data coverage rather than missing user intent, keep the clarification tightly aligned to the planner question only.
- Never ask the user to provide internal database IDs such as store IDs or
  category keys. Ask for the business name, or ask them to choose from named
  options if options are available.

Available sources:
1. Recent chat history
2. Last assistant payload
3. Top glossary hits
4. Relevant schema context
5. Intent reason
6. Planner reason
7. Planner clarification question

Intent reason:
{intent_reason or "None"}

Planner reason:
{plan_reason or "None"}

Planner clarification question:
{planner_question or "None"}

Resolved shorthand term:
{resolved_term or "None"}

Last assistant payload:
{_last_assistant_payload(chat_history)}

Top glossary hits:
{glossary_preview}

Relevant schema context:
{glossary_context or "None"}

Recent chat history:
{_history_text(chat_history)}

Current user query:
{query}
""".strip()

    return await get_llm().generate_json(prompt, ClarificationDecision)
