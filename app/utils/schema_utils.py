from __future__ import annotations

from app.graph.copilot_state import CopilotState


def generic_fallback_clarification(state: CopilotState) -> str:
    glossary_hits = state.get("glossary_hits") or []
    if glossary_hits:
        field_names: list[str] = []
        seen_fields: set[str] = set()
        for row in glossary_hits:
            name = str(row.get("field_name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen_fields:
                continue
            seen_fields.add(key)
            field_names.append(name)
        if field_names:
            joined = ", ".join(f"`{name}`" for name in field_names[:4])
            return f"I found a few possible fields: {joined}. Which one do you want?"

    return (
        "I need one more detail to answer that. "
        "Please specify the exact metric, field, grouping, filter, or time period you want."
    )


def schema_match_confidence(query: str, glossary_hits: list[dict] | None) -> str:
    q = (query or "").strip().lower()
    hits = glossary_hits or []

    if not q or not hits:
        return "none"

    normalized_query = "".join(ch for ch in q if ch.isalnum() or ch.isspace()).strip()
    query_tokens = {tok for tok in normalized_query.split() if tok}
    if not query_tokens:
        return "none"

    top_field = str(hits[0].get("field_name", "")).lower()
    top_desc = str(hits[0].get("description", "")).lower()
    top_text = f"{top_field} {top_desc}"

    overlap = sum(1 for tok in query_tokens if tok in top_text)

    if overlap >= 3:
        return "high"
    if overlap >= 2:
        return "medium"
    if overlap >= 1:
        return "low"
    return "low"
