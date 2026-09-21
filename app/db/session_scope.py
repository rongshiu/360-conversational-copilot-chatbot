# app/db/session_scope.py
"""Per-request database role and persona selection.

This module used to apply row-level scope. It no longer does: access control is
now the role split alone. Three roles:

  ci_loader        -- trusted write role, for the reference and fact loaders.
  ci_copilot_hod   -- read role for HOD callers. Reads ci_hod, and only ci_hod.
  ci_copilot_exec  -- read role for EXEC callers. Reads ci_exec, and only ci_exec.

The two read roles are what makes the money split a database fact. Neither holds
any privilege on the fact tables in ci_core, and neither can read the other's
schema, so an executive query naming ci_hod.v_sales_daily -- or the base table
under it -- is refused by PostgreSQL rather than by the SQL validator. The
validator still rejects both, earlier and with a better message; it is no longer
the only thing standing there.

The request path is a single scope:

    async with copilot_scope(db, principal):   # persona role + search_path
        ... every query the copilot generates ...

The system_scope() / resolve_principal() round-trip that used to precede it is
gone. It existed because expanding a category grant is a cross-scope read -- you
cannot resolve the caller's scope while already inside it -- and there is no
longer a scope to resolve.

SET LOCAL is used throughout so the settings unwind with the transaction and can
never leak into a pooled connection's next checkout.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings
from app.core.logging import Logger
from app.db.v3_ddl import (
    CORE,
    GUC_PRINCIPAL_ID,
    GUC_ROLE_LEVEL,
    LOADER_ROLE,
    META,
    PERSONA_ROLES,
    safe_ident,
)

logger = Logger.get_logger(__name__)

_LOADER = safe_ident(LOADER_ROLE, fallback="ci_loader")
_PERSONA_ROLES = {
    level: safe_ident(role, fallback="ci_copilot_exec")
    for level, role in PERSONA_ROLES.items()
}

async def _set_role(db: AsyncSession, role: str) -> None:
    # RESET first so the switch never depends on which role the connection was
    # left in. Not because chaining fails -- it does not: SET ROLE is authorised
    # against the SESSION user, which stays the login user, so
    # ci_copilot_exec -> ci_loader succeeds directly (verified). The reason is
    # pooling.
    # Connections are reused, and a role left over from an earlier unit of work
    # would otherwise decide what this one can see if the SET below ever failed.
    await db.execute(sa.text("RESET ROLE"))
    await db.execute(sa.text(f'SET LOCAL ROLE "{role}"'))


async def _set_guc(db: AsyncSession, name: str, value: str) -> None:
    # set_config is used rather than SET LOCAL because the value is a bind
    # parameter -- SET LOCAL takes no parameters, which would mean interpolating
    # caller-supplied strings into DDL-ish SQL.
    await db.execute(
        sa.text("SELECT set_config(:name, :value, true)"),
        {"name": name, "value": value},
    )


async def _safe_reset(db: AsyncSession) -> None:
    """Release the role without letting cleanup raise.

    A failed statement leaves the transaction aborted, and every subsequent
    command -- including RESET ROLE -- then fails with
    InFailedSQLTransactionError. Raising that from a finally block replaces the
    real error with a useless one, and inside an mlflow span it surfaced as
    "generator didn't stop after throw()". Roll back first so the session is
    reusable, and never let this path raise.
    """
    try:
        if db.in_transaction():
            await db.rollback()
    except Exception:  # noqa: BLE001
        logger.debug("rollback during scope cleanup failed", exc_info=True)

    try:
        await db.execute(sa.text("RESET ROLE"))
    except Exception:  # noqa: BLE001
        logger.debug("RESET ROLE during scope cleanup failed", exc_info=True)


@asynccontextmanager
async def system_scope(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Trusted cross-scope read/write access. Never wrap user-generated SQL in this."""
    await _set_role(db, _LOADER)
    try:
        yield db
    finally:
        await _safe_reset(db)


@asynccontextmanager
async def copilot_scope(db: AsyncSession, principal) -> AsyncIterator[AsyncSession]:
    """Apply the caller's role and persona for the duration of the block.

    Sets the read role, two session GUCs, and a search_path whose first entry is
    the caller's persona schema -- so the planner emits one unqualified query and
    the database resolves it to the permitted view.

    Neither GUC is a scope. principal_id is read back by the observability trace,
    and role_level is asserted against the session role by assert_scope_active().

    The role and the search_path are a matched pair, both derived from the same
    role_level: the role decides what the session MAY read, the search_path decides
    what an unqualified name resolves TO. Setting one without the other does not
    open a hole -- it produces "permission denied for view", which is the correct
    failure -- but they are set together here so no caller can arrange half of it.
    """
    role = safe_ident(principal.persona_role, fallback=settings.ci_copilot_exec_role)
    await _set_role(db, role)

    await _set_guc(db, GUC_PRINCIPAL_ID, principal.principal_id)
    await _set_guc(db, GUC_ROLE_LEVEL, principal.role_level)

    # Persona schema first so unqualified view names resolve to the permitted
    # variant, then ci_core and ci_meta for dimension joins and metadata.
    #
    # The pg_trgm schema used to be appended here so the `%` operator and
    # similarity() were reachable -- without it, fuzzy lookup died with
    # `operator does not exist: text % text`. Entity resolution now scores in
    # memory against the RLS-scoped dictionary, so nothing in the request path
    # calls a trigram function and the extension no longer needs to be on the
    # path. The GIN trgm indexes remain for ad-hoc queries.
    persona = safe_ident(principal.persona_schema, fallback=settings.ci_exec_schema)
    await db.execute(
        sa.text(f'SET LOCAL search_path TO "{persona}", "{CORE}", "{META}"')
    )

    try:
        yield db
    finally:
        await _safe_reset(db)


async def assert_scope_active(db: AsyncSession) -> None:
    """Fail fast if the request never went through copilot_scope().

    This check used to be load-bearing in a way it no longer is. Its stronger half
    tested that the OpCo GUC was set, because an unset scope made RLS filter every
    table to zero rows -- which reads as "no data" rather than "misconfigured", a
    silent wrong answer instead of an error. With no policies there is nothing left
    for that half to catch, and it is removed rather than left in place asserting
    something that cannot fail.

    What remains is worth keeping, and one part of it got stronger. Running as the
    login user is still a bug -- it has no grants at all, so it surfaces as a
    permission error far from its cause -- and role_level being unset means the
    persona schema fell back to ci_exec, which is safe but silently answers a HOD's
    question without money.

    The new part: the session role must be the one this caller's role_level maps
    to. Before the read role was split there was one correct answer and the check
    could hardcode it. Now a HOD session running as ci_copilot_exec, or the reverse,
    is a scope-assembly bug -- and the second direction is a privilege escalation,
    so it is asserted rather than left to produce a confusing "permission denied"
    three layers down.
    """
    row = (
        await db.execute(
            sa.text(
                """
                SELECT current_user AS role,
                       NULLIF(current_setting('app.role_level', true), '') AS role_level
                """
            )
        )
    ).mappings().first()

    if not row["role_level"]:
        raise RuntimeError(
            "app.role_level is not set, so the persona schema fell back to the "
            "executive view and money columns would be silently absent. "
            "SQL must run inside copilot_scope()."
        )

    expected = _PERSONA_ROLES.get(row["role_level"])
    if expected is None:
        raise RuntimeError(
            f"app.role_level is {row['role_level']!r}, which maps to no persona "
            f"role. Known levels: {', '.join(sorted(_PERSONA_ROLES))}."
        )

    if row["role"] != expected:
        raise RuntimeError(
            f"query attempted as role {row['role']!r}, but role_level "
            f"{row['role_level']!r} requires {expected!r}. SQL must run inside "
            "copilot_scope()."
        )
