from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RoleLevel = Literal["EXEC", "HOD"]


class PermissionContext(BaseModel):
    """Caller authorization, supplied by the calling service on every request.

    This is the sole source of truth for what the request may see. Nothing is
    inferred from the user id, and there is no server-side user table to fall back
    on -- an absent or malformed permission block is a hard 403, never a silent
    widening.

    The copilot resolves this into a persona schema on the search_path: HOD reads
    ci_hod, where the revenue columns exist, and EXEC reads ci_exec, where they do
    not. That is the whole of access control.

    NOT PART OF THIS BLOCK. An earlier design carried
    `opco_codes`, `category_keys` and `category_names`, and the access model
    restricted rows by all three. Access is no longer scoped by OpCo or by product
    category, so all three fields are removed rather than kept as fields that look
    like they restrict something and do not -- a payload that lies about what will
    be enforced is worse than one that breaks loudly.

    Because of extra="forbid" below, a caller still sending any of them receives a
    422 naming the field. That is the intended behaviour: an authorization payload
    is the last place to silently drop a field nobody recognises. The same rule
    already caught callers sending the retired `is_group_user` flag.
    """

    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(
        min_length=1,
        max_length=255,
        description="Stable identifier for the caller. Recorded on the trace.",
    )

    role_level: RoleLevel = Field(
        description=(
            "HOD sees revenue-bearing measures (sales, GMV, basket value). EXEC "
            "sees volume measures only -- money columns are physically absent "
            "from the views it reads, so a query touching one fails loudly "
            "instead of returning zeros. This is the only access control in the "
            "system."
        ),
    )


class CopilotAskRequest(BaseModel):
    query: str = Field(..., min_length=1)
    thread_id: Optional[str] = None
    permission: PermissionContext
