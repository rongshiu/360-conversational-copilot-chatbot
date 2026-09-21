# app/service/access_log_service.py
"""Access audit trail: who ran what, and what was withheld.

Deliberately narrow. MLflow already stores the question, the generated SQL, the
executed SQL, row counts and latency, so none of that is duplicated here -- the
row links out via mlflow_trace_id.

What MLflow structurally cannot store is what this table exists for:

  - the REAL principal_id. mlflow_observability.safe_user_hash() hashes the user
    id, so a trace cannot answer "what did user X run".
  - denials, which are access outcomes rather than model observability.

WHY THIS SURVIVED THE ACCESS CHANGE, having very nearly not.

Three of its columns went with row-level security: opco_codes, is_group_user and
category_key_count recorded a grant scope that no longer exists, and the table's
own comment used to defer to RLS as "the structural proof" that scoping held,
leaving this as a record for investigating a misconfiguration window.

Read quickly, that argues for dropping the table once RLS is gone. It argues the
opposite. FORCE ROW LEVEL SECURITY was the evidence; with the evidence removed,
this log is the only durable record of access -- and every caller can now query
every OpCo's customer data, so what that record covers is materially more
sensitive than when a row filter bounded it.

Writing is best-effort: an audit failure must never turn a good answer into an
error for the user. Failures are logged loudly instead. That trade is worth
restating now that this is the only record: a lost row is invisible, so
CI_ACCESS_LOG_ENABLED should be treated as a compliance setting rather than a
performance one, and the log volume watched rather than assumed.
"""
from __future__ import annotations

from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings
from app.core.logging import Logger
from app.db.session_scope import system_scope
from app.db.v3_ddl import META, qualified

logger = Logger.get_logger(__name__)


async def write_access_log(
    db: AsyncSession,
    *,
    principal: Any,
    mlflow_trace_id: Optional[str] = None,
    denied_reason: Optional[str] = None,
) -> None:
    if not settings.ci_access_log_enabled or principal is None:
        return

    try:
        # The copilot read role has no grant on this table by design -- a
        # generated query reaching the audit log would be a bug. Writes go
        # through the trusted system role.
        async with system_scope(db):
            await db.execute(
                sa.text(
                    f"""
                    INSERT INTO {qualified(META, 'copilot_access_log')}
                        (mlflow_trace_id, principal_id, role_level, denied_reason)
                    VALUES
                        (:trace_id, :principal_id, :role_level, :denied_reason)
                    """
                ),
                {
                    "trace_id": mlflow_trace_id,
                    "principal_id": getattr(principal, "principal_id", None),
                    "role_level": getattr(principal, "role_level", None),
                    "denied_reason": denied_reason,
                },
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        # Loud, because this is now the only record. A silently dropped audit row
        # is worse than a noisy log line.
        logger.exception(
            "access log write failed for principal %s (trace %s)",
            getattr(principal, "principal_id", None),
            mlflow_trace_id,
        )
