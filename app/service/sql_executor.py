# app/service/sql_executor.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session_scope import assert_scope_active
from app.service.copilot_usage_service import _now_ms, get_usage_tracker
from app.service.mlflow_observability import mlflow_span, safe_dict


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _normalize_pg_rows(rows: list[dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {
            "columns": [],
            "rows": [],
            "error": None,
            "raw_text": "no rows",
        }

    columns = list(rows[0].keys())
    data_rows = [
        [_json_safe(row.get(column)) for column in columns]
        for row in rows
    ]

    return {
        "columns": columns,
        "rows": data_rows,
        "error": None,
        "raw_text": "",
    }


def _with_row_cap(sql: str, row_cap: int) -> str:
    """Wrap a validated SELECT so PostgreSQL stops producing rows at the cap.

    MAX_QUERY_ROWS was only ever a preview limit: the executor called .all() and
    the driver buffered the entire result first, so a query with a bad GROUP BY
    materialised every row in the application before anything was trimmed. The
    trimming happened later, in the answer, which made the setting look like a
    protection it was not.

    Wrapping rather than appending ` LIMIT n`: the inner query may already end in
    its own LIMIT or ORDER BY, and an outer LIMIT composes with either. The leading
    newline matters -- generated SQL can end in a line comment.

    One row past the cap is requested so the caller can tell "exactly n rows" from
    "n rows and more were dropped".
    """
    return f"SELECT * FROM (\n{sql}\n) AS _row_cap LIMIT {row_cap + 1}"


def classify_sql_error(message: str) -> str:
    low = (message or "").lower()

    if "current transaction is aborted" in low or "infailedsqltransactionerror" in low:
        return (
            "A previous PostgreSQL statement failed and left the transaction aborted. "
            "Rollback the session before retrying the SQL."
        )

    if "missing from-clause entry" in low or "invalid reference to from-clause entry" in low:
        return (
            "The SQL referenced a table column incorrectly. "
            "Use table alias.column format, not schema.table.column, after assigning an alias."
        )

    if "does not exist" in low or "undefinedcolumn" in low or "undefinedtable" in low:
        return "The SQL referenced a missing table or column. Use exact table and column names from the glossary."

    if "syntax error" in low:
        return "The SQL has a PostgreSQL syntax problem. Fix the query structure."

    if "operator does not exist" in low or "invalid input syntax" in low or "cannot cast" in low:
        return "The SQL has a type mismatch. Fix casting, comparison, or filter logic."

    if "permission denied" in low:
        return "The database user does not have permission to query one of the referenced tables."

    return message or "Unknown SQL execution error."


async def run_sql(sql: str, db: AsyncSession) -> Dict[str, Any]:
    """
    Execute one PostgreSQL SELECT/WITH query safely.

    Adds:
    - a hard assertion that the caller's scope is active
    - PostgreSQL statement_timeout
    - a hard row cap, applied by the database
    - async timeout guard
    - rollback on timeout/error
    - usage tracking
    """
    import asyncio

    from app.core import settings
    timeout_seconds = int(settings.sql_timeout_seconds)
    row_cap = max(1, int(settings.max_query_rows))
    started_ms = _now_ms()

    try:
        with mlflow_span(
            "sql.run_sql",
            inputs={"sql": sql},
            attributes={
                "span_category": "sql",
                "sql_timeout_seconds": timeout_seconds,
                "row_limit": row_cap,
            },
        ) as span:
            # Fail fast if scoping was not applied. Without the session role and
            # GUCs, row-level security filters everything to zero rows -- which
            # reads as "no data" rather than "misconfigured", so a scoping bug
            # would surface as a confidently wrong answer instead of an error.
            await assert_scope_active(db)

            # SET LOCAL only applies inside the current transaction.
            # Milliseconds are required by PostgreSQL.
            await db.execute(text(f"SET LOCAL statement_timeout = {timeout_seconds * 1000}"))

            # SAVEPOINT, not the outer transaction.
            #
            # db.rollback() would end the transaction, and SET LOCAL ROLE plus every
            # set_config(..., true) GUC dies with it -- so after one failed query the
            # session silently drops back to the login role and RLS stops applying.
            # assert_scope_active() catches that and refuses to run, which means the
            # retry loop could never recover from a single bad query.
            #
            # A savepoint rolls back just the failed statement and leaves the scope
            # intact, so the planner's retry runs correctly scoped.
            async with db.begin_nested():
                result = await asyncio.wait_for(
                    db.execute(text(_with_row_cap(sql, row_cap))),
                    timeout=timeout_seconds + 5,
                )
                mappings = result.mappings().all()

            # One row past the cap is fetched precisely so this is knowable. A
            # truncated result that says nothing about it is the confident-wrong
            # answer again: "top stores" over a trimmed set reads as complete.
            truncated = len(mappings) > row_cap
            if truncated:
                mappings = mappings[:row_cap]

            rows = [dict(row) for row in mappings]
            payload = _normalize_pg_rows(rows)
            payload["row_limit"] = row_cap
            payload["truncated"] = truncated
            if truncated:
                payload["truncation_note"] = (
                    f"Only the first {row_cap} rows are included; the query returned "
                    "more. Narrow the period or the filters, or ask for a ranking, to "
                    "see a complete result."
                )

            latency_ms = max(0, _now_ms() - started_ms)
            row_count = len(payload.get("rows") or [])

            tracker = get_usage_tracker()
            if tracker is not None:
                tracker.add_sql_call(
                    name="run_sql",
                    latency_ms=latency_ms,
                    rows_returned=row_count,
                    sql=sql,
                )

            if span is not None:
                span.set_outputs(
                    safe_dict(
                        {
                            "row_count": row_count,
                            "truncated": truncated,
                            "columns": payload.get("columns") or [],
                            "latency_ms": latency_ms,
                            "error": None,
                        },
                        limit=1000,
                    )
                )

            return payload

    except asyncio.TimeoutError:
        # The savepoint has already unwound; the outer transaction and the caller's
        # scope survive so a retry can run.
        latency_ms = max(0, _now_ms() - started_ms)

        tracker = get_usage_tracker()
        if tracker is not None:
            tracker.add_sql_call(
                name="run_sql_timeout",
                latency_ms=latency_ms,
                rows_returned=0,
                sql=sql,
            )

        return {
            "columns": [],
            "rows": [],
            "error": (
                f"SQL execution timed out after {timeout_seconds} seconds. "
                "The generated query may be too expensive. Please simplify the query, add a period, "
                "or use a customer-level snapshot table when possible."
            ),
            "raw_text": "",
        }

    except Exception as exc:
        latency_ms = max(0, _now_ms() - started_ms)

        tracker = get_usage_tracker()
        if tracker is not None:
            tracker.add_sql_call(
                name="run_sql_error",
                latency_ms=latency_ms,
                rows_returned=0,
                sql=sql,
            )

        return {
            "columns": [],
            "rows": [],
            "error": str(exc),
            "raw_text": "",
        }