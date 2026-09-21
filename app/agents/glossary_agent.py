from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.service.currency import CURRENCY_RULE
from app.service.llm_service import get_llm
from app.service.business_context import NORTHCO_360_BUSINESS_CONTEXT


class GlossaryExplanation(BaseModel):
    answer: str = Field(..., min_length=1)


def _format_matches(matches: List[Dict[str, Any]] | None, limit: int = 8) -> str:
    if not matches:
        return "None"

    lines: list[str] = []
    for item in matches[:limit]:
        table_name = item.get("table_name") or ""
        field_name = item.get("field_name") or ""
        data_type = item.get("data_type") or ""
        description = item.get("description") or ""

        if table_name:
            lines.append(f"- `{field_name}` [{table_name}] ({data_type}): {description}")
        else:
            lines.append(f"- `{field_name}` ({data_type}): {description}")

    return "\n".join(lines)


async def explain_glossary_term(
    query: str,
    *,
    matches: List[Dict[str, Any]] | None = None,
    glossary_context: str = "",
    intent_reason: str | None = None,
) -> str:
    prompt = f"""
You are the glossary explanation agent for a CUSTOMER INTELLIGENCE copilot.

Business context:
{NORTHCO_360_BUSINESS_CONTEXT}

Return JSON:
{{
  "answer": "final user-facing glossary explanation"
}}

Task:
- Explain the user's requested business term naturally.
- Do not simply list matched columns unless the user asks for fields or columns.
- If the term is a broad business concept, explain it as a concept using the available customer intelligence context.
- If the exact term is not a field in the glossary, say it is not a single physical column, then explain what it most likely refers to in this copilot.
- Use the matched glossary fields only as supporting context.
- Do not claim a formal enterprise definition unless it is explicitly present in the glossary context.
- Be concise and business-friendly.

{CURRENCY_RULE}

Important examples:
- If the user asks "what is N360", "what does N360 mean", "what is NORTHCO360", or "what does NorthCo 360 mean", explain it as NorthCo 360: the customer 360 / unified customer intelligence view across NorthCo OpCos, customer profile, membership, lifecycle, value segment, revenue, product/category, store, payment, and monthly activity signals.
- Do not answer "NorthCo 360" by listing `northco_bank_revenue`, `northco_mart_revenue`, etc.
- If the user asks "what does family_segment mean?", explain the field directly.
- If the user asks "what fields are related to NorthCo?", then it is okay to list related fields.

Intent reason:
{intent_reason or "None"}

Top glossary matches:
{_format_matches(matches)}

Relevant schema context:
{glossary_context or "None"}

User query:
{query}
""".strip()

    result = await get_llm().generate_json(prompt, GlossaryExplanation)
    return result.answer.strip()