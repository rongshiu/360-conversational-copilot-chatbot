# app/service/principal_service.py
"""Turn a request's PermissionContext into the caller's persona.

This module used to do two jobs. The first -- expanding granted category nodes
through dim_product_category_closure into the full set of visible keys, so an RLS
predicate could be a single `= ANY(...)` -- is gone with the grants themselves, and
took the closure table, the expansion cache, the name-to-key resolver and the
group-entity promotion with it.

What is left is the second job, minus the scope: decide which persona schema the
caller reads. There is exactly one access control in the system now, and it is
whether money columns exist in that schema.

No database round-trip is needed for that, which is why this no longer takes a
session. The old flow opened system_scope() first -- as ci_loader, because you
cannot resolve a caller's scope while already inside it -- resolved the principal,
then reopened as the read role. The request path now enters copilot_scope()
directly, switching straight into the caller's persona role.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core import settings
from app.db.v3_ddl import PERSONA_ROLES, PERSONA_SCHEMAS
from app.models.requests.copilot import PermissionContext


@dataclass(frozen=True)
class PrincipalContext:
    """Everything the request path needs to enforce and explain the caller's access."""

    principal_id: str
    role_level: str

    @property
    def persona_schema(self) -> str:
        """The schema whose views this caller reads.

        The fallback is deliberate and is the last default-deny left in the request
        path: an unrecognised role level resolves to the executive schema, where the
        money columns do not exist. A broken role_level therefore loses revenue
        rather than gaining it.

        assert_scope_active() raises when role_level is unset, so this fallback
        catches a value that is present but unknown -- a new role added upstream
        before it is added here -- rather than a missing one.
        """
        return PERSONA_SCHEMAS.get(self.role_level, settings.ci_exec_schema)

    @property
    def persona_role(self) -> str:
        """The database role this caller's queries run as.

        Same default-deny as persona_schema, and it has to be: the role and the
        schema are a matched pair, and a caller switched into ci_copilot_exec while
        ci_hod is on the search_path gets "permission denied for view", not money.
        An unrecognised role level therefore resolves to the executive role, which
        can read exactly the schema the executive fallback selects.
        """
        return PERSONA_ROLES.get(self.role_level, settings.ci_copilot_exec_role)

    @property
    def can_see_money(self) -> bool:
        return self.role_level == "HOD"

    def scope_signature(self) -> str:
        """Stable key for per-persona caches (entity dictionary, glossary view).

        This used to be `opcos|categories|role`, so every distinct grant built and
        held its own copy of the ~6,000-row lookup dictionary and its own filtered
        glossary. There are now two possible values, so both caches collapse to one
        entry per role level.
        """
        return self.role_level

    def describe_scope(self) -> str:
        """One line for prompts and out-of-scope explanations."""
        money = (
            "including revenue measures"
            if self.can_see_money
            else "excluding revenue measures"
        )
        return f"all OpCos and all categories; {money}"


def resolve_principal(permission: PermissionContext) -> PrincipalContext:
    """Build the caller's context from a validated permission block.

    Synchronous and total: there is nothing left to look up and nothing left to
    reject. PermissionContext already validates role_level against the RoleLevel
    literal, so an unknown value is a 422 at the edge rather than a PermissionError
    here.
    """
    return PrincipalContext(
        principal_id=permission.principal_id,
        role_level=permission.role_level,
    )
