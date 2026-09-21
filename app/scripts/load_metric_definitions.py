# app/scripts/load_metric_definitions.py
"""Seed the metric registry (ci_meta.metric_definition) from the seed CSV.

    python -m app.scripts.load_metric_definitions

The registry exists so "membership penetration" and "basket size" have exactly one
definition each. Without it every turn re-derives them from the column list, and
two users asking the same question in different words can get numerators that do
not match -- transaction-based penetration one time, sales-based the next, with
nothing in the answer saying which.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from app.core import settings
from app.db.postgres import get_pg_conn
from app.utils.text_utils import clean_text_value, safe_schema_name

MONEY_CLASSES = {"money", "ratio_money"}
RATIO_CLASSES = {"ratio_volume", "ratio_money"}
_TRUE = {"TRUE", "1", "YES", "Y", "T"}


def _first(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = clean_text_value(row.get(name))
        if value:
            return value
    return ""


def _flag(row: dict[str, Any], name: str) -> bool:
    return _first(row, name).strip().upper() in _TRUE


def _synonyms(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        # Tolerate a plain comma-separated list in a hand-edited CSV.
        return [part.strip() for part in raw.split(",") if part.strip()]


def build_metric_rows() -> list[tuple[Any, ...]]:
    path = Path(os.getenv("METRIC_CSV_PATH", "/app/data/v3_metric_definition_seed.csv"))
    if not path.exists():
        raise FileNotFoundError(f"Metric seed CSV not found: {path}")

    df = pd.read_csv(path).rename(columns=lambda c: str(c).strip())
    rows: list[tuple[Any, ...]] = []
    seen: set[str] = set()

    for record in df.to_dict("records"):
        row = {str(k).strip(): v for k, v in record.items()}

        metric_key = _first(row, "metric_key")
        if not metric_key or metric_key in seen:
            continue
        seen.add(metric_key)

        metric_class = _first(row, "metric_class")
        min_role_level = (_first(row, "min_role_level") or "EXEC").upper()
        denominator = _first(row, "denominator_sql") or None
        is_additive = _flag(row, "is_additive")

        # The table enforces both of these with CHECK constraints. Failing here
        # names the offending metric, which a constraint violation from
        # executemany does not.
        if metric_class in MONEY_CLASSES and min_role_level != "HOD":
            raise ValueError(
                f"{metric_key}: metric_class={metric_class} is money-bearing and "
                f"requires min_role_level=HOD, got {min_role_level!r}"
            )
        if metric_class in RATIO_CLASSES and (denominator is None or is_additive):
            raise ValueError(
                f"{metric_key}: a ratio metric needs a denominator_sql and must not be "
                f"additive (denominator={denominator!r}, is_additive={is_additive})"
            )

        rows.append(
            (
                metric_key,
                _first(row, "metric_name") or metric_key,
                metric_class,
                min_role_level,
                _first(row, "base_table"),
                _first(row, "numerator_sql"),
                denominator,
                is_additive,
                _first(row, "grain_note") or None,
                json.dumps(_synonyms(_first(row, "synonyms")), ensure_ascii=False),
                _first(row, "description") or None,
            )
        )

    return rows


def main() -> None:
    schema = safe_schema_name(settings.ci_meta_schema)
    rows = build_metric_rows()

    insert_sql = f"""
        INSERT INTO {schema}.metric_definition (
            metric_key, metric_name, metric_class, min_role_level, base_table,
            numerator_sql, denominator_sql, is_additive, grain_note, synonyms,
            description, is_active, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, true, now(), now())
    """

    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET LOCAL ROLE "{settings.ci_loader_role}"')
            cur.execute(f"TRUNCATE TABLE {schema}.metric_definition")
            if rows:
                cur.executemany(insert_sql, rows)

    hod = sum(1 for r in rows if r[3] == "HOD")
    print(
        f"Loaded {len(rows)} metrics into {schema}.metric_definition "
        f"({hod} HOD-only, {len(rows) - hod} available to all roles)"
    )


if __name__ == "__main__":
    main()
