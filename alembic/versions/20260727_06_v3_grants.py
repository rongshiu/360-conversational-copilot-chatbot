"""v3 grants: who may read what

This revision used to install row-level security -- eleven policy sets, FORCE ROW
LEVEL SECURITY on every scoped table, and predicates reading six session GUCs. It
opened with the line "this is the actual enforcement layer; the LLM cannot write
its way out of these policies", and that was true.

Access is no longer scoped by OpCo or by product category. What remains is the
role split, and the role split has never been enforced here: it is enforced by
which columns exist in the persona view the caller's search_path resolves to. So
this revision is now what its name says -- grants, and nothing else.

WHAT WAS LOST, stated plainly because it does not show up as a failing test:

  The system no longer fails closed. FORCE ROW LEVEL SECURITY removed even the
  table owner's exemption, so a code path that forgot to set the caller's scope
  saw ZERO rows rather than all four OpCos. There is no scope to forget now, so
  nothing is protected by forgetting it -- but equally, a bug in the request path
  surfaces as a wrong answer rather than an empty one.

WHAT STILL HOLDS:

  The privilege split survives and is not decoration. The login user holds no
  grants at all; reads go through ci_copilot and writes through ci_loader, both
  non-login roles reached by SET LOCAL ROLE. A session that does nothing still
  sees nothing, and the copilot role still cannot write.

  ci_copilot gets SELECT on the base tables because the persona views are
  security_invoker and therefore execute as the caller. It is the SQL validator,
  not a grant, that stops generated SQL naming a base table -- which produces a
  useful retry message instead of a permission error.

SUPERSEDED IN PART BY 20260828_09. That last paragraph described a real hole: a
grant the views forced, and a validator as the only thing standing on it. Revision
09 splits ci_copilot into ci_copilot_hod and ci_copilot_exec, drops
security_invoker so the views run as their owner, and revokes every base-table
grant this revision issues to the read role. The grants below are left as they
were, because rewriting an applied revision is how a database and its history stop
matching -- read 09 for the state that is live.

Revision ID: 20260727_06
Revises: 20260727_05
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.v3_ddl import (
    ANALYST_ROLE,
    COPILOT_ROLE,
    CORE,
    CORE_TABLES,
    LOADER_ROLE,
    META,
    META_TABLES,
    qualified,
    quote_ident,
    safe_ident,
)

revision: str = "20260727_06"
down_revision: Union[str, Sequence[str], None] = "20260727_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE = quote_ident(safe_ident(COPILOT_ROLE, fallback="ci_copilot"))
LOADER = quote_ident(safe_ident(LOADER_ROLE, fallback="ci_loader"))
ANALYST = quote_ident(safe_ident(ANALYST_ROLE, fallback="ci_analyst"))

WRITE_PRIVS = "SELECT, INSERT, UPDATE, DELETE, TRUNCATE"


def upgrade() -> None:
    # ------------------------------------------------------------ ci_core
    for table in CORE_TABLES:
        target = qualified(CORE, table)
        op.execute(f"GRANT SELECT ON {target} TO {ROLE}")
        op.execute(f"GRANT SELECT ON {target} TO {ANALYST}")
        op.execute(f"GRANT {WRITE_PRIVS} ON {target} TO {LOADER}")

    # ------------------------------------------------------------ ci_meta
    for table in META_TABLES:
        target = qualified(META, table)
        op.execute(f"GRANT SELECT ON {target} TO {ROLE}")
        op.execute(f"GRANT SELECT ON {target} TO {ANALYST}")
        op.execute(f"GRANT {WRITE_PRIVS} ON {target} TO {LOADER}")

    op.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quote_ident(META)} TO {LOADER}"
    )

    # copilot_access_log: the copilot read role gets NOTHING. A generated query
    # reaching the audit log is a bug, and the validator rejects the name before
    # the database is asked. The loader writes the rows; a human investigating who
    # ran what is exactly who the SELECT is for.
    audit = qualified(META, "copilot_access_log")
    op.execute(f"GRANT SELECT, INSERT ON {audit} TO {LOADER}")
    op.execute(f"GRANT SELECT ON {audit} TO {ANALYST}")
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE {qualified(META, 'copilot_access_log_id_seq')} TO {LOADER}"
    )

    # Schema USAGE is separate from table privileges: without it every SELECT fails
    # with "permission denied for schema" no matter what was granted above. The
    # copilot role gets its USAGE in revision 01, because it needs the persona
    # schemas on its search_path.
    op.execute(
        f"GRANT USAGE ON SCHEMA {quote_ident(CORE)}, {quote_ident(META)} TO {ANALYST}"
    )

    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote_ident(CORE)} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote_ident(META)} FROM PUBLIC")


def downgrade() -> None:
    for table in CORE_TABLES:
        target = qualified(CORE, table)
        op.execute(f"REVOKE ALL ON {target} FROM {ROLE}")
        op.execute(f"REVOKE ALL ON {target} FROM {ANALYST}")
        op.execute(f"REVOKE ALL ON {target} FROM {LOADER}")

    for table in (*META_TABLES, "copilot_access_log"):
        target = qualified(META, table)
        op.execute(f"REVOKE ALL ON {target} FROM {ROLE}")
        op.execute(f"REVOKE ALL ON {target} FROM {ANALYST}")
        op.execute(f"REVOKE ALL ON {target} FROM {LOADER}")

    op.execute(
        f"REVOKE USAGE ON SCHEMA {quote_ident(CORE)}, {quote_ident(META)} FROM {ANALYST}"
    )
