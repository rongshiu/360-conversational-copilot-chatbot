# app/agents/answer_from_previous_agent.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.service.currency import CURRENCY_RULE
from app.service.llm_service import get_llm


class PreviousAnswerResponse(BaseModel):
    answer: str = Field(..., min_length=1)
    should_replan: bool = False
    replan_reason: Optional[str] = None

    chart_type: Literal["text", "table", "bar_chart", "line_chart", "donut_chart"] = "text"
    chart_title: Optional[str] = None

    x_field: Optional[str] = None
    x_axis: Optional[str] = None
    y_axis: Optional[str] = None
    label_field: Optional[str] = None
    value_field: Optional[str] = None

    columns: Optional[List[str]] = None
    series: Optional[List[Dict[str, str]]] = None
    chart_data: Optional[List[dict]] = None


def _history_text(chat_history: list[dict] | None, limit: int = 8) -> str:
    lines: list[str] = []
    for item in (chat_history or [])[-limit:]:
        role = item.get("role", "unknown")
        content = item.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "None"


def _payload_text(payload: dict | None) -> str:
    if not payload:
        return "None"
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        return str(payload)


async def answer_from_previous(
    query: str,
    *,
    previous_payload: Dict[str, Any] | None = None,
    chat_history: list[dict] | None = None,
) -> PreviousAnswerResponse:
    prompt = f"""
You are the follow-up answer agent for a CUSTOMER INTELLIGENCE copilot.

Return JSON:
{{
  "answer": "final user-facing answer",
  "should_replan": true or false,
  "replan_reason": "short reason or null",
  "chart_type": "text" | "table" | "bar_chart" | "line_chart" | "donut_chart",
  "chart_title": "optional title or null",
  "x_field": "exact x-axis data key or null",
  "x_axis": "x-axis display label or null",
  "y_axis": "y-axis display label or null",
  "label_field": "label field or null",
  "value_field": "value field or null",
  "columns": ["col1", "col2"],
  "series": [{{"id": "metric_name", "label": "Metric Name"}}],
  "chart_data": []
}}

Your task:
- Answer the user's latest message using the immediately previous assistant payload and recent history.
- Do NOT invent facts not grounded in the previous payload.
- If the user is asking for explanation, interpretation, justification, or reaction to the previous analytics result, answer directly.
- If the user asks "why", explain the reasoning using the previous result only.
- If the user asks "how", explain the calculation, filter, grouping, ranking, or SQL logic used.
- If the user asks what to do next, suggest the most useful next analysis.
- If the user asks a subjective question like "is this a lot?", "is this high?", "is this good?", or "is this bad?" and no benchmark exists in the previous payload, say a benchmark is needed and suggest the right comparison.
- If the user is actually asking for a new comparison, new benchmark, new breakdown, new aggregation, new time slice, or any fresh calculation, set:
  - should_replan = true
  - replan_reason = short reason.
- Be concise and natural.
- Do not use fixed section headings.
- Do not write like a templated report.
- When useful, naturally explain what the result says, how it was derived, why it matters, and what the next useful analysis would be.
- Do not mention internal routing, payloads, prompts, or implementation details.
- If the previous payload includes SQL or rows and they help explain the answer, you may refer to them briefly in natural language.
- Do not ask a clarification question unless absolutely necessary. Prefer answering from the available context when possible.

{CURRENCY_RULE}

Chart rules:
- Choose "table" when the previous payload already contains structured rows and the result is best shown as a listing or record set.
- Choose "bar_chart" when comparing categories/items is clearer visually than in text.
- Choose "line_chart" only when the previous payload clearly represents a trend, ordered progression, or when the user explicitly asks for a line chart.
- Choose "donut_chart" only for proportional category breakdowns.
- Choose "text" when no visual adds value.
- Only use the previous payload rows as the source of truth.
- Do not invent chart points that are not supported by the previous payload.
- For bar_chart and donut_chart:
  - set label_field to the categorical field name from the previous rows
  - set value_field to the numeric field name from the previous rows
  - chart_data may be empty if the previous rows already contain the needed fields.
- For line_chart:
  - x_field must be the exact field name from the previous rows or chart_data used for x values
  - series ids must match the metric field names in chart_data
  - x_axis and y_axis are display labels only.
- For line_chart, if you can reuse the previous rows directly, return chart_data using those same keys.
- If there are no suitable structured rows for a chart, choose "text".
- Return [] instead of null for list-like fields when possible.

Recent chat history:
{_history_text(chat_history)}

Previous assistant payload:
{_payload_text(previous_payload)}

Current user query:
{query}
""".strip()

    response = await get_llm().generate_json(prompt, PreviousAnswerResponse)
    response.columns = response.columns or []
    response.series = response.series or []
    response.chart_data = response.chart_data or []
    return response