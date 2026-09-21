from __future__ import annotations

import math
from decimal import Decimal


def safe_float(value):
    if value is None:
        return None

    if isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, (int, float)):
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return None
        return num

    if isinstance(value, str):
        cleaned = value.strip()
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


def clean_chart_row_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def clean_chart_rows(rows: list[dict], limit: int = 50) -> list[dict]:
    cleaned: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        cleaned.append({key: clean_chart_row_value(val) for key, val in row.items()})
    return cleaned


def build_points_from_rows(
    rows: list[dict],
    *,
    label_field: str | None,
    value_field: str | None,
    limit: int = 20,
) -> list[dict]:
    if not rows or not label_field or not value_field:
        return []

    points: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue

        raw_label = row.get(label_field)
        raw_value = row.get(value_field)

        if raw_label is None:
            continue

        value = safe_float(raw_value)
        if value is None:
            continue

        label = str(raw_label).strip()
        if not label:
            continue

        points.append({"label": label, "value": value})

    return points


def looks_like_point_list(items: list[dict] | None) -> bool:
    if not items:
        return False
    return all(
        isinstance(item, dict) and "label" in item and "value" in item
        for item in items
    )


def normalize_point_list(items: list[dict], limit: int = 20) -> list[dict]:
    points: list[dict] = []
    for row in items[:limit]:
        label = str(row.get("label", "")).strip()
        value = safe_float(row.get("value"))
        if not label or value is None:
            continue
        points.append({"label": label, "value": value})
    return points


def resolve_line_chart(
    *,
    chart_data: list[dict] | None,
    fallback_rows: list[dict],
    x_field: str | None,
    series: list[dict] | None,
    limit: int = 50,
) -> tuple[str | None, list[dict], list[dict]]:
    source_rows = chart_data if chart_data else fallback_rows
    cleaned_rows = clean_chart_rows(source_rows, limit=limit)
    if not cleaned_rows:
        return None, [], []

    normalized_series = [
        {"id": item["id"], "label": item["label"]}
        for item in (series or [])
        if isinstance(item, dict) and item.get("id") and item.get("label")
    ]
    if not normalized_series:
        return None, [], []

    series_ids = {item["id"] for item in normalized_series}

    candidate_x = (x_field or "").strip() or None
    if candidate_x and all(candidate_x in row for row in cleaned_rows):
        if all(any(metric in row for metric in series_ids) for row in cleaned_rows):
            return candidate_x, normalized_series, cleaned_rows

    first_row_keys = list(cleaned_rows[0].keys())
    inferred_x = next((key for key in first_row_keys if key not in series_ids), None)
    if not inferred_x:
        return None, [], []

    valid_rows = []
    for row in cleaned_rows:
        if inferred_x not in row:
            continue
        if not any(metric in row for metric in series_ids):
            continue
        valid_rows.append(row)

    if not valid_rows:
        return None, [], []

    return inferred_x, normalized_series, valid_rows
