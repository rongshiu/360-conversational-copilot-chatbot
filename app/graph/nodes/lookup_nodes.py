# app/graph/nodes/lookup_nodes.py
from __future__ import annotations

from app.agents.answer_agent import build_clarify_response
from app.service.stream_context import emit_status
from app.core.logging import Logger
from app.graph.copilot_state import CopilotState
from app.service.entity_resolution.models import (
    public_lookup_context,
    public_lookup_plan,
    public_lookup_slots,
)
from app.db.v3_ddl import CATEGORY_DEPTH
from app.service.access_policy import money_denial, money_substitution, to_payload
from app.service.metric_service import get_metric_service
from app.utils.common import sanitize_for_json
from app.utils.schema_utils import generic_fallback_clarification

logger = Logger.get_logger(__name__)

# Deepest category level the customer bridge stores. Read from the DDL rather
# than hardcoded, so widening the bridge updates this automatically.
CUSTOMER_CATEGORY_DEPTH = CATEGORY_DEPTH["bridge_customer_category_monthly"]


class LookupNodesMixin:
    """Resolve entity mentions to canonical, in-scope filter values.

    Cross-turn state is carried entirely by the resolution plan in
    `lookup_plan`. The v2 design also threaded `selected_lookup_selections`,
    `inherited_lookup_matches`, `inherited_lookup_context` and
    `inherited_lookup_scopes` through state and merged them here; the plan's
    slots already hold every resolved entity, so all of that is gone.
    """

    async def resolve_lookup_entities(self, state: CopilotState) -> CopilotState:
        await emit_status("resolving stores and product categories")
        principal = state.get("principal") or self.principal
        if principal is None:
            # Should be unreachable: the graph only runs SQL routes inside
            # copilot_scope, which requires a principal. Fail loudly rather than
            # resolving against an unscoped dictionary.
            raise RuntimeError(
                "resolve_lookup_entities reached without a principal; entity "
                "resolution must run inside the caller's scope."
            )

        # Identity questions ("who bought X", "give me 5 example customers") are NOT
        # caught here any more. A regex over the question could only match the
        # phrasings someone had thought of, and every miss was refused by the planner
        # instead -- correctly, but reported differently, because a different layer
        # had made the call. The planner now classifies it as
        # SqlPlan.unsupported_cause, which handles arbitrary wording, and
        # route_after_plan sends it to the same refusal terminal this used.

        # Role-level money check, before entity resolution and before the planner.
        #
        # It sits on the analytics path rather than in load_context on purpose. An
        # executive asking "what does GMV mean" is a glossary question about a money
        # metric and must still be answered; only a request to COMPUTE one is
        # affected, and that is what routes through here.
        #
        # Nothing downstream can report this. The money columns, metrics and tables
        # are already filtered out of the planner's prompt, so by planning time there
        # is no trace that revenue was ever asked for -- the planner simply writes a
        # transactions query and the answer reads like a complete one.
        policies = list(state.get("applied_policies") or [])
        withheld = get_metric_service().withheld_for(state["query"], principal)
        if withheld is not None:
            if not withheld.has_substitute:
                return {
                    "lookup_matches": [],
                    "lookup_context": "",
                    "lookup_plan": state.get("lookup_plan"),
                    "lookup_needs_clarification": False,
                    "out_of_scope_code": "role_money_withheld",
                    "out_of_scope_reason": money_denial(principal).message,
                    "applied_policies": policies,
                }
            policies.append(
                money_substitution(
                    principal,
                    requested_metric=withheld.requested.metric_key,
                    substitute_metric=withheld.substitute.metric_key,
                ).to_dict()
            )

        try:
            resolved = await self.entity_resolver.resolve(
                self.db,
                state["query"],
                principal=principal,
                previous_plan=state.get("lookup_plan"),
                selected_option=state.get("clarification_selected_option"),
                entity_phrases=state.get("entity_phrases"),
            )
        except Exception as exc:
            # Block SQL planning rather than let unresolved raw phrases reach the
            # planner, where they would become invented literal filters.
            logger.exception("Entity resolution failed: %s", exc)

            # A failed statement leaves the transaction aborted, so every later
            # command on this session -- including the scope teardown -- would fail
            # too and bury this error. Roll back so the rest of the turn still works.
            try:
                if self.db.in_transaction():
                    await self.db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("rollback after resolution failure failed", exc_info=True)

            return {
                "lookup_matches": [],
                "lookup_context": "",
                "lookup_plan": state.get("lookup_plan"),
                "lookup_needs_clarification": True,
                "lookup_clarification_question": (
                    "I ran into a problem matching one of the store or product names in "
                    "your question. Please try again using the exact name."
                ),
                "applied_policies": policies,
            }

        matches = [
            slot.model_dump()
            for slot in resolved.plan.slots
            if slot.status == "resolved"
        ]

        return {
            "lookup_matches": sanitize_for_json(matches),
            "lookup_context": self._planner_context(resolved, principal),
            "lookup_plan": sanitize_for_json(resolved.plan.to_public_dict()),
            "lookup_needs_clarification": resolved.needs_clarification,
            "lookup_clarification_question": resolved.clarification_question,
            # No entity is out of scope any more, so entity resolution never
            # produces a refusal. The out_of_scope_* keys stay in the state because
            # the money path above still sets them -- an EXEC asking for revenue
            # with no volume equivalent is now the only way to reach that route.
            "applied_policies": policies,
        }

    @staticmethod
    def _category_depth_notes(resolved) -> list[str]:
        """Warn when a resolved category is deeper than customer data goes.

        The customer bridge now carries every level, so this fires only if that
        depth is ever reduced again -- CUSTOMER_CATEGORY_DEPTH is read from
        CATEGORY_DEPTH rather than written here, so the guard tracks the
        schema instead of asserting a number.

        It is kept because the failure it caught was severe and silent: while the
        bridge held levels 1-2, "how many customers buys baby products" rolled BABY
        (level 4) up to its level-2 ancestor and reported 132 customers as BABY
        buyers, when that was the count for the whole NON FOOD & HBC division. A
        roll-up is a fine answer; presenting it as the leaf is not.

        The ancestor label comes from the resolved display path, so this needs no
        extra query.
        """
        notes: list[str] = []
        for slot in resolved.plan.resolved:
            if not slot.entity_class.startswith("category_l"):
                continue
            try:
                level = int(slot.entity_class.rsplit("l", 1)[1])
            except (IndexError, ValueError):
                continue
            if level <= CUSTOMER_CATEGORY_DEPTH:
                continue

            path = [p.strip() for p in (slot.display_value or "").split(">") if p.strip()]
            leaf = path[-1] if path else slot.phrase
            ancestor = (
                path[CUSTOMER_CATEGORY_DEPTH - 1]
                if len(path) >= CUSTOMER_CATEGORY_DEPTH
                else ""
            )
            notes.append(
                f'CATEGORY DEPTH LIMIT: "{slot.phrase}" resolves to "{leaf}", a level '
                f"{level} category. Customer tables store categories only to level "
                f"{CUSTOMER_CATEGORY_DEPTH}, so there is NO distinct-customer count "
                f'for "{leaf}". Either answer with transactions or quantity, which are '
                f"available at deeper levels, or roll up to its level "
                f"{CUSTOMER_CATEGORY_DEPTH} ancestor"
                + (f' "{ancestor}"' if ancestor else "")
                + ". If you roll up, the answer MUST name "
                + (f'"{ancestor}"' if ancestor else "the broader category")
                + f' rather than "{leaf}", because the figure covers the whole '
                "division and attributing it to the narrower category overstates it."
            )
        return notes

    @staticmethod
    def _cross_opco_category_notes(resolved) -> list[str]:
        """Warn when a resolved category cannot cover every resolved store.

        A category key belongs to ONE OpCo -- NorthCo Mart's Grocery and NorthCo's
        FOOD > GROCERY are different nodes with different keys. Comparing a store in
        each is a perfectly ordinary question ("Veldra Selbycross versus Gledeholt Vantryton"),
        and the resolver now allows it, because two stores are alternatives rather
        than a conjunction. But the category resolved against only one of them, so
        the other side filters a key no row in that OpCo carries.

        The result is a comparison with one side silently empty: "NorthCo Mart VELDRA
        SELBYCROSS generated RM 7,883.52 ... no records found for GLEDEHOLT VANTRYTON" reads as a
        business fact about Gledeholt Vantryton when it is an artefact of which OpCo the
        category came from. Say so, so the answer either widens or admits the gap.
        """
        stores = [
            slot
            for slot in resolved.plan.resolved
            if slot.entity_class == "store" and slot.opco_code
        ]
        categories = [
            slot
            for slot in resolved.plan.resolved
            if slot.entity_class.startswith("category_") and slot.opco_code
        ]
        if not stores or not categories:
            return []

        notes: list[str] = []
        for category in categories:
            uncovered = sorted(
                {s.opco_code for s in stores if s.opco_code != category.opco_code}
            )
            if not uncovered:
                continue
            notes.append(
                f'CROSS-OPCO CATEGORY LIMIT: "{category.phrase}" resolved to '
                f"{category.target_column} = {category.canonical_value} "
                f'("{category.display_value}"), which exists only in '
                f"{category.opco_code}. The question also names a store in "
                f"{', '.join(uncovered)}, where that key matches no row, so filtering "
                "on it returns an empty side rather than a zero. "
                "ANSWER THE QUESTION -- do not ask which category to use for the "
                "other OpCo, because the user named one category and the catalogue "
                "simply keys it per OpCo. Drop the category filter, compare the "
                "stores on total revenue and penetration, and say in `reason` that "
                f'"{category.phrase}" covers only {category.opco_code} so the '
                "comparison is store-wide. Reporting the uncovered side as zero "
                "activity is the one thing that is wrong."
            )
        return notes

    @staticmethod
    def _planner_context(resolved, principal) -> str:
        """Resolved filters, plus the overlap constraint when one applies.

        A question naming a foreign OpCo alongside a granted one is allowed
        through as a candidate customer-overlap count. The planner has to be told
        what it may do with that OpCo, because the only safe use is an array
        membership test on the group view -- using it as `opco_code = '...'` on a
        scoped fact would return nothing and read as a genuine zero.
        """
        context = resolved.filter_context()

        # The CROSS-OPCO OVERLAP CONSTRAINT directive used to be appended here when
        # a question named an OpCo outside the caller's grant alongside one inside
        # it. It confined that code to an array-membership test on the group view,
        # because any other use returned an empty result that looked like a real
        # zero. Every OpCo is now queryable directly, and the group view is gone --
        # overlap is active_opco_codes on v_customer_opco_monthly, which the
        # planner prompt covers.
        blocks = [
            b
            for b in [
                context,
                *LookupNodesMixin._category_depth_notes(resolved),
                *LookupNodesMixin._cross_opco_category_notes(resolved),
            ]
            if b
        ]
        return "\n\n".join(blocks)

    def route_after_lookup_resolution(self, state: CopilotState) -> str:
        if state.get("out_of_scope_reason"):
            return "out_of_scope"
        if state.get("lookup_needs_clarification"):
            return "clarify_from_lookup"
        return "make_plan"

    async def clarify_from_lookup(self, state: CopilotState) -> CopilotState:
        reason = " | ".join(
            x
            for x in [
                state.get("intent_reason"),
                "Entity resolution needs clarification.",
            ]
            if x
        )
        question = state.get("lookup_clarification_question") or generic_fallback_clarification(state)
        result = build_clarify_response(question, reason=reason)

        result["lookup_matches"] = sanitize_for_json(
            public_lookup_slots(state.get("lookup_matches") or [])
        )
        result["lookup_context"] = public_lookup_context(
            state.get("lookup_plan"),
            state.get("lookup_context") or "",
        )
        if state.get("lookup_plan"):
            result["lookup_plan"] = sanitize_for_json(
                public_lookup_plan(state.get("lookup_plan"))
            )

        result["policies"] = to_payload(state.get("applied_policies") or [])

        return {"result": result}
