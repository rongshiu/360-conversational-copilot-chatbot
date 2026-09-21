# app/agents/intent_agent.py
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.service.llm_service import get_llm
from app.service.business_context import NORTHCO_360_BUSINESS_CONTEXT
from app.core.logging import Logger

logger = Logger.get_logger(__name__)

INTENTS = ("analytics", "glossary", "smalltalk", "clarify")
ROUTES = ("answer_from_previous", "plan_sql", "glossary", "smalltalk", "clarify")


class RawIntentDecision(BaseModel):
    """Permissive shape.

    Models routinely put a route value in `intent` (usually `plan_sql` or
    `answer_from_previous`), so both fields accept the union and the normalization
    layer in detect_intent sorts it out. Rejecting those outright just turns a
    recoverable answer into a failed request.
    """

    intent: Literal[
        "analytics",
        "glossary",
        "smalltalk",
        "clarify",
        "answer_from_previous",
        "plan_sql",
    ]
    route: Literal[
        "answer_from_previous",
        "plan_sql",
        "glossary",
        "smalltalk",
        "clarify",
        "analytics",
    ]
    # Optional so a response truncated inglegate-`reason` can still be salvaged into a
    # valid decision from its intent/route fields.
    reason: str = ""
    is_followup: bool = False
    resolved_term: Optional[str] = Field(default=None)
    entity_phrases: list[str] = Field(default_factory=list)


class IntentDecision(BaseModel):
    intent: Literal["analytics", "glossary", "smalltalk", "clarify"]
    route: Literal["answer_from_previous", "plan_sql", "glossary", "smalltalk", "clarify"]
    reason: str = ""
    is_followup: bool = False
    resolved_term: Optional[str] = Field(default=None)
    # Spans of the question that NAME something -- a store, a product, a customer
    # attribute. Consumed by the entity resolver to bound its fuzzy pass; see
    # EntityResolver.resolve. None means the router could not say, which is not the
    # same as an empty list, and the resolver treats the two differently.
    entity_phrases: Optional[list[str]] = Field(default=None)


def _history_text(chat_history: list[dict] | None, limit: int = 8) -> str:
    lines: list[str] = []
    for item in (chat_history or [])[-limit:]:
        role = item.get("role", "unknown")
        content = item.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "None"


def _format_glossary_hits(glossary_hits: list[dict] | None) -> str:
    if not glossary_hits:
        return "None"

    lines: list[str] = []
    for row in glossary_hits[:8]:
        field_name = row.get("field_name") or row.get("column_name") or ""
        data_type = row.get("data_type", "")
        description = row.get("description", "")
        table_name = row.get("table_name", "")

        if table_name:
            lines.append(f"- `{field_name}` [{table_name}] ({data_type}): {description}")
        else:
            lines.append(f"- `{field_name}` ({data_type}): {description}")

    return "\n".join(lines)


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
                compact = {
                    "type": payload.get("type"),
                    "answer_source": payload.get("answer_source"),
                    "answer": payload.get("answer"),
                    "has_sql": bool(payload.get("sql")),
                    "has_rows": bool(payload.get("rows")),
                    "row_count": len(payload.get("rows") or [])
                    if isinstance(payload.get("rows"), list)
                    else 0,
                    "has_chart": bool(payload.get("chart")),
                    "chart": payload.get("chart") if isinstance(payload.get("chart"), dict) else None,
                    "lookup_plan": payload.get("lookup_plan")
                    if isinstance(payload.get("lookup_plan"), dict)
                    else None,
                }

                analysis = payload.get("analysis")
                if isinstance(analysis, dict):
                    compact["analysis"] = {
                        "chart_type": analysis.get("chart_type"),
                        "chart_title": analysis.get("chart_title"),
                        "finding": analysis.get("finding"),
                        "columns": analysis.get("columns"),
                    }

                return json.dumps(compact, ensure_ascii=False, default=str)
        except Exception:
            return content or "None"

    return "None"


# Static routing rules -- identical for every request, sent once as a cached
# system_instruction. Only per-turn context (payload, glossary hits, history,
# query) goes in the dynamic prompt.
#
# There are four routes. The frontend/dashboard UI-explanation route was removed
# along with the frontend context feature: it competed directly with glossary on
# every "what does X mean" question and needed a stack of override and
# tie-breaker rules to arbitrate. Definition questions now go to glossary and
# data questions to analytics, which is the only distinction that ever mattered.
INTENT_SYSTEM_RULES = f"""
You are the routing agent for NorthCo Customer Intelligence Copilot.

Business context:
{NORTHCO_360_BUSINESS_CONTEXT}

Classify the current user query into exactly one route.

Allowed outputs:
{{
  "intent": "analytics" | "glossary" | "smalltalk" | "clarify",
  "route": "answer_from_previous" | "plan_sql" | "glossary" | "smalltalk" | "clarify",
  "reason": "one short phrase, max 12 words",
  "is_followup": true or false,
  "resolved_term": null or "short business term",
  "entity_phrases": ["exact substrings of the query that NAME a specific thing"]
}}

Routing priority order:
1. analytics, when the user wants real data: numbers, rows, values, trends,
   breakdowns, customers, rankings, comparisons, or anything SQL-backed.
2. glossary, when the user wants the meaning or definition of a metric, field,
   table, column, enum value, or business term.
3. answer_from_previous, only for follow-up interpretation of the immediately
   previous analytics result.
4. smalltalk.
5. clarify.

The core distinction is DEFINITION versus DATA:
- Definition intent -> the user wants to know what something means. glossary.
- Data intent -> the user wants actual values, counts, examples, or rows.
  analytics + plan_sql.
- When a question contains both a known term and a request for values, DATA
  wins. "What product categories are available for NorthCo" is analytics, not
  glossary, even though product_category is a glossary field.
- Never answer a value-retrieval question from glossary text.

analytics + plan_sql:
- Counts, totals, sales, revenue, membership penetration, basket size, customer
  counts, rankings, trends, period comparisons, breakdowns, filters, drilldowns,
  top/bottom, store/product/OpCo/category analysis, daypart analysis.
- Also: examples, sample values, available values, possible values, distinct
  values, unique values, options, or actual members of a field.
- Follow-ups that ask for a different cut of the data -- "break this down by
  OpCo", "show by month", "which stores", "drill into Fashion" -- are analytics
  even when the previous answer was a glossary definition.

analytics + answer_from_previous:
- Only when the user is reacting to the immediately previous analytics answer and
  it can be answered from that result without new SQL.
- Examples after an analytics response: "why do you say so", "explain this
  result", "is that high", "summarize this table".
- If answering would need any value not already in the previous result, use
  plan_sql instead.

glossary + glossary:
- The user wants the meaning, definition, or business interpretation of a metric
  or a schema term: "what does membership penetration mean", "what is
  family_segment", "explain lifecycle stage", "what counts as a Member".
- A glossary answer explains the term. It never retrieves values.
- If the user asks for examples or available values of a field, that is analytics.
- If the user asks for values under a specific OpCo, store, category, period, or
  other business filter, that is analytics.

smalltalk + smalltalk:
- Greetings, thanks, capability questions, conversational turns.
- Product identity questions belong here: "what is n360", "what is NorthCo 360",
  "what does N360 mean", "what is this copilot", "who are you", "what can you
  do". Never route these to clarify -- they are known terms.

clarify + clarify:
- Only when the request is too incomplete to route safely.
- Do not use clarify for a question you can route but cannot fully answer; the
  downstream planner produces its own targeted clarification with better
  context than this router has.
- In particular, a data question you judge unanswerable is still analytics +
  plan_sql. You cannot see the schema, so you cannot tell "needs one more
  detail" from "no table can answer this" -- the planner can, and it decides.
  This holds even when the history shows a previous turn already refused the
  same question: repeat that question to the planner rather than turning it
  into a clarification here.

Follow-up rules:
- Previous answer was analytics with rows/chart/sql, and the user asks about
  "this result / this table / this chart" -> answer_from_previous.
- Previous answer was analytics, and the user asks for a new cut, filter, period,
  or dimension -> plan_sql.
- Previous answer was a glossary definition, and the user now asks for numbers on
  that term -> plan_sql.
- A short reply that answers a pending clarification is handled before this
  router runs, so you will not see those.

entity_phrases:
Copy out the parts of the query that NAME a specific store, product category,
brand, or customer attribute value -- the words a person would look up in a
catalogue. This bounds what the entity resolver is allowed to guess at, so
including a word costs little and omitting one can lose a filter.

- Copy substrings VERBATIM from the query, lowercase, in the order they appear.
  Do not translate, expand, correct spelling, or invent a canonical name.
- INCLUDE proper names and value words: "inglegate juniperford", "veldra selbycross", "grocery",
  "home fashion", "gen z", "elite", "3 star", "northvalu", "northco mart".
- EXCLUDE everything the question is DOING rather than naming: verbs ("hold",
  "linked", "moved", "buying", "shopped"), metric and dimension words
  ("penetration", "basket size", "revenue", "brands", "category", "segment",
  "region"), time expressions ("last quarter", "june 2026", "ytd"), and
  comparison words ("top 5", "best", "versus").
- A word that is only doing grammatical work is never an entity phrase, even when
  it happens to look like a product name.
- Return [] when the question names nothing specific -- that is a real answer, not
  a failure.

Tie-breakers:
- Actual numbers, rows, examples, or distinct values -> analytics.
- Meaning of a metric or schema term -> glossary.
- "How is X calculated" -> glossary, because it is asking for a definition.
- "What is X for period P" or "X by dimension D" -> analytics.
""".strip()


async def detect_intent(
    query: str,
    chat_history: list[dict] | None = None,
    glossary_hits: list[dict] | None = None,
) -> IntentDecision:
    prompt = f"""
Last assistant payload:
{_last_assistant_payload(chat_history)}

Top glossary candidates:
{_format_glossary_hits(glossary_hits)}

Recent chat history:
{_history_text(chat_history)}

Current user query:
{query}
""".strip()

    try:
        raw = await get_llm().generate_json(
            prompt,
            RawIntentDecision,
            system_instruction=INTENT_SYSTEM_RULES,
            # 512 was too small: a verbose `reason` truncated the JSON inglegate-string
            # and the whole request failed. Give the response room to close.
            max_output_tokens=1024,
        )
    except Exception as exc:
        # Never let routing crash a request. Default to the most common route;
        # downstream nodes degrade gracefully into a clarification or an
        # unsupported answer if it turns out to be wrong.
        logger.warning("Intent classification failed; defaulting to analytics: %s", exc)
        # entity_phrases stays None, not [], so the resolver falls back to its own
        # judgement rather than reading a failed call as "this question names
        # nothing" and silently dropping every fuzzy match.
        return IntentDecision(
            intent="analytics",
            route="plan_sql",
            reason="Intent classification was unavailable; treated as an analytics question.",
            is_followup=False,
            resolved_term=None,
            entity_phrases=None,
        )

    intent = raw.intent
    route = raw.route

    # Normalization: `answer_from_previous` and `plan_sql` are routes, not final
    # intents, but models put them in `intent` often enough to matter.
    if intent == "answer_from_previous":
        intent, route = "analytics", "answer_from_previous"
    elif intent == "plan_sql":
        intent, route = "analytics", "plan_sql"

    # ...and `analytics` sometimes lands in `route`.
    if route == "analytics":
        route = "plan_sql"

    # Keep intent aligned with route for the non-analytics terminals.
    if route in {"glossary", "smalltalk", "clarify"}:
        intent = route

    return IntentDecision(
        intent=intent,
        route=route,
        reason=raw.reason,
        is_followup=raw.is_followup,
        resolved_term=raw.resolved_term,
        entity_phrases=[p for p in (raw.entity_phrases or []) if str(p).strip()],
    )
