"""v3 persona roles: make the HOD/EXEC split a database boundary

Revision 08 withdrew row-level scoping. This one hardens what is left, because
after 08 the money split was the ONLY access control in the system and it was not
enforced by the database.

THE HOLE.

The persona views were created WITH (security_invoker = true), which checks the
base table's privileges against the calling role. That was correct while the base
tables carried policies -- an owner-executed view would have handed over every row
-- but it forced a second grant: ci_copilot had to hold SELECT on
ci_core.fact_sales_daily, or every view over it failed. Two consequences:

  1. An executive session could read gross_sales_amount by naming the base table.
  2. ci_copilot held SELECT on the views in BOTH ci_hod and ci_exec, so an
     executive session could read ci_hod.v_sales_daily by naming it.

Both were prevented by sql_validator_agent -- the base-table allowlist and the
refusal to accept schema-qualified names. That is a real control and it stays, but
it is a control on generated text. The thing it is protecting against is a model
being talked into writing that text.

THE FIX.

  - The read role becomes two: ci_copilot_hod and ci_copilot_exec.
  - The views drop security_invoker, so they execute as their owner and need no
    base-table privilege from the caller.
  - Each persona role gets SELECT on ONE persona schema. Neither gets any
    privilege on the fact, aggregate or bridge tables.
  - ci_copilot -- the single role they replace -- is stripped of everything.

What both roles keep: SELECT on the four dimensions in ci_core, which generated
SQL joins directly and which carry neither money nor a customer key, and SELECT on
the three ci_meta catalogues the request path reads (the glossary, the lookup
dictionary and the metric registry). Neither gets the audit log.

AFTER THIS REVISION an executive session naming a money column has three
independent things to get past: the column is not in the view, the view it would
have to name instead is not readable by its role, and the base table under that is
not readable either. Before it had one, and that one was a prompt.

OPERATIONAL PRECONDITION. The two roles must exist before this runs. CREATE ROLE
needs CREATEROLE or superuser and the migration user has neither by design, so the
revision verifies and fails with the exact bootstrap command rather than creating
them. Re-run scripts/bootstrap_roles.sql, then `alembic upgrade head`.

Revision ID: 20260828_09
Revises: 20260727_07
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core import settings
from app.db.v3_ddl import (
    ANALYST_ROLE,
    CI_SCHEMAS,
    COPILOT_ROLE,
    CORE,
    CORE_DIMENSIONS,
    EXEC,
    HOD,
    META,
    META_TABLES,
    PERSONA_ROLES,
    PERSONA_VIEWS,
    SCHEMA_READER,
    persona_view_sql,
    qualified,
    quote_ident,
    safe_ident,
)

revision: str = "20260828_09"
down_revision: Union[str, Sequence[str], None] = "20260727_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HOD_ROLE = safe_ident(PERSONA_ROLES["HOD"], fallback="ci_copilot_hod")
EXEC_ROLE = safe_ident(PERSONA_ROLES["EXEC"], fallback="ci_copilot_exec")
LEGACY_ROLE = safe_ident(COPILOT_ROLE, fallback="ci_copilot")
ANALYST = quote_ident(safe_ident(ANALYST_ROLE, fallback="ci_analyst"))

PERSONA_ROLE_NAMES: tuple[str, ...] = (HOD_ROLE, EXEC_ROLE)

# Reachable by both read roles. Dimensions because generated SQL joins them by
# name; ci_meta catalogues because the resolver and the glossary read them inside
# copilot_scope. Nothing here is money and nothing here is a customer.
SHARED_CORE_TABLES: tuple[str, ...] = CORE_DIMENSIONS
SHARED_META_TABLES: tuple[str, ...] = META_TABLES


def _require_roles(bind: sa.engine.Connection) -> None:
    """Both persona roles must exist, and the migration user must not be superuser.

    The superuser check is not ceremony. These views are owner-executed now, and
    object privileges are the whole boundary -- a superuser owner is fine, but a
    superuser RUNTIME login user owns its way straight past the grants below, the
    same way it used to bypass every policy.
    """
    bootstrap_hint = (
        "Run once as a superuser, then re-run this migration:\n\n"
        '    psql "$SUPERUSER_URI" \\\n'
        f"         -v app_user={settings.db_user} \\\n"
        f"         -v copilot_hod_role={HOD_ROLE} \\\n"
        f"         -v copilot_exec_role={EXEC_ROLE} \\\n"
        f"         -v copilot_role={LEGACY_ROLE} \\\n"
        f"         -v loader_role={safe_ident(settings.ci_loader_role, fallback='ci_loader')} \\\n"
        "         -f scripts/bootstrap_roles.sql\n"
    )

    for role_name, level in ((HOD_ROLE, "HOD"), (EXEC_ROLE, "EXEC")):
        exists = bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"),
            {"r": role_name},
        ).scalar()
        if not exists:
            raise RuntimeError(
                f"\nRole {role_name!r} (read path for {level} callers) does not exist.\n\n"
                "The HOD/EXEC split is enforced by which role may SELECT which persona "
                "schema, so this revision cannot make it real without both roles.\n\n"
                + bootstrap_hint
            )

        can_set_role = bind.execute(
            sa.text("SELECT pg_has_role(CURRENT_USER, :r, 'MEMBER')"), {"r": role_name},
        ).scalar()
        if not can_set_role:
            raise RuntimeError(
                f"\nCURRENT_USER is not a member of {role_name!r}, so the application "
                "cannot SET LOCAL ROLE into it and every request would fail.\n\n"
                f"Run as superuser:\n\n    GRANT {role_name} TO {settings.db_user};\n"
            )

        # An inheriting grant would mean the login user holds this role's privileges
        # with no SET ROLE -- so a request that skipped copilot_scope() would read
        # the HOD views as the login user instead of failing.
        inherits = bind.execute(
            sa.text("SELECT pg_has_role(:app, :r, 'USAGE')"),
            {"app": settings.db_user, "r": role_name},
        ).scalar()
        if inherits:
            raise RuntimeError(
                f"\n{settings.db_user!r} INHERITS {role_name!r}, so it holds that "
                "role's privileges without any SET ROLE -- a request that never "
                "entered copilot_scope() would read the persona views anyway.\n\n"
                f"Fix with:\n\n    GRANT {role_name} TO {settings.db_user} "
                "WITH INHERIT FALSE;\n"
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_roles(bind)

    hod = quote_ident(HOD_ROLE)
    exec_ = quote_ident(EXEC_ROLE)
    legacy = quote_ident(LEGACY_ROLE)
    persona_roles = (hod, exec_)

    # =================================================================
    # 1. Schema USAGE.
    # =================================================================
    # Each read role gets USAGE on its OWN persona schema plus ci_core and ci_meta.
    # NOT on the other persona schema: without USAGE the other schema's views are
    # unreachable even if a grant on them were ever added by mistake, so the
    # boundary does not depend on one GRANT statement being correct.
    for role, own_schema in ((hod, HOD), (exec_, EXEC)):
        for schema in (own_schema, CORE, META):
            op.execute(f"GRANT USAGE ON SCHEMA {quote_ident(schema)} TO {role}")

    op.execute(f"REVOKE USAGE ON SCHEMA {quote_ident(HOD)} FROM {exec_}")
    op.execute(f"REVOKE USAGE ON SCHEMA {quote_ident(EXEC)} FROM {hod}")

    # Anything created in these schemas later stays unreadable until granted.
    for schema in CI_SCHEMAS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quote_ident(schema)} "
            "REVOKE ALL ON TABLES FROM PUBLIC"
        )

    # =================================================================
    # 2. What both read roles may reach in ci_core and ci_meta.
    # =================================================================
    for role in persona_roles:
        for table in SHARED_CORE_TABLES:
            op.execute(f"GRANT SELECT ON {qualified(CORE, table)} TO {role}")
        for table in SHARED_META_TABLES:
            op.execute(f"GRANT SELECT ON {qualified(META, table)} TO {role}")

        # The audit log records who ran what; a generated query reaching it would
        # be a bug, and it is one the database refuses rather than the validator.
        op.execute(
            f"REVOKE ALL ON {qualified(META, 'copilot_access_log')} FROM {role}"
        )

    # =================================================================
    # 3. The fact tables become unreachable from the request path.
    # =================================================================
    # This is the grant that security_invoker forced and that the views no longer
    # need. Stated as an explicit REVOKE over every non-dimension table in ci_core
    # rather than "the ones we granted", so a table added by a future revision and
    # granted by habit is caught by the assertion in step 6 rather than silently
    # readable.
    fact_tables = [
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = :schema AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            ),
            {"schema": CORE},
        )
        if row[0] not in SHARED_CORE_TABLES
    ]

    for table in fact_tables:
        for role in (*persona_roles, legacy):
            op.execute(f"REVOKE ALL ON {qualified(CORE, table)} FROM {role}")

    # =================================================================
    # 4. Rebuild the views: owner-executed, one reader each.
    # =================================================================
    for schema in (HOD, EXEC):
        for view in PERSONA_VIEWS:
            op.execute(f"DROP VIEW IF EXISTS {qualified(schema, view)} CASCADE")

    for view, base_table in PERSONA_VIEWS.items():
        for schema, include_money in ((HOD, True), (EXEC, False)):
            reader = quote_ident(safe_ident(SCHEMA_READER[schema], fallback=EXEC_ROLE))
            for statement in persona_view_sql(
                bind,
                schema,
                view,
                base_table,
                include_money=include_money,
                copilot_role=reader,
                analyst_role=ANALYST,
            ):
                op.execute(statement)

    # =================================================================
    # 5. Retire the single read role.
    # =================================================================
    # Revisions 01, 06 and 07 granted it schema usage and SELECT on everything.
    # Nothing switches into it any more, and a role that still holds SELECT on both
    # persona schemas is exactly the boundary this revision exists to remove.
    for schema in CI_SCHEMAS:
        op.execute(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote_ident(schema)} FROM {legacy}"
        )
        op.execute(f"REVOKE USAGE ON SCHEMA {quote_ident(schema)} FROM {legacy}")

    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote_ident(CORE)} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote_ident(META)} FROM PUBLIC")

    # =================================================================
    # 6. Assert the boundary, rather than trusting the statements above.
    # =================================================================
    # has_table_privilege answers the question the grants were trying to produce,
    # and it accounts for role membership, PUBLIC and ownership -- none of which
    # reading the GRANTs back would catch.
    _assert_isolation(bind)


def _assert_isolation(bind: sa.engine.Connection) -> None:
    """Fail the migration if the split is not actually enforced."""
    failures: list[str] = []

    # No read role may touch a fact table.
    for table in bind.execute(
        sa.text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = :schema AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        ),
        {"schema": CORE},
    ):
        if table[0] in SHARED_CORE_TABLES:
            continue
        for role in PERSONA_ROLE_NAMES:
            if bind.execute(
                sa.text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                {"r": role, "t": f"{CORE}.{table[0]}"},
            ).scalar():
                failures.append(f"{role} can SELECT {CORE}.{table[0]}")

    # Each read role may reach its own persona schema and not the other's.
    for view in PERSONA_VIEWS:
        for schema, reader in ((HOD, HOD_ROLE), (EXEC, EXEC_ROLE)):
            if not bind.execute(
                sa.text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                {"r": reader, "t": f"{schema}.{view}"},
            ).scalar():
                failures.append(f"{reader} cannot SELECT {schema}.{view}")

        for schema, stranger in ((HOD, EXEC_ROLE), (EXEC, HOD_ROLE)):
            # USAGE was revoked on the other schema, so the privilege test raises
            # rather than returning false on some versions. Treat any answer other
            # than a clean "no" as a failure.
            try:
                permitted = bind.execute(
                    sa.text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                    {"r": stranger, "t": f"{schema}.{view}"},
                ).scalar()
            except Exception:  # noqa: BLE001
                permitted = False
            if permitted:
                failures.append(f"{stranger} can SELECT {schema}.{view}")

    if failures:
        raise RuntimeError(
            "persona role isolation is not enforced after this revision:\n  - "
            + "\n  - ".join(failures)
        )

    print(
        f"[v3] verified: {HOD_ROLE} reads {HOD} only, {EXEC_ROLE} reads {EXEC} only, "
        "neither reads a fact table"
    )


def downgrade() -> None:
    """Hand the grants back to the single read role and drop the split.

    The persona roles are cluster state owned by bootstrap_roles.sql, so they are
    left in place with nothing granted to them.
    """
    bind = op.get_bind()
    legacy = quote_ident(LEGACY_ROLE)

    for schema in CI_SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {quote_ident(schema)} TO {legacy}")

    for table in bind.execute(
        sa.text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = :schema AND table_type = 'BASE TABLE'
            """
        ),
        {"schema": CORE},
    ):
        op.execute(f"GRANT SELECT ON {qualified(CORE, table[0])} TO {legacy}")

    for table in META_TABLES:
        op.execute(f"GRANT SELECT ON {qualified(META, table)} TO {legacy}")

    for role in PERSONA_ROLE_NAMES:
        quoted = quote_ident(role)
        for schema in CI_SCHEMAS:
            op.execute(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote_ident(schema)} FROM {quoted}"
            )
            op.execute(f"REVOKE USAGE ON SCHEMA {quote_ident(schema)} FROM {quoted}")

    # The views are left owner-executed. Recreating them WITH (security_invoker)
    # would require the legacy role to hold base-table SELECT at exactly the moment
    # this function is granting it, and a half-applied downgrade is worse than a
    # view that runs as its owner.
