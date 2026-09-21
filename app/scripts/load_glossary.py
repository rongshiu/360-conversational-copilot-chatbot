# app/scripts/load_glossary.py
"""Seed the governed glossary table (copilot_glossary) from the glossary CSV.

The CSV is the offline seed input; this loader is the single writer of the runtime
source of truth in ci_meta.copilot_glossary. Run it after
`load_reference_data`, and whenever the glossary CSV changes:

    python -m app.scripts.load_glossary
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from app.core import settings
from app.db.postgres import get_pg_conn
from app.utils.text_utils import clean_text_value, parse_enum_values, safe_schema_name

# Canonical column -> accepted CSV header aliases (new canonical + legacy names).
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "table_name": ("table_name", "Table_Name"),
    "column_name": ("column_name", "field_name", "Field_Name"),
    "table_title": ("table_title", "Table_Title", "Table_Label"),
    "data_type": ("data_type", "Data_Type"),
    "grain": ("grain", "Grain"),
    "enum_values": ("enum_values", "Enum_Values", "sample_values", "Sample_Values"),
    "remarks": ("remarks", "Remarks"),
    "description": ("description", "Description"),
    "lookup_resolution_mode": ("lookup_resolution_mode", "Lookup_Resolution_Mode"),
    "lookup_resolution_scope": ("lookup_resolution_scope", "Lookup_Resolution_Scope"),
    "lookup_reference": ("lookup_reference", "Lookup_Reference"),
    # v3 governance columns. These drive the per-request filtering of the planner's
    # schema context, so a missing min_role_level would silently expose a money
    # column to an executive.
    "metric_class": ("metric_class", "Metric_Class"),
    "min_role_level": ("min_role_level", "Min_Role_Level"),
    "is_identity": ("is_identity", "Is_Identity"),
    "aggregation_rule": ("aggregation_rule", "Aggregation_Rule"),
}

_TRUE = {"TRUE", "1", "YES", "Y", "T"}


def _flag(row: dict[str, Any], *names: str) -> bool:
    return _first(row, *names).strip().upper() in _TRUE


def _first(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = clean_text_value(row.get(name))
        if value:
            return value
    return ""


def read_csv(path: str) -> pd.DataFrame:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Glossary CSV not found: {csv_path}")
    return pd.read_csv(csv_path).rename(columns=lambda c: str(c).strip())


def build_glossary_rows() -> list[tuple[Any, ...]]:
    glossary_path = os.getenv("GLOSSARY_CSV_PATH", "/app/data/v3_column_glossary.csv")
    df = read_csv(glossary_path)

    rows: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()
    # Ordinal restarts per table so schema context renders columns in their
    # declared order rather than CSV row order.
    ordinals: dict[str, int] = {}

    for record in df.to_dict("records"):
        row = {str(k).strip(): v for k, v in record.items()}

        table_name = _first(row, *COLUMN_ALIASES["table_name"])
        column_name = _first(row, *COLUMN_ALIASES["column_name"])
        if not table_name or not column_name:
            continue

        key = (table_name, column_name)
        if key in seen:
            continue
        seen.add(key)

        enum_values = parse_enum_values(_first(row, *COLUMN_ALIASES["enum_values"]))

        ordinals[table_name] = ordinals.get(table_name, 0) + 1
        ordinal = ordinals[table_name]

        metric_class = _first(row, *COLUMN_ALIASES["metric_class"]) or None
        min_role_level = (_first(row, *COLUMN_ALIASES["min_role_level"]) or "EXEC").upper()
        aggregation_rule = _first(row, *COLUMN_ALIASES["aggregation_rule"]) or None
        is_identity = _flag(row, *COLUMN_ALIASES["is_identity"])

        # The table has CHECK constraints for both of these. Failing here with a
        # readable message beats a raw constraint violation from executemany.
        if metric_class == "measure_money" and min_role_level != "HOD":
            raise ValueError(
                f"{table_name}.{column_name}: metric_class=measure_money requires "
                f"min_role_level=HOD, got {min_role_level!r}"
            )
        if is_identity and aggregation_rule != "COUNT_DISTINCT_ONLY":
            raise ValueError(
                f"{table_name}.{column_name}: is_identity requires "
                f"aggregation_rule=COUNT_DISTINCT_ONLY, got {aggregation_rule!r}"
            )

        rows.append(
            (
                table_name,
                column_name,
                _first(row, *COLUMN_ALIASES["table_title"]) or None,
                _first(row, *COLUMN_ALIASES["data_type"]) or None,
                _first(row, *COLUMN_ALIASES["grain"]) or None,
                json.dumps(enum_values, ensure_ascii=False),
                _first(row, *COLUMN_ALIASES["remarks"]) or None,
                _first(row, *COLUMN_ALIASES["description"]) or None,
                _first(row, *COLUMN_ALIASES["lookup_resolution_mode"]) or None,
                _first(row, *COLUMN_ALIASES["lookup_resolution_scope"]) or None,
                _first(row, *COLUMN_ALIASES["lookup_reference"]) or None,
                metric_class,
                min_role_level,
                is_identity,
                aggregation_rule,
                ordinal,
            )
        )

    return rows


def main() -> None:
    # ci_meta, not POSTGRES_SCHEMA: the governed glossary moved with the rest of
    # the v3 metadata.
    schema = safe_schema_name(settings.ci_meta_schema)
    rows = build_glossary_rows()

    insert_sql = f"""
        INSERT INTO {schema}.copilot_glossary (
            table_name,
            column_name,
            table_title,
            data_type,
            grain,
            enum_values,
            remarks,
            description,
            lookup_resolution_mode,
            lookup_resolution_scope,
            lookup_reference,
            metric_class,
            min_role_level,
            is_identity,
            aggregation_rule,
            ordinal,
            is_active,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, true, now(), now())
    """

    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            # Writes go through the loader role: every scoped table has FORCE ROW
            # LEVEL SECURITY, and the login user has no policy of its own.
            cur.execute(f'SET LOCAL ROLE "{settings.ci_loader_role}"')
            cur.execute(f"TRUNCATE TABLE {schema}.copilot_glossary")
            if rows:
                cur.executemany(insert_sql, rows)

    # Tuple positions must match the INSERT column list above.
    _MIN_ROLE_LEVEL, _IS_IDENTITY = 12, 15
    hod = sum(1 for r in rows if r[_MIN_ROLE_LEVEL] == "HOD")
    identity = sum(1 for r in rows if r[_IS_IDENTITY])
    print(
        f"Loaded {len(rows)} glossary rows into {schema}.copilot_glossary "
        f"({hod} HOD-only, {identity} identity)"
    )


if __name__ == "__main__":
    main()
