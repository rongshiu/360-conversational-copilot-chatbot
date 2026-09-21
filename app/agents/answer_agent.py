# app/agents/answer_agent.py
from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd

from app.models.responses import ChartDatum, ChartSeries, ChartSpec, GlossaryMatch
from app.core import settings
from app.service.currency import currency_columns
from app.utils.common import sanitize_for_json


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None

    if isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return None
        return num

    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned or cleaned.upper() == "NULL":
            return None
        try:
            num = float(cleaned)
            if math.isnan(num) or math.isinf(num):
                return None
            return num
        except ValueError:
            return None

    return None


def _clean_row_value(value: Any) -> Any:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None

    return value


def _clean_row_dicts(row_dicts: List[dict]) -> List[dict]:
    cleaned: list[dict] = []
    for row in row_dicts:
        cleaned.append({key: _clean_row_value(value) for key, value in row.items()})
    return cleaned


def build_clarify_response(answer: str, reason: str | None = None) -> Dict[str, Any]:
    return {
        "type": "clarify",
        "answer": answer,
        "chart": None,
        "rows": [],
        "sql": None,
        "glossary_matches": [],
        "intent_reason": reason,
    }


def build_unsupported_response(answer: str, reason: str | None = None) -> Dict[str, Any]:
    return {
        "type": "unsupported",
        "answer": answer,
        "chart": None,
        "rows": [],
        "sql": None,
        "glossary_matches": [],
        "intent_reason": reason,
    }


def build_error_response(answer: str, sql: str | None = None, reason: str | None = None) -> Dict[str, Any]:
    return {
        "type": "error",
        "answer": answer,
        "chart": None,
        "rows": [],
        "sql": sql,
        "glossary_matches": [],
        "intent_reason": reason,
    }


def build_glossary_response(matches: List[Dict[str, Any]], reason: str | None = None) -> Dict[str, Any]:
    if not matches:
        return build_clarify_response(
            "I'm not sure which term you mean. Try naming the exact field you're after, or ask me an analytics question instead.",
            reason=reason,
        )

    answer_parts: list[str] = []
    payload_matches: list[dict] = []

    for item in matches[:5]:
        samples = ", ".join(item.get("sample_values", [])[:3])
        answer_parts.append(
            f"{item['field_name']}: {item.get('description') or 'No description available.'}"
            + (f" Sample values: {samples}." if samples else "")
        )
        payload_matches.append(
            GlossaryMatch(
                field_name=item["field_name"],
                description=item.get("description") or "",
                data_type=item.get("data_type") or None,
                sample_values=item.get("sample_values", []),
            ).model_dump()
        )

    return {
        "type": "glossary",
        "answer": "\n\n".join(answer_parts),
        "glossary_matches": payload_matches,
        "chart": None,
        "rows": [],
        "sql": None,
        "intent_reason": reason,
    }


def _looks_like_percentages(values: List[float]) -> bool:
    """
    Heuristic: the source values are already percentages (not absolute counts)
    when every value is within 0-100 and they sum to roughly 100.
    """
    if not values or any(v < 0 or v > 100 for v in values):
        return False

    total = sum(values)
    return 95.0 <= total <= 105.0


def _build_bar_or_donut_points(
    data: List[dict], limit: int = 20
) -> tuple[List[dict], str]:
    """
    Normalize chart points and infer a value format.

    Returns (points, value_format). Each point always carries `percentage`
    (share of the total across all points) so the frontend can size slices
    consistently regardless of whether `value` is a count or a percentage.
    """
    raw: list[tuple[str, float]] = []
    for row in data[:limit]:
        label = str(row.get("label", "")).strip()
        value = _safe_float(row.get("value"))

        if not label or value is None:
            continue

        raw.append((label, value))

    if not raw:
        return [], "number"

    values = [value for _, value in raw]
    already_percentage = _looks_like_percentages(values)
    total = sum(values)

    points: list[dict] = []
    for label, value in raw:
        if already_percentage:
            share = value
        elif total > 0:
            share = round(value / total * 100.0, 2)
        else:
            share = None

        points.append(
            ChartDatum(label=label, value=value, percentage=share).model_dump()
        )

    value_format = "percentage" if already_percentage else "count"
    return points, value_format


def _clean_line_chart_data(data: List[dict], limit: int = 50) -> List[dict]:
    cleaned: list[dict] = []
    for row in data[:limit]:
        if not isinstance(row, dict):
            continue
        cleaned.append({key: _clean_row_value(value) for key, value in row.items()})
    return cleaned


def _clean_line_chart_series(
    data: List[dict],
    series: List[Dict[str, str]] | None,
    x_field: str | None,
) -> tuple[str | None, list[dict]]:
    cleaned_data = _clean_line_chart_data(data)
    if not cleaned_data:
        return x_field, []

    candidate_x = (x_field or "").strip() or None
    if candidate_x and all(candidate_x in row for row in cleaned_data):
        return candidate_x, cleaned_data

    series_ids = {item["id"] for item in (series or []) if item.get("id")}

    first_row_keys = list(cleaned_data[0].keys())
    for key in first_row_keys:
        if key not in series_ids:
            return key, cleaned_data

    return candidate_x, cleaned_data


def _chart_currency(
    sql: str | None,
    analysis_result: dict,
    columns: List[str],
) -> Optional[str]:
    """The currency code for this chart, or None if nothing charted is money.

    Checks the columns the chart actually plots, not every column in the result: a
    revenue-by-category bar chart is currency, but a transaction-count chart that
    merely sits next to a revenue column is not.

    y_axis and value_field are both consulted because either may hold the original
    result column -- the chart builder renames the plotted measure to the generic
    "value", and the analysis agent is asked for a column name but sometimes returns
    a display label instead ("Total Sales (RM)").

    Names that match no money column are ignored, which is what makes a display
    label harmless. The cost is a false negative when *every* charted name is a
    label; that is the right way to be wrong. An unmarked amount is formatted as a
    plain number, while a wrongly marked count is a wrong unit on a right number --
    and a wrong unit reads exactly like a correct answer.
    """
    charted = [
        analysis_result.get("y_axis"),
        analysis_result.get("value_field"),
        *[item.get("id") for item in (analysis_result.get("series") or []) if isinstance(item, dict)],
    ]
    if (analysis_result.get("chart_type") or "text") == "table":
        charted.extend(columns)

    candidates = [str(name) for name in charted if name]
    if not candidates:
        return None

    return settings.currency_code if currency_columns(sql, candidates) else None


def _make_chart(
    chart_type: str,
    title: Optional[str],
    data: List[dict],
    *,
    x_field: Optional[str] = None,
    x_axis: Optional[str] = None,
    y_axis: Optional[str] = None,
    label_field: Optional[str] = None,
    value_field: Optional[str] = None,
    columns: Optional[List[str]] = None,
    series: Optional[List[Dict[str, str]]] = None,
    table_rows: Optional[List[dict]] = None,
    currency: Optional[str] = None,
) -> Optional[dict]:
    if chart_type == "table":
        spec = ChartSpec(
            chart_type="table",
            title=title,
            columns=columns or [],
            currency=currency,
            data=table_rows or data or [],
        )
        return sanitize_for_json(spec.model_dump())

    if chart_type == "bar_chart":
        points, value_format = _build_bar_or_donut_points(data)
        if not points:
            return None

        # A declared unit beats an inferred one: _build_bar_or_donut_points guesses
        # the format from the values, and money that happens to fall in 0-100 looks
        # exactly like a percentage.
        if currency:
            value_format = "currency"

        spec = ChartSpec(
            chart_type="bar_chart",
            title=title,
            x_axis=x_axis,
            y_axis=y_axis,
            label_field=label_field or "label",
            value_field=value_field or "value",
            value_format=value_format,
            currency=currency,
            data=points,
        )
        return sanitize_for_json(spec.model_dump())

    if chart_type == "donut_chart":
        points, value_format = _build_bar_or_donut_points(data)
        if not points:
            return None

        if currency:
            value_format = "currency"

        spec = ChartSpec(
            chart_type="donut_chart",
            title=title,
            label_field=label_field or "label",
            value_field=value_field or "value",
            value_format=value_format,
            currency=currency,
            data=points,
        )
        return sanitize_for_json(spec.model_dump())

    if chart_type == "line_chart":
        line_series = [
            ChartSeries(id=item["id"], label=item["label"])
            for item in (series or [])
            if item.get("id") and item.get("label")
        ]
        resolved_x_field, cleaned_data = _clean_line_chart_series(data, series or [], x_field)
        if not cleaned_data or not resolved_x_field or not line_series:
            return None

        spec = ChartSpec(
            chart_type="line_chart",
            title=title,
            x_field=resolved_x_field,
            x_axis=x_axis,
            y_axis=y_axis,
            value_format="currency" if currency else "number",
            currency=currency,
            series=line_series,
            data=cleaned_data,
        )
        return sanitize_for_json(spec.model_dump())

    return None


def _looks_like_empty_result(raw_text: str) -> bool:
    low = (raw_text or "").strip().lower()
    if not low:
        return True

    markers = [
        "no rows",
        "0 rows",
        "returned no rows",
        "empty result",
        "no results",
        "[]",
    ]
    return any(marker in low for marker in markers)

def _extract_text_filter_value(sql: str, column_name: str) -> Optional[str]:
    """
    Extract text filter values from common generated SQL patterns.

    Supports:
      LOWER(TRIM(CAST(t.store_name AS TEXT))) = LOWER(TRIM('NorthCo X'))
      t.store_name = 'NorthCo X'
    """
    if not sql:
        return None

    normalized_column = re.escape(column_name)

    cast_pattern = re.compile(
        rf"""
        \b{normalized_column}\b
        \s+AS\s+TEXT
        \s*\)\s*\)\s*\)
        \s*=\s*
        LOWER\s*\(\s*TRIM\s*\(\s*'(?P<value>[^']+)'\s*\)\s*\)
        """,
        re.IGNORECASE | re.VERBOSE | re.DOTALL,
    )
    match = cast_pattern.search(sql)
    if match:
        return match.group("value").strip()

    simple_pattern = re.compile(
        rf"""
        \b{normalized_column}\b
        \s*=\s*
        '(?P<value>[^']+)'
        """,
        re.IGNORECASE | re.VERBOSE | re.DOTALL,
    )
    match = simple_pattern.search(sql)
    if match:
        return match.group("value").strip()

    return None


def _empty_result_filter_summary(sql: str | None) -> str:
    """
    Builds a user-facing summary of the exact filters used by the SQL.

    This is best-effort only. If a filter cannot be extracted, it is simply omitted.
    """
    sql = sql or ""

    store_name = (
        _extract_text_filter_value(sql, "store_name")
        or _extract_text_filter_value(sql, "primary_store_name")
    )
    opco_name = _extract_text_filter_value(sql, "opco_name")

    product_category = _extract_text_filter_value(sql, "product_category")
    # v3 filters on category KEYS, not denormalised level text. The resolved
    # entity display name is carried in lookup_context, so the readable label comes
    # from there rather than being scraped out of the SQL.
    category_name = _extract_text_filter_value(sql, "category_name")
    daypart = _extract_text_filter_value(sql, "daypart")

    payment_type = _extract_text_filter_value(sql, "payment_type")

    membership_tier = _extract_text_filter_value(sql, "membership_tier")
    family_segment = _extract_text_filter_value(sql, "family_segment")
    lifecycle_stage = _extract_text_filter_value(sql, "lifecycle_stage")
    repeat_purchase_segment = _extract_text_filter_value(sql, "repeat_purchase_segment")
    customer_status = _extract_text_filter_value(sql, "customer_status")

    calendar_date = _extract_text_filter_value(sql, "calendar_date")
    month_start_date = _extract_text_filter_value(sql, "month_start_date")

    parts: list[str] = []

    if store_name:
        parts.append(f"store `{store_name}`")
    if opco_name:
        parts.append(f"opco `{opco_name}`")

    if product_category:
        parts.append(f"product category `{product_category}`")
    if category_name:
        parts.append(f"category `{category_name}`")
    if daypart:
        parts.append(f"daypart `{daypart}`")

    if payment_type:
        parts.append(f"payment type `{payment_type}`")


    if membership_tier:
        parts.append(f"membership tier `{membership_tier}`")
    if family_segment:
        parts.append(f"family segment `{family_segment}`")
    if lifecycle_stage:
        parts.append(f"lifecycle stage `{lifecycle_stage}`")
    if repeat_purchase_segment:
        parts.append(f"repeat purchase segment `{repeat_purchase_segment}`")
    if customer_status:
        parts.append(f"customer status `{customer_status}`")

    if month_start_date:
        parts.append(f"month `{month_start_date}`")
    elif calendar_date:
        parts.append(f"period `{calendar_date}`")

    return ", ".join(parts)


def _fallback_empty_answer(sql: str | None = None) -> str:
    filter_summary = _empty_result_filter_summary(sql)

    if filter_summary:
        return (
            f"I didn't find any data matching {filter_summary}. "
            "There just aren't any records for that specific combination in the current data. "
            "Try widening the date range, removing one of the filters, or picking a different value — "
            "for example a nearby period or another store, category, or segment."
        )

    return (
        "I didn't find any data for that request — there aren't any records matching those filters in the current data. "
        "Try widening the date range or removing one of the filters (like the period, store, category, tier, or segment), "
        "and I'll take another look."
    )


def _detect_preview_note(row_dicts: list[dict]) -> str:
    if not row_dicts:
        return ""

    first = row_dicts[0]
    # v2 detected a customer preview list by looking for customer_id +
    # total_customer_count. Neither exists now -- customer identity is not
    # projectable at all -- so this shape can never occur.
    if True:
        return ""

    total = _safe_float(first.get("total_customer_count"))
    if total is None:
        return ""

    shown = len(row_dicts)
    total_int = int(total)

    if total_int > shown:
        return f"Showing {shown} preview rows out of {total_int} matching customers."

    return f"Showing all {shown} matching customers."


def _compose_answer_from_analysis(
    analysis_result: Dict[str, Any],
    *,
    row_dicts: list[dict] | None = None,
) -> str:
    row_dicts = row_dicts or []
    preview_note = _detect_preview_note(row_dicts)

    natural_answer = (analysis_result.get("natural_answer") or "").strip()
    if natural_answer:
        if preview_note and preview_note not in natural_answer:
            return f"{natural_answer} {preview_note}"
        return natural_answer

    finding = (analysis_result.get("finding") or "").strip()
    calculation_logic = analysis_result.get("calculation_logic") or []
    interpretation = analysis_result.get("interpretation") or []
    next_step = analysis_result.get("next_step") or []
    limitations = analysis_result.get("limitations") or []

    parts: list[str] = []

    if finding:
        parts.append(finding)

    if calculation_logic:
        logic_text = " ".join(str(item).strip() for item in calculation_logic if str(item).strip())
        if logic_text:
            parts.append(f"I derived this by {logic_text[0].lower() + logic_text[1:] if len(logic_text) > 1 else logic_text}")

    if interpretation:
        interpretation_text = " ".join(str(item).strip() for item in interpretation if str(item).strip())
        if interpretation_text:
            parts.append(interpretation_text)

    if next_step:
        next_text = " ".join(str(item).strip() for item in next_step if str(item).strip())
        if next_text:
            parts.append(f"The next useful step is to {next_text[0].lower() + next_text[1:] if len(next_text) > 1 else next_text}")

    if limitations:
        limitation_text = " ".join(str(item).strip() for item in limitations if str(item).strip())
        if limitation_text:
            parts.append(f"One thing to watch out for: {limitation_text}")

    if preview_note and preview_note not in " ".join(parts):
        parts.append(preview_note)

    if parts:
        return " ".join(parts)

    if row_dicts:
        if preview_note:
            return f"I found matching customer records and returned a limited detail preview. {preview_note}"

        if len(row_dicts) == 1:
            first = row_dicts[0]
            metric_parts = [f"{key} is {value}" for key, value in first.items()]
            return "The result is " + ", ".join(metric_parts) + "."

        return (
            f"The query returned {len(row_dicts)} rows. "
            "Ask for a breakdown, ranking, comparison, trend, or customer profile if you want deeper analysis."
        )

    return (
        "I found some data, but not in a shape I can summarize clearly. "
        "Try asking for a ranking, breakdown, comparison, trend, or customer drilldown."
    )


async def build_analytics_response(
    query: str,
    sql: str,
    rows_payload: Dict[str, Any],
    *,
    reason: str | None = None,
    analysis_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    columns = rows_payload.get("columns") or []
    rows = rows_payload.get("rows") or []
    raw_text = (rows_payload.get("raw_text") or "").strip()
    error = rows_payload.get("error")
    preview_limit = settings.max_result_preview_rows

    if error:
        return build_error_response(
            answer=(
                "Something went wrong while running that request, so I couldn't get an answer this time. "
                "Please try again, and if it keeps happening, try rephrasing or simplifying the question."
            ),
            sql=sql,
            reason=" | ".join(part for part in [reason, f"Execution error: {error}"] if part),
        )

    if not rows:
        if _looks_like_empty_result(raw_text):
            return {
                "type": "analytics",
                "answer": _fallback_empty_answer(sql),
                "chart": None,
                "rows": [],
                "sql": sql,
                "glossary_matches": [],
                "intent_reason": reason,
                "analysis": analysis_result or {},
            }

        return build_error_response(
            answer=(
                "I ran your request but had trouble reading the result, so I can't show it right now. "
                "Please try again in a moment, or rephrase the question."
            ),
            sql=sql,
            reason=" | ".join(
                part
                for part in [reason, "Unrecognized result format from data service."]
                if part
            ),
        )

    if all(isinstance(row, dict) for row in rows):
        row_dicts = _clean_row_dicts(rows[:preview_limit])
    else:
        df = pd.DataFrame(rows, columns=columns)
        row_dicts = df.head(preview_limit).to_dict(orient="records")
        row_dicts = _clean_row_dicts(row_dicts)

    analysis_result = analysis_result or {}
    answer = _compose_answer_from_analysis(analysis_result, row_dicts=row_dicts)

    chart_type = analysis_result.get("chart_type") or "text"

    if chart_type == "table" and not analysis_result.get("columns"):
        analysis_result["columns"] = columns or (list(row_dicts[0].keys()) if row_dicts else [])

    chart = _make_chart(
        chart_type,
        analysis_result.get("chart_title"),
        analysis_result.get("chart_data") or [],
        x_field=analysis_result.get("x_field"),
        x_axis=analysis_result.get("x_axis"),
        y_axis=analysis_result.get("y_axis"),
        label_field=analysis_result.get("label_field"),
        value_field=analysis_result.get("value_field"),
        columns=analysis_result.get("columns") or columns or (list(row_dicts[0].keys()) if row_dicts else []),
        series=analysis_result.get("series") or [],
        table_rows=row_dicts,
        currency=_chart_currency(
            sql,
            analysis_result,
            analysis_result.get("columns") or columns or (list(row_dicts[0].keys()) if row_dicts else []),
        ),
    )

    response = {
        "type": "analytics",
        "answer": answer,
        "chart": chart,
        "rows": row_dicts,
        "sql": sql,
        "glossary_matches": [],
        "intent_reason": reason,
        "analysis": analysis_result,
    }

    return sanitize_for_json(response)


