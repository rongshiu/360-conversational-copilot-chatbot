"""v3 foundation: serving schemas, extensions, role verification

Creates the four v3 schemas and the extensions the schema depends on.

Role CREATION is deliberately NOT here -- see scripts/bootstrap_roles.sql. It
needs CREATEROLE or superuser, and the migration user must be neither: a
superuser silently bypasses every RLS policy, so OpCo isolation would stop being
enforced with no error at query time. This revision only verifies the role
exists and grants it schema usage.

Extension handling is defensive: pg_trgm is required; ltree is probed and
category_path degrades to text when absent, so `alembic upgrade head` works on a
stock postgres image.

No postgres_hll. Distinct customer counts are exact, computed with
COUNT(DISTINCT customer_key) against the monthly customer tables, so there are no
sketch columns and no third-party extension to install.

This is the base revision. The v2 serving migrations were deleted rather than
superseded: the database is provisioned fresh, and every v2 table carried
customer_id as a key column, which is the exact property v3 exists to make
structurally impossible. There was nothing to preserve and nothing to drop.

Revision ID: 20260727_01
Revises: None
Create Date: 2026-07-27
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
    LOADER_ROLE,
    extension_available,
    quote_ident,
    safe_ident,
)

revision: str = "20260727_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_NAME = safe_ident(COPILOT_ROLE, fallback="ci_copilot")
LOADER_NAME = safe_ident(LOADER_ROLE, fallback="ci_loader")
ANALYST_NAME = safe_ident(ANALYST_ROLE, fallback="ci_analyst")


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # Role must already exist. Fail loudly with the exact fix.
    # ------------------------------------------------------------------
    bootstrap_hint = (
        "Run once as a superuser, then re-run this migration:\n\n"
        '    psql "$SUPERUSER_URI" \\\n'
        f"         -v app_user={settings.db_user} \\\n"
        f"         -v copilot_role={ROLE_NAME} -v loader_role={LOADER_NAME} \\\n"
        f"         -v analyst_role={ANALYST_NAME} \\\n"
        "         -f scripts/bootstrap_roles.sql\n"
    )

    for role_name, purpose in (
        (ROLE_NAME, "read path, scoped by RLS"),
        (LOADER_NAME, "write path, permissive RLS policy"),
        (ANALYST_NAME, "read-only human role, all-OpCo SELECT policy"),
    ):
        exists = bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"),
            {"r": role_name},
        ).scalar()
        if not exists:
            raise RuntimeError(
                f"\nRole {role_name!r} ({purpose}) does not exist.\n\n"
                "Roles are created out-of-band because CREATE ROLE needs superuser, "
                "and the migration user must NOT be superuser -- a superuser bypasses "
                "RLS, so OpCo isolation would silently stop being enforced.\n\n"
                + bootstrap_hint
            )

        if role_name == ANALYST_NAME:
            # A human account, not one the app switches into, so membership is
            # neither needed nor wanted -- see bootstrap_roles.sql.
            continue

        can_set_role = bind.execute(
            sa.text("SELECT pg_has_role(CURRENT_USER, :r, 'MEMBER')"), {"r": role_name}
        ).scalar()
        if not can_set_role:
            raise RuntimeError(
                f"\nCURRENT_USER is not a member of {role_name!r}, so the application "
                f"cannot SET LOCAL ROLE into it.\n\nRun as superuser:\n\n"
                f"    GRANT {role_name} TO {settings.db_user};\n"
            )

    # ------------------------------------------------------------------
    # Schemas
    # ------------------------------------------------------------------
    for schema in CI_SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}")

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    # Required: powers fuzzy entity resolution over the lookup catalog.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    if extension_available(bind, "ltree"):
        op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    else:
        print(
            "[v3] ltree unavailable -> dim_product_category.category_path becomes text. "
            "Subtree operators (<@) will not work; the l1..l4 ancestor keys remain the "
            "enforcement mechanism either way, so nothing about isolation changes."
        )

    # ------------------------------------------------------------------
    # Schema usage. Table grants are issued per object in 20260727_06, so
    # nothing is readable until a policy exists to constrain it.
    # ------------------------------------------------------------------
    # Both roles need schema USAGE before any table grant can take effect.
    for schema in CI_SCHEMAS:
        for role in (quote_ident(ROLE_NAME), quote_ident(LOADER_NAME)):
            op.execute(f"GRANT USAGE ON SCHEMA {quote_ident(schema)} TO {role}")
        # Anything created later in these schemas stays unreadable by default.
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quote_ident(schema)} "
            f"REVOKE ALL ON TABLES FROM PUBLIC"
        )

    # ------------------------------------------------------------------
    # Warn if the migration user itself can bypass RLS. Harmless for DDL, but
    # it usually means the runtime user is the same account.
    # ------------------------------------------------------------------
    is_super = bind.execute(
        sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = CURRENT_USER")
    ).scalar()
    if is_super:
        print(
            "\n[v3] WARNING: the migration user is superuser or has BYPASSRLS.\n"
            "     Fine for DDL, but the RUNTIME login user must be neither, or\n"
            "     OpCo and category isolation will not be enforced. FORCE ROW LEVEL\n"
            "     SECURITY is set on every scoped table so mere ownership does not\n"
            "     bypass policies -- but superuser still does.\n"
        )


def downgrade() -> None:
    role = quote_ident(ROLE_NAME)
    for schema in reversed(CI_SCHEMAS):
        op.execute(f"REVOKE USAGE ON SCHEMA {quote_ident(schema)} FROM {role}")
        op.execute(f"DROP SCHEMA IF EXISTS {quote_ident(schema)} CASCADE")
    # The role itself is cluster state owned by bootstrap_roles.sql, not by this
    # migration, so it is intentionally left in place.
