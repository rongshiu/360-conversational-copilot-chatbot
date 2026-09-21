# app/agents/analysis_agent.py
from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from app.core import settings
from app.service.currency import (
    CURRENCY_RULE,
    amount_formatter,
    currency_columns,
    currency_note,
    format_amount,
)
from app.service.llm_service import get_llm
from app.service.stream_context import get_stream_writer
from app.utils.common import sanitize_for_json


# Static analysis rules — identical for every request, sent once as a cached
# system_instruction. Only the per-turn data (reasons, SQL, rows, query) goes in
# the dynamic prompt.
ANALYSIS_SYSTEM_RULES = """
You are the analysis agent for a CUSTOMER INTELLIGENCE copilot.

Your job:
- Interpret SQL result rows into a natural analytical answer.
- Do not generate SQL.
- Do not invent values, causes, or business facts not present in the rows.
- Be more analytical than a plain restatement: explain the main finding, calculation logic, interpretation, and next useful drilldown.
- Keep the answer natural, not a rigid report with headings.
- If the result is a ranking or breakdown, identify the leader and concentration/share if the rows support it.
- If the result is a trend, explain direction and movement.
- If the result is a detail preview, clearly say preview, but detail previews are usually handled by deterministic fallback.
- A null is an aggregate over no matching rows. Say there were no records for
  those filters. Never call it zero, and never claim a value was withheld or that
  a threshold applied -- nothing withholds values.
- If the resolved display context says a figure is a roll-up to a broader category,
  name that broader category in the answer. Never attribute a roll-up to the
  narrower category the user asked about -- it overstates it.

Return JSON matching this schema exactly:
{
  "natural_answer": "natural user-facing answer, 1 to 3 concise paragraphs, no markdown table",
  "finding": "main finding",
  "calculation_logic": ["how it was calculated"],
  "interpretation": ["why it matters"],
  "next_step": ["best next analysis"],
  "limitations": ["caveats"],
  "chart_type": "text | table | bar_chart | line_chart | donut_chart",
  "chart_title": "title or null",
  "x_field": "field or null",
  "x_axis": "label or null",
  "y_axis": "label or null",
  "label_field": "field or null",
  "value_field": "field or null",
  "columns": ["columns for table"],
  "series": [{"id": "metric_field", "label": "Metric Label"}],
  "chart_data": []
}

Chart rules:
- text: one scalar answer or no useful chart.
- table: detail rows or mixed fields.
- bar_chart: category ranking/breakdown. chart_data must be [{"label": category, "value": number}].
- donut_chart: small composition/share result. chart_data must be [{"label": category, "value": number}].
- line_chart: a time series over calendar_date or month_start_date. Use x_field and series.

Important:
- If the deterministic fallback is already good, you may reuse and improve it.
- Never say a LIMIT preview is the full population.
- A null is an aggregate over no matching rows: say there were no records for
  those filters. Never call it zero, and never mention suppression or thresholds.
- If the resolved display context says a figure is a roll-up to a broader category,
  name that broader category. Never attribute a roll-up to the narrower category
  the user asked about.
- Never expose SQL unless the user asked for it.
- Do not mention unsupported root causes like frontend, backend, or MCP unless the rows show that.
- If resolved display context is provided, use those business labels when naming
  filters. Do not repeat internal keys such as store_id/category_key, even if they
  appear in SQL or planner notes.

""".strip() + "\n" + CURRENCY_RULE


ANALYSIS_STREAM_SYSTEM_RULES = """
You are the final answer writer for a CUSTOMER INTELLIGENCE copilot.

Write the user-facing answer only.

Rules:
- Stream a natural analytical answer.
- Use only the SQL result rows and deterministic fallback provided.
- Do not generate SQL.
- Do not invent values, causes, or business facts not present in the rows.
- Be more analytical than a plain restatement.
- Explain the main finding, how it was calculated, why it matters, and the best next drilldown.
- Keep it concise: 1 to 3 short paragraphs.
- Do not return JSON.
- Do not use markdown tables.
- Never expose SQL unless the user explicitly asked for it.
- Never say a LIMIT preview is the full population.
- This copilot can answer insights and drill down to customer × monthly level.
- If resolved display context is provided, use those business labels when naming
  filters. Do not repeat internal keys such as store_id/category_key.

""".strip() + "\n" + CURRENCY_RULE


class AnalysisResult(BaseModel):
    natural_answer: str = Field(default="")
    finding: str = Field(default="")
    calculation_logic: List[str] = Field(default_factory=list)
    interpretation: List[str] = Field(default_factory=list)
    next_step: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)

    chart_type: Literal["text", "table", "bar_chart", "line_chart", "donut_chart"] = "text"
    chart_title: Optional[str] = None
    x_field: Optional[str] = None
    x_axis: Optional[str] = None
    y_axis: Optional[str] = None
    label_field: Optional[str] = None
    value_field: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    series: List[Dict[str, str]] = Field(default_factory=list)
    chart_data: List[dict] = Field(default_factory=list)


def _clean_value(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    return value


def _rows_as_dicts(rows_payload: Dict[str, Any], limit: int | None = None) -> list[dict]:
    columns = rows_payload.get("columns") or []
    rows = rows_payload.get("rows") or []

    if not rows:
        return []

    resolved_limit = limit or getattr(settings, "max_analysis_rows", 120)

    if all(isinstance(row, dict) for row in rows):
        return [
            {key: _clean_value(value) for key, value in row.items()}
            for row in rows[:resolved_limit]
        ]

    if not columns:
        return []

    df = pd.DataFrame(rows, columns=columns)
    records = df.head(resolved_limit).to_dict(orient="records")

    return [
        {key: _clean_value(value) for key, value in row.items()}
        for row in records
    ]


def _safe_number(value: Any) -> float | None:
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
        except Exception:
            return None

    return None


def _is_customer_detail_preview(rows: list[dict]) -> bool:
    if not rows:
        return False

    first = rows[0]
    # v2 detected a truncated customer preview list. v3 cannot produce one:
    # customer identity is never projectable, so there is no preview to flag.
    return False


def _preview_note(rows: list[dict]) -> str:
    if not _is_customer_detail_preview(rows):
        return ""

    total = rows[0].get("total_customer_count")
    total_num = _safe_number(total)
    if total_num is None:
        return ""

    total_int = int(total_num)
    shown = len(rows)

    if total_int > shown:
        return f"Showing {shown} preview rows out of {total_int} matching customers."

    return f"Showing all {shown} matching customers."


def _pick_numeric_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []

    cols = list(rows[0].keys())
    numeric_cols: list[str] = []

    for col in cols:
        values = [_safe_number(row.get(col)) for row in rows[:20]]
        usable = [value for value in values if value is not None]
        if usable:
            numeric_cols.append(col)

    return numeric_cols


def _pick_categorical_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []

    numeric_cols = set(_pick_numeric_columns(rows))
    return [col for col in rows[0].keys() if col not in numeric_cols]


def _find_rank_or_sort_metric(rows: list[dict]) -> str | None:
    numeric_cols = _pick_numeric_columns(rows)

    priority = [
        "total_revenue",
        "revenue_contribution",
        "customer_revenue",
        "monthly_revenue",
        "total_gmv",
        "gmv",
        "customer_count",
        "distinct_customers",
        "transaction_count",
        "sales_amount",
        "amount",
        "value",
    ]

    for preferred in priority:
        if preferred in numeric_cols:
            return preferred

    for col in numeric_cols:
        if col not in {"calendar_date", "month_start_date", "daypart", "daypart_seq"}:
            return col

    return numeric_cols[0] if numeric_cols else None


def _result_columns(rows: List[dict]) -> List[str]:
    """Column names of the result, taken from the rows themselves.

    The payload also carries a `columns` list, but only the atomic path populates
    it, and only the row dicts are in scope on the streaming path. The first row is
    authoritative either way.
    """
    for row in rows or []:
        if isinstance(row, dict):
            return list(row.keys())
    return []


def _truncation_note(rows_payload: Dict[str, Any]) -> str:
    """The row-cap sentence, or "" when the whole result came back."""
    if not rows_payload.get("truncated"):
        return ""
    return str(rows_payload.get("truncation_note") or "").strip()


def _with_truncation_disclosure(result: AnalysisResult, note: str) -> AnalysisResult:
    """State the row cap as a limitation whenever it actually bit.

    The executor stops the database at MAX_QUERY_ROWS, and a partial result
    narrated as a complete one is worse than a slow query. Limitations only -- the
    cap changes how much of the answer is shown, not whether the figures in it are
    real.
    """
    if not note:
        return result

    if not any("rows are included" in item.lower() for item in result.limitations):
        result.limitations = [*result.limitations, note]

    return result


def _customer_detail_answer(query: str, sql: str, rows: list[dict]) -> AnalysisResult:
    columns = list(rows[0].keys())
    first = rows[0]
    total_num = _safe_number(first.get("total_customer_count"))
    total = int(total_num) if total_num is not None else first.get("total_customer_count")
    preview_count = len(rows)
    metric = _find_rank_or_sort_metric(rows)
    preview = _preview_note(rows)

    top_customer = None
    top_metric = first.get(metric) if metric else None

    if metric and top_customer is not None:
        answer = (
            f"I found {total} matching customers for this request. "
            f"The API response is a ranked preview, not always the full customer list: {preview} "
            f"In the preview, customer {top_customer} ranks first by {metric}"
            f"{f' with {top_metric}' if top_metric is not None else ''}. "
            "This means the returned rows are useful for inspecting the leading customers behind the result, "
            "while the total_customer_count column should be used as the real population size."
        )
    else:
        answer = (
            f"I found {total} matching customers for this request. "
            f"The API response is a preview, not necessarily the complete customer list: {preview} "
            "Use total_customer_count as the real population size and the returned rows as the sample/detail view."
        )

    return AnalysisResult(
        natural_answer=answer,
        finding=f"{total} customers matched the request; {preview_count} rows are returned as the current preview.",
        calculation_logic=[
            "filtered the relevant serving table using the requested business conditions",
            "aggregated the result before applying the output limit",
            "calculated total_customer_count before applying the preview limit",
        ],
        interpretation=[
            "The LIMIT protects the API from returning a very large customer list, but it must not be interpreted as the total number of customers.",
        ],
        next_step=[
            "break the matching customers down by membership tier, value segment, store, opco, product line, or category to understand the profile and drivers",
        ],
        limitations=[
            "Only the preview rows are included in this response; use pagination or export for the full customer list.",
        ],
        chart_type="table",
        chart_title="Customer preview",
        columns=columns,
        chart_data=rows,
    )


def _single_row_answer(rows: list[dict], fmt) -> AnalysisResult:
    first = rows[0]
    columns = list(first.keys())

    meaningful = [
        f"{col} is {fmt(col, value)}"
        for col, value in first.items()
        if value is not None
    ]

    answer = "The result is " + ", ".join(meaningful) + "."

    return AnalysisResult(
        natural_answer=answer,
        finding=", ".join(meaningful),
        calculation_logic=["ran the requested aggregation using the selected filters"],
        interpretation=["This is a direct metric answer, so there is no distribution or trend to compare inside this result."],
        next_step=["compare it by month, opco, store, category, or segment if you need context"],
        chart_type="text",
        columns=columns,
    )


def _trend_answer(rows: list[dict], money: frozenset[str]) -> AnalysisResult | None:
    if not rows:
        return None

    first = rows[0]
    if "calendar_date" not in first and "month_start_date" not in first:
        return None

    metric = _find_rank_or_sort_metric(rows)
    if not metric:
        return None

    values = [_safe_number(row.get(metric)) for row in rows if _safe_number(row.get(metric)) is not None]
    if not values:
        return None

    start = values[0]
    end = values[-1]
    direction = "increased" if end > start else "decreased" if end < start else "stayed flat"
    change = end - start
    pct_change = (change / start * 100) if start else None

    # A percentage change is dimensionless, so only the three levels take a symbol.
    def level(value: float) -> str:
        return format_amount(value) if metric in money else f"{value:g}"

    if pct_change is not None:
        movement = (
            f"{direction} from {level(start)} to {level(end)}, "
            f"a change of {level(change)} ({pct_change:.1f}%)."
        )
    else:
        movement = f"{direction} from {level(start)} to {level(end)}, a change of {level(change)}."

    return AnalysisResult(
        natural_answer=(
            f"Across the returned monthly periods, {metric} {movement} "
            "This suggests the selected customer/business slice has a measurable month-to-month movement, "
            "so the next useful step is to compare the movement against opco, store, category, or segment mix."
        ),
        finding=f"{metric} {movement}",
        calculation_logic=["grouped the data by period and compared the first and last returned periods"],
        interpretation=["The trend direction shows whether the selected metric is expanding, declining, or stable over the returned period."],
        next_step=["compare the same trend by opco, store, product category, or membership segment to find the driver"],
        chart_type="line_chart",
        chart_title=f"Monthly trend of {metric}",
        x_field="period",
        x_axis="Month",
        y_axis=metric,
        series=[{"id": metric, "label": metric}],
        chart_data=[
            {
                "period": str(row.get("calendar_date") or row.get("month_start_date"))
                if (row.get("calendar_date") or row.get("month_start_date")) is not None
                else str(idx + 1),
                metric: row.get(metric),
            }
            for idx, row in enumerate(rows)
        ],
        columns=list(first.keys()),
    )


def _breakdown_answer(rows: list[dict], fmt) -> AnalysisResult | None:
    if len(rows) < 2:
        return None

    categorical_columns = _pick_categorical_columns(rows)
    numeric_columns = _pick_numeric_columns(rows)

    if not categorical_columns or not numeric_columns:
        return None

    label_field = categorical_columns[0]
    value_field = _find_rank_or_sort_metric(rows) or numeric_columns[0]

    sorted_rows = sorted(
        rows,
        key=lambda row: _safe_number(row.get(value_field)) if _safe_number(row.get(value_field)) is not None else float("-inf"),
        reverse=True,
    )

    top = sorted_rows[0]
    top_label = top.get(label_field)
    top_value = _safe_number(top.get(value_field))
    numeric_values = [_safe_number(row.get(value_field)) for row in sorted_rows]
    numeric_values = [value for value in numeric_values if value is not None]
    total = sum(numeric_values) if numeric_values else None

    share_text = ""
    if total and top_value is not None:
        share = top_value / total * 100
        share_text = f" This top value contributes about {share:.1f}% of the returned total."

    answer = (
        f"The strongest returned segment is {top_label} with {value_field} = "
        f"{fmt(value_field, top.get(value_field))}."
        f"{share_text} "
        "That points to where the selected metric is most concentrated in this result, so the next step is to compare the leader against another period or drill into its customer/profile mix."
    )

    return AnalysisResult(
        natural_answer=answer,
        finding=f"{top_label} leads by {value_field}.",
        calculation_logic=["grouped the result by the returned category field and compared the selected metric across groups"],
        interpretation=["The leading group indicates the largest contributor or most concentrated segment in the returned result."],
        next_step=["compare the same breakdown against another month or add another dimension such as opco, store, product category, or segment"],
        chart_type="bar_chart",
        chart_title=f"{value_field} by {label_field}",
        label_field="label",
        value_field="value",
        x_axis=label_field,
        y_axis=value_field,
        chart_data=[
            {"label": str(row.get(label_field)), "value": _safe_number(row.get(value_field))}
            for row in sorted_rows[:20]
            if row.get(label_field) is not None and _safe_number(row.get(value_field)) is not None
        ],
        columns=list(rows[0].keys()),
    )


def _table_answer(rows: list[dict]) -> AnalysisResult:
    return AnalysisResult(
        natural_answer=(
            f"The query returned {len(rows)} rows. "
            "I am showing it as a table because the result contains mixed fields or record-level detail rather than a single metric, trend, or clean category breakdown."
        ),
        finding=f"{len(rows)} rows returned.",
        calculation_logic=["ran the planned SQL and prepared the returned rows for display"],
        interpretation=["This result is better read as a detail table than as a chart."],
        next_step=["ask for a ranking, profile, trend, comparison, or breakdown to get a more analytical summary"],
        chart_type="table",
        chart_title="Query result",
        columns=list(rows[0].keys()) if rows else [],
        chart_data=rows,
    )


def _basic_deterministic_analysis(query: str, sql: str, rows: list[dict]) -> AnalysisResult:
    if not rows:
        return AnalysisResult(
            natural_answer=(
                "I didn't find any data for that request. "
                "Try widening the date range or removing one of the filters, and I'll take another look."
            ),
            finding="No matching rows were returned.",
            limitations=["The selected filters may not exist in the current serving tables."],
            chart_type="text",
        )

    if _is_customer_detail_preview(rows):
        return _customer_detail_answer(query, sql, rows)

    # The unit comes from the SQL, not from the column names: see
    # app/service/currency.py for why a name-based rule cannot work here.
    columns = _result_columns(rows)
    money = currency_columns(sql, columns)
    fmt = amount_formatter(sql, columns)

    if len(rows) == 1:
        return _single_row_answer(rows, fmt)

    trend = _trend_answer(rows, money)
    if trend:
        return trend

    breakdown = _breakdown_answer(rows, fmt)
    if breakdown:
        return breakdown

    return _table_answer(rows)


def _compact_json(value: Any, max_chars: int = 16000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)

    if len(text) <= max_chars:
        return text

    return text[:max_chars] + "...[truncated]"


async def analyze_sql_result(
    query: str,
    sql: str,
    rows_payload: Dict[str, Any],
    *,
    intent_reason: str | None = None,
    plan_reason: str | None = None,
    lookup_context: str | None = None,
) -> Dict[str, Any]:
    rows = _rows_as_dicts(rows_payload)
    deterministic = _basic_deterministic_analysis(query, sql, rows)
    cap_note = _truncation_note(rows_payload)
    deterministic = _with_truncation_disclosure(deterministic, cap_note)

    if not rows:
        return sanitize_for_json(deterministic.model_dump())

    if _is_customer_detail_preview(rows):
        return sanitize_for_json(deterministic.model_dump())

    stream_writer = get_stream_writer()

    if stream_writer is not None:
        streamed_answer = await _stream_analysis_answer(
            query=query,
            sql=sql,
            rows=rows,
            deterministic=deterministic,
            intent_reason=intent_reason,
            plan_reason=plan_reason,
            lookup_context=lookup_context,
        )

        if streamed_answer.strip():
            deterministic.natural_answer = streamed_answer.strip()

        return sanitize_for_json(
            _with_truncation_disclosure(deterministic, cap_note).model_dump()
        )

    prompt = f"""
Intent reason:
{intent_reason or "None"}

Plan reason:
{plan_reason or "None"}

Resolved display context:
{lookup_context or "None"}

SQL already executed:
{sql}

Rows preview:
{_compact_json(rows[:100])}

Monetary columns:
{currency_note(sql, _result_columns(rows))}

Deterministic fallback:
{_compact_json(deterministic.model_dump())}

User query:
{query}
""".strip()

    try:
        result = await get_llm().generate_json(
            prompt,
            AnalysisResult,
            system_instruction=ANALYSIS_SYSTEM_RULES,
        )

        if not result.natural_answer.strip():
            result.natural_answer = deterministic.natural_answer

        if not result.finding.strip():
            result.finding = deterministic.finding

        if not result.calculation_logic:
            result.calculation_logic = deterministic.calculation_logic

        if not result.interpretation:
            result.interpretation = deterministic.interpretation

        if not result.next_step:
            result.next_step = deterministic.next_step

        if not result.limitations:
            result.limitations = deterministic.limitations

        if not result.chart_type:
            result.chart_type = deterministic.chart_type

        if not result.chart_title:
            result.chart_title = deterministic.chart_title

        if not result.columns:
            result.columns = deterministic.columns

        if not result.chart_data:
            result.chart_data = deterministic.chart_data

        if result.chart_type in {"bar_chart", "donut_chart"}:
            valid_points = []
            for point in result.chart_data[:20]:
                if not isinstance(point, dict):
                    continue
                label = point.get("label")
                value = _safe_number(point.get("value"))
                if label is None or value is None:
                    continue
                valid_points.append({"label": str(label), "value": value})
            if valid_points:
                result.chart_data = valid_points
                result.label_field = "label"
                result.value_field = "value"
            else:
                result.chart_type = deterministic.chart_type
                result.chart_data = deterministic.chart_data
                result.label_field = deterministic.label_field
                result.value_field = deterministic.value_field

        return sanitize_for_json(
            _with_truncation_disclosure(result, cap_note).model_dump()
        )
    except Exception:
        return sanitize_for_json(deterministic.model_dump())
    
async def _stream_analysis_answer(
    *,
    query: str,
    sql: str,
    rows: list[dict],
    deterministic: AnalysisResult,
    intent_reason: str | None = None,
    plan_reason: str | None = None,
    lookup_context: str | None = None,
) -> str:
    stream_writer = get_stream_writer()
    if stream_writer is None:
        return deterministic.natural_answer

    prompt = f"""
Intent reason:
{intent_reason or "None"}

Plan reason:
{plan_reason or "None"}

Resolved display context:
{lookup_context or "None"}

SQL already executed:
{sql}

Rows preview:
{_compact_json(rows[:100])}

Monetary columns:
{currency_note(sql, _result_columns(rows))}

Deterministic fallback:
{_compact_json(deterministic.model_dump())}

User query:
{query}
""".strip()

    await stream_writer("status", {"message": "streaming answer"})

    pieces: list[str] = []

    async for delta in get_llm().stream_text(prompt, system_instruction=ANALYSIS_STREAM_SYSTEM_RULES):
        pieces.append(delta)
        await stream_writer("delta", {"text": delta})

    answer = "".join(pieces).strip()
    return answer or deterministic.natural_answer
