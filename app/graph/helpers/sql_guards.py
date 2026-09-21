from __future__ import annotations

import re
from typing import Any

from app.service.table_registry import PERIOD_COLUMN


def strip_sql_comments_and_literals(sql: str | None) -> str:
    """Normalize SQL for lightweight guard checks only."""
    text = sql or ""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--.*?$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"'(?:''|[^'])*'", "''", text)
    return text.lower()


def sql_references_period_table(
    sql: str | None,
    *,
    fact_tables: set[str] | list[str],
) -> bool:
    """True when the SQL touches a period-grained view.

    v2 also matched an optional `postgres_schema.` prefix, because table names were
    schema-qualified. v3 views are always referenced bare -- the search_path
    resolves them to the caller's persona schema -- and the validator rejects any
    qualified name, so there is no prefix to match.
    """
    lowered = strip_sql_comments_and_literals(sql)
    if not lowered.strip():
        return False

    for table_name in fact_tables:
        if re.search(rf"\b{re.escape(table_name.lower())}\b", lowered):
            return True
    return False


def sql_has_period_filter(sql: str | None) -> bool:
    """True when a period column is constrained in a WHERE clause.

    v2 looked for event_year / event_month. The serving tables now use real dates:
    calendar_date on the daily views, month_start_date on the monthly ones.
    """
    lowered = strip_sql_comments_and_literals(sql)
    if not lowered.strip():
        return False

    columns = "|".join(sorted({re.escape(c) for c in PERIOD_COLUMN.values()}))

    for match in re.finditer(
        r"\bwhere\b(?P<where>.*?)(\bgroup\s+by\b|\border\s+by\b|\blimit\b|\bunion\b|$)",
        lowered,
        flags=re.DOTALL,
    ):
        if re.search(rf"\b(?:{columns})\b", match.group("where") or ""):
            return True

    return False


def fact_time_period_guard_question(
    state: dict[str, Any],
    sql: str | None,
    *,
    fact_tables: set[str] | list[str],
    postgres_schema: str | None = None,
) -> str | None:
    """Ask for a period rather than letting an unbounded scan through.

    `postgres_schema` is accepted and ignored, kept only so the single call site
    does not need to change shape; v3 view names are never schema-qualified.
    """
    if not sql_references_period_table(sql, fact_tables=fact_tables):
        return None

    if sql_has_period_filter(sql):
        return None

    return (
        "which period should I use? Every serving table is period-grained, so I need "
        "a date range, month, quarter, or year. Sales and daypart questions can use any "
        "date range; customer counts are available by whole month."
    )
