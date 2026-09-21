# app/service/access_policy.py
"""Structured record of every policy that shaped an answer.

An authorization outcome used to reach the caller as prose and nothing else.
Two problems with that.

The first is that prose is not actionable. A client cannot tell "this is outside
your scope" from "there is no data for that period" without parsing an English
sentence, so a refusal and an empty result render identically -- which is the
confident-zero failure the schema is built to prevent, moved up one layer.

The second is that some outcomes were not reported at all. A revenue question
from an EXEC produced a volume answer with nothing saying a substitution had
happened, which was invisible to the client.

These notices are the only record of an access decision the CALLER receives.
ci_meta.copilot_access_log still records the same decisions server-side -- it lost
the three columns that described a grant scope which no longer exists, but the
table itself was kept deliberately; see app/service/access_log_service.py. What is
in neither is in the MLflow trace or nowhere.

So every policy that changes what the caller receives emits one of these,
whichever way it went:

    denied      -- the question was refused
    substituted -- it was answered, but with a different measure

There was a third, `suppressed`, for small-cell suppression: counts of fewer than
CI_MIN_CELL_SIZE customers were blanked, because a narrow enough filter identifies
a person without naming one ("Fashion buyers at store 2032, Morning, 3 June ->
1"). That control was removed deliberately. What still holds is structural and did
not depend on it: the sales facts carry no customer key, the customer tables expose
only an opaque surrogate the validator permits solely inside COUNT(DISTINCT), and
a request to identify or list individuals is refused outright. What is no longer
true is that a count of 1 is withheld -- it is now answered.

`effect` is the field a client should branch on. `code` is stable and safe to key
on. `message` is for humans and may be reworded without notice.

One rule used to constrain what may go in here: a policy notice never named the
entity that was excluded, because the entity dictionary was RLS-scoped so that an
out-of-scope store or category could not be confirmed to exist, and echoing one
back inside a machine-readable denial would have been that disclosure with a
schema attached. There are no out-of-scope entities left, so the rule no longer
binds -- but the habit is worth keeping. A notice describes what happened to the
ANSWER; it is not a channel for enumerating the catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Effects. A client branches on these; they are a closed set.
#
# SUPPRESSED was a third, emitted when small-cell suppression blanked counts of
# fewer than CI_MIN_CELL_SIZE customers. That control is gone -- see the note
# below -- and nothing raises the effect, so it is removed rather than kept as a
# value that would never be sent.
DENIED = "denied"
SUBSTITUTED = "substituted"
# WIDENED was a fourth effect, for the case where a caller received MORE than the
# permission block asked for -- naming the group entity in opco_codes promoted the
# request to group-wide access, and a silent widening is the one nobody audits for.
# There is no scope left to widen, so it is gone with the promotion branch.

# code -> the mechanism that actually enforces it.
#
# Stated in the payload because "why can't I see this" has very different answers
# for "your grant excludes those rows" and "that column does not exist for your
# role", and whoever fields the question should not have to read the source to
# tell them apart.
# opco_out_of_scope and category_out_of_scope were here, both enforced by RLS.
# Neither can occur now: every caller sees every OpCo and every category, so a
# question naming one is answered rather than refused.
ENFORCED_BY: dict[str, str] = {
    "customer_identity": "schema.no_customer_key",
    "role_money_withheld": "persona_view.ci_exec",
}


@dataclass(frozen=True)
class AppliedPolicy:
    code: str
    effect: str
    message: str
    scope: dict[str, Any]
    # The metric the question asked for, and the one used instead. Both are
    # metric_definition keys, and both are safe to name: a metric definition is not
    # scoped data, and the planner prompt already lists the withheld ones by name
    # to an EXEC caller. Only ever set on a substitution.
    requested_metric: Optional[str] = None
    substituted_with: Optional[str] = None

    @property
    def enforced_by(self) -> str:
        return ENFORCED_BY.get(self.code, "")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "effect": self.effect,
            "enforced_by": self.enforced_by,
            "message": self.message,
            "scope": self.scope,
        }
        if self.requested_metric:
            payload["requested_metric"] = self.requested_metric
        if self.substituted_with:
            payload["substituted_with"] = self.substituted_with
        return payload


def scope_of(principal: Any) -> dict[str, Any]:
    """The caller's own grant, as they were provisioned with it."""
    if principal is None:
        return {}
    # role_level is the whole of the caller's grant now. opco_codes,
    # is_group_user and category_names were reported here while they restricted
    # rows; reporting them as constants would tell a client its access is scoped
    # when it is not.
    return {"role_level": getattr(principal, "role_level", None)}


def denial(code: str, message: str, principal: Any) -> AppliedPolicy:
    return AppliedPolicy(
        code=code,
        effect=DENIED,
        message=message,
        scope=scope_of(principal),
    )


def money_substitution(
    principal: Any,
    *,
    requested_metric: str,
    substitute_metric: str,
) -> AppliedPolicy:
    """An EXEC asked for revenue and was answered with a volume measure instead.

    Reported rather than refused because the substitution is a real answer to the
    question behind the question -- the metric registry ships executive-safe twins
    (best_daypart_by_txn, avg_basket_size_units) precisely so this path exists.
    What was missing was saying so: a transactions figure narrated without comment
    reads exactly like the revenue figure that was asked for.
    """
    return AppliedPolicy(
        code="role_money_withheld",
        effect=SUBSTITUTED,
        message=(
            "Revenue measures are not available at your role level, so this answer "
            "uses a volume measure instead. The figure is not money."
        ),
        scope=scope_of(principal),
        requested_metric=requested_metric,
        substituted_with=substitute_metric,
    )


def money_denial(principal: Any) -> AppliedPolicy:
    """An EXEC asked for revenue and no volume measure answers the question.

    "Average basket VALUE" has no honest volume equivalent -- units per basket is
    a different quantity, not the same one in other clothes -- so substituting
    silently would answer a question nobody asked.
    """
    return AppliedPolicy(
        code="role_money_withheld",
        effect=DENIED,
        message=(
            "That question can only be answered with revenue, which is not "
            "available at your role level."
        ),
        scope=scope_of(principal),
    )


def to_payload(policies: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize a mixed list of AppliedPolicy and already-serialised dicts.

    State travels through LangGraph as plain JSON, so a policy set at one node
    arrives at the terminal as a dict while one built in the terminal itself is
    still a dataclass.
    """
    out: list[dict[str, Any]] = []
    for policy in policies or ():
        if isinstance(policy, AppliedPolicy):
            out.append(policy.to_dict())
        elif isinstance(policy, dict) and policy.get("code"):
            out.append(policy)
    return out
