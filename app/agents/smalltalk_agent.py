# app/agents/smalltalk_agent.py
from __future__ import annotations

import json
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.service.glossary_service import GlossaryService
from app.service.llm_service import get_llm
from app.service.table_registry import TABLE_PROFILES
from app.service.business_context import NORTHCO_360_BUSINESS_CONTEXT


class SmalltalkResponse(BaseModel):
    answer: str = Field(..., min_length=1)


def _history_text(chat_history: list[dict] | None, limit: int = 8) -> str:
    lines: list[str] = []

    for item in (chat_history or [])[-limit:]:
        role = item.get("role", "unknown")
        content = item.get("content", "")

        # Stored assistant messages are JSON payloads. Keep them compact.
        if role == "assistant":
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    content = parsed.get("answer") or content
            except Exception:
                pass

        lines.append(f"{role}: {content}")

    return "\n".join(lines) if lines else "None"


def _format_active_table_profiles(glossary_service: GlossaryService) -> str:
    active_profiles = []

    for table_name, profile in TABLE_PROFILES.items():
        if table_name not in glossary_service.all_tables:
            continue

        active_profiles.append(
            {
                "table_name": table_name,
                "table_type": profile.table_type,
                "grain": profile.grain,
                "grain": profile.grain,
                "best_for": profile.best_for,
                "avoid_for": profile.avoid_for,
            }
        )

    if not active_profiles:
        return "No active table profiles found."

    return json.dumps(active_profiles, ensure_ascii=False, indent=2)


def _format_lookup_backed_fields(glossary_service: GlossaryService, limit: int = 30) -> str:
    rows: List[Dict[str, Any]] = []

    for row in glossary_service.get_lookup_backed_rows()[:limit]:
        rows.append(
            {
                "table_name": row.get("table_name"),
                "field_name": row.get("field_name") or row.get("column_name"),
                "description": row.get("description"),
                "lookup_resolution_scope": row.get("lookup_resolution_scope"),
                "lookup_reference": row.get("lookup_reference"),
                "remarks": row.get("remarks"),
            }
        )

    if not rows:
        return "None"

    return json.dumps(rows, ensure_ascii=False, indent=2)


def _format_top_glossary_hits(glossary_hits: list[dict] | None, limit: int = 8) -> str:
    if not glossary_hits:
        return "None"

    rows: list[dict[str, Any]] = []

    for row in glossary_hits[:limit]:
        rows.append(
            {
                "table_name": row.get("table_name"),
                "field_name": row.get("field_name"),
                "data_type": row.get("data_type"),
                "description": row.get("description"),
                "enum_values": row.get("enum_values") or row.get("sample_values") or [],
                "lookup_resolution_scope": row.get("lookup_resolution_scope"),
                "lookup_reference": row.get("lookup_reference"),
            }
        )

    return json.dumps(rows, ensure_ascii=False, indent=2)


async def generate_smalltalk_answer(
    query: str,
    *,
    chat_history: list[dict] | None = None,
    glossary_hits: list[dict] | None = None,
    glossary_service: GlossaryService,
    intent_reason: str | None = None,
) -> str:
    """
    LLM-based smalltalk/capability responder.

    This replaces the previous keyword-based capability answer path. The answer is
    still grounded in the active table registry and glossary, but the wording and
    level of detail are chosen by the LLM based on the user's actual phrasing.
    """
    prompt = f"""
You are the smalltalk and capability answer agent for a CUSTOMER INTELLIGENCE copilot.

Business context:
{NORTHCO_360_BUSINESS_CONTEXT}

Return JSON:
{{
  "answer": "final user-facing answer"
}}

Your job:
- Answer conversational messages naturally.
- Adapt the answer to the user's actual wording.
- Do not use a fixed template.
- Do not list every table unless the user explicitly asks for detailed scope.
- Do not generate SQL.
- Do not claim you can answer things outside the available Customer 360 serving tables.
Your job:
- Answer conversational messages naturally.
- Adapt the answer to the user's actual wording.
- Do not use a fixed template.
- Do not list every table unless the user explicitly asks for detailed scope.
- Do not generate SQL.
- Do not claim you can answer things outside the available Customer 360 serving tables.
- Always mention, in natural wording, that this copilot can provide insights and drill down to customer × monthly level.
- For greetings, respond briefly and invite a Customer Intelligence question.
- For thanks, respond briefly.
- For "who are you", explain your role naturally.
- For "what can you answer", "what are your capabilities", "what questions can I ask", or similar:
  - Give a concise capability answer.
  - Mention the main categories only.
  - Include a few natural example questions.
  - Mention that answers are grounded in the configured serving tables/glossary.
- If the user asks about unavailable areas such as NBA, campaigns, trigger events, recommendation rules, or non-Customer-Intelligence tables, say that those are not available in the current scope.
- Keep the answer business-friendly and not too verbose.
- Do not mention internal agents, prompts, routing, LangGraph, or implementation details.

Intent reason:
{intent_reason or "None"}

Active table profiles:
{_format_active_table_profiles(glossary_service)}

Lookup-backed fields:
{_format_lookup_backed_fields(glossary_service)}

Top glossary hits for this query:
{_format_top_glossary_hits(glossary_hits)}

Recent chat history:
{_history_text(chat_history)}

User query:
{query}
""".strip()

    result = await get_llm().generate_json(prompt, SmalltalkResponse)
    return result.answer.strip()