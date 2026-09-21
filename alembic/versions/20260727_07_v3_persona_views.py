"""v3 persona views: ci_hod and ci_exec

Same view names in two schemas. The request path puts one of them on the
search_path based on role_level, so the planner emits ONE query and the database
resolves it to the permitted view.

The executive views do not mask money columns -- they omit them. A query that
references gross_sales_amount as an executive fails with "column does not exist"
rather than quietly returning zeros, which is the behaviour you want: the retry
loop gets a real error it can act on, and no plausible-looking wrong number ever
reaches the answer agent.

Views also expose generated member_* columns so the planner never has to reach
for FILTER (WHERE customer_type = 'Member') to compute penetration.

Views are created WITH (security_invoker = true). That flag was load-bearing while
the base tables carried row-level policies: without it a view runs as its OWNER
and hands every row to whoever queries it, which would have been a hole straight
through the security model.

There are no policies left for it to defer to, and it is kept anyway. It costs
nothing, it keeps the views executing as the caller, and if row scoping is ever
reinstated for a subset of tables these views do not silently become the hole in
it. Removing it would be work now in exchange for a trap later.

Revision ID: 20260727_07
Revises: 20260727_06
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.v3_ddl import (
    ANALYST_ROLE,
    COPILOT_ROLE,
    EXEC,
    HOD,
    PERSONA_VIEWS,
    persona_view_sql,
    qualified,
    quote_ident,
    safe_ident,
)

revision: str = "20260727_07"
down_revision: Union[str, Sequence[str], None] = "20260727_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE = quote_ident(safe_ident(COPILOT_ROLE, fallback="ci_copilot"))
ANALYST = quote_ident(safe_ident(ANALYST_ROLE, fallback="ci_analyst"))

def _create_view(
    bind: sa.engine.Connection,
    schema: str,
    view: str,
    base_table: str,
    include_money: bool,
) -> None:
    """Build one persona view.

    The projection, money masking and grants live in app.db.v3_ddl.persona_view_sql
    so that later revisions -- which must rebuild these views whenever a base table
    gains a column -- cannot drift from what this one created.
    """
    for statement in persona_view_sql(
        bind,
        schema,
        view,
        base_table,
        include_money=include_money,
        copilot_role=ROLE,
        analyst_role=ANALYST,
    ):
        op.execute(statement)


def upgrade() -> None:
    bind = op.get_bind()

    # security_invoker requires PostgreSQL 15+. Without it a view would run with
    # the owner's privileges and bypass every RLS policy from the last revision.
    version = bind.execute(sa.text("SHOW server_version_num")).scalar()
    if int(version) < 150000:
        raise RuntimeError(
            f"PostgreSQL 15+ required for security_invoker views (found {version}). "
            "On an older server these views would bypass row-level security "
            "entirely, so the migration refuses to create them."
        )

    # Schema USAGE for the analyst, or the per-view grants below are dead: a
    # SELECT fails with "permission denied for schema" before privileges on the
    # view are even consulted. The copilot role gets its USAGE from revision 01
    # because it needs the persona schemas on its search_path.
    op.execute(
        f"GRANT USAGE ON SCHEMA {quote_ident(HOD)}, {quote_ident(EXEC)} TO {ANALYST}"
    )

    for view, base_table in PERSONA_VIEWS.items():
        _create_view(bind, HOD, view, base_table, include_money=True)
        _create_view(bind, EXEC, view, base_table, include_money=False)

    print(f"[v3] created {len(PERSONA_VIEWS)} views in each of {HOD} and {EXEC}")


def downgrade() -> None:
    for schema in (HOD, EXEC):
        for view in PERSONA_VIEWS:
            op.execute(f"DROP VIEW IF EXISTS {qualified(schema, view)} CASCADE")
