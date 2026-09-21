"""Policy notices: the structured half of an authorization outcome.

The behaviour under test is not "does a denial happen" -- RLS and the persona
views guarantee that regardless. It is whether the caller is TOLD, in a form a
client can branch on, and whether the notice stays inside the disclosure rule:
it may state the caller's own grant and must never name what was excluded.
"""
from __future__ import annotations

import unittest

import pytest

from app.agents.sql_validator_agent import validate_sql

from app.models.responses import PolicyNotice
from app.service.access_policy import (
    DENIED,
    SUBSTITUTED,
    denial,
    money_denial,
    money_substitution,
    scope_of,
    to_payload,
)
from app.service.metric_service import get_metric_service


class FakePrincipal:
    """Mirrors PrincipalContext, which is now role_level and nothing else."""

    def __init__(self, role_level="HOD"):
        self.role_level = role_level

    @property
    def can_see_money(self) -> bool:
        return self.role_level == "HOD"

    def describe_scope(self) -> str:
        money = "including" if self.can_see_money else "excluding"
        return f"all OpCos and all categories; {money} revenue measures"


HOD = FakePrincipal("HOD")
EXEC = FakePrincipal("EXEC")


# ---------------------------------------------------------------- scope shape


def test_scope_reports_only_what_was_granted():
    assert scope_of(FakePrincipal("EXEC")) == {"role_level": "EXEC"}


def test_scope_does_not_report_opco_or_category_as_constants():
    """Reporting them would tell a client its access is scoped when it is not.

    opco_codes, is_group_user and category_names were in this payload while they
    restricted rows. Access is the role split alone now, so anything else here
    would be describing a control that no longer exists.
    """
    scope = scope_of(FakePrincipal("HOD"))
    assert set(scope) == {"role_level"}


# ------------------------------------------------------------ the deny notice


def test_a_denial_carries_a_machine_readable_code_and_mechanism():
    policy = denial(
        "customer_identity", "There is no column that identifies a customer.", HOD
    )
    assert policy.effect == DENIED
    assert policy.code == "customer_identity"
    assert policy.enforced_by == "schema.no_customer_key"


def test_the_retired_scope_codes_no_longer_name_a_mechanism():
    """opco_out_of_scope and category_out_of_scope were enforced by RLS.

    Nothing raises them now. If one reappears it should surface as an unmapped
    code rather than claiming an enforcement mechanism the schema does not have.
    """
    assert denial("opco_out_of_scope", "x", HOD).enforced_by == ""
    assert denial("category_out_of_scope", "x", HOD).enforced_by == ""


def test_money_denial_points_at_the_persona_view_not_at_rls():
    """The two failures are different and support should not have to guess which."""
    assert money_denial(EXEC).enforced_by == "persona_view.ci_exec"
    assert money_denial(EXEC).effect == DENIED


def test_a_notice_does_not_enumerate_the_catalogue():
    """No entity is out of scope now, so this is a habit rather than a control.

    It is still worth holding: a policy notice describes what happened to the
    ANSWER, and is not a channel for listing OpCos or categories back to a client.
    """
    payload = denial(
        "customer_identity",
        "There is no column that identifies a customer.",
        FakePrincipal("HOD"),
    ).to_dict()

    serialized = repr(payload)
    assert "NORTHCO_CREDIT" not in serialized
    assert "NORTHCO_MART" not in serialized
    assert payload["scope"] == {"role_level": "HOD"}


# ---------------------------------------------------------- the substitution


def test_substitution_is_reported_as_answered_not_denied():
    policy = money_substitution(
        EXEC, requested_metric="total_sales", substitute_metric="total_transactions"
    )
    assert policy.effect == SUBSTITUTED
    payload = policy.to_dict()
    assert payload["requested_metric"] == "total_sales"
    assert payload["substituted_with"] == "total_transactions"


# ------------------------------------------------- metric-side detection


def test_hod_asking_for_revenue_triggers_no_notice():
    assert get_metric_service().withheld_for("total sales last month", HOD) is None


def test_exec_asking_for_revenue_is_detected_with_a_volume_substitute():
    """resolve() alone cannot see this: it searches only within the caller's own
    set, so an executive gets the volume twin and no trace remains that revenue
    was ever asked for."""
    withheld = get_metric_service().withheld_for("best daypart last month", EXEC)
    assert withheld is not None
    assert withheld.requested.metric_key == "best_daypart_by_sales"
    assert withheld.has_substitute
    assert withheld.substitute.metric_key == "best_daypart_by_txn"


def test_a_twin_is_found_even_when_the_phrase_matches_no_synonym_of_it():
    """The design point. Whether a twin exists decides substitute-vs-refuse, so it
    is derived from shared synonyms, not by re-running the fuzzy phrase matcher --
    otherwise a matcher MISS becomes a refusal of a legitimate question.

    "best performing daypart by sales" contains no synonym of best_daypart_by_txn
    as a substring, yet the two are twins and this must still substitute."""
    withheld = get_metric_service().withheld_for("best performing daypart by sales", EXEC)
    assert withheld is not None
    assert withheld.has_substitute, "a matcher miss must not become a denial"


def test_a_money_metric_with_no_volume_equivalent_has_no_substitute():
    """Average basket VALUE is the case that genuinely refuses: units per basket is
    a different quantity, not the same one in other clothes."""
    withheld = get_metric_service().withheld_for("average transaction value", EXEC)
    assert withheld is not None
    assert withheld.requested.metric_key == "avg_basket_size_value"
    assert not withheld.has_substitute


def test_exec_asking_a_volume_question_triggers_no_notice():
    assert get_metric_service().withheld_for("how many transactions in June", EXEC) is None


@pytest.mark.parametrize("phrase", ["what is the weather", "", "   "])
def test_unmatched_phrases_produce_no_notice(phrase):
    assert get_metric_service().withheld_for(phrase, EXEC) is None


def test_role_filtering_still_behaves_as_before_the_refactor():
    """_match/_prefer were split out of resolve(); resolve's contract must not move."""
    metrics = get_metric_service()
    hod_choice = metrics.resolve("best daypart", HOD)
    exec_choice = metrics.resolve("best daypart", EXEC)
    assert hod_choice is not None and hod_choice.needs_money
    assert exec_choice is not None and not exec_choice.needs_money


# ------------------------------------------------- load-order independence


def _reindexed(order):
    """A registry whose synonym index was built in a given metric order.

    Postgres loads `ORDER BY metric_key`; the CSV fallback loads in file order.
    Anything that depends on which came first is a bug that only appears in one
    of the two deployments.
    """
    from app.utils.text_utils import normalize_lookup_text

    live = get_metric_service()
    clone = live.__class__.__new__(live.__class__)
    clone.metrics = dict(live.metrics)
    clone.source = "test"
    clone._by_synonym = {}
    for key in order:
        metric = clone.metrics[key]
        for synonym in (metric.metric_name, *metric.synonyms):
            norm = normalize_lookup_text(synonym)
            if norm:
                clone._by_synonym.setdefault(norm, []).append(key)
    return clone


REPORTED_QUERY = (
    "What is the year-over-year combined revenue growth and active member overlap "
    "across NorthCo., NorthCo Mart, and NorthCo Credit in the Southern Region "
    "(Fenwickholt/Tannermere) in 2026?"
)


def test_equal_length_synonyms_resolve_the_same_under_both_load_orders():
    """The reported bug. `revenue` and `overlap` are both 7 characters, so the
    winner used to be whichever the registry indexed first -- total_sales from the
    CSV, cross_opco_overlap from Postgres. The deployed service therefore could not
    see that revenue had been asked for, and returned no policy notice."""
    keys = sorted(get_metric_service().metrics)
    by_key = _reindexed(keys)                 # Postgres: ORDER BY metric_key
    by_file = _reindexed(list(reversed(keys)))  # a different order entirely

    a = by_key.withheld_for(REPORTED_QUERY, EXEC)
    b = by_file.withheld_for(REPORTED_QUERY, EXEC)
    assert a is not None and b is not None
    assert a.requested.metric_key == b.requested.metric_key == "total_sales"
    assert a.substitute.metric_key == b.substitute.metric_key == "total_transactions"


def test_a_money_synonym_wins_a_tie_against_a_volume_one_when_detecting_withholding():
    """On a tie the money candidate must surface, or the request to compute revenue
    is invisible and the answer is narrated as if nothing was withheld."""
    withheld = get_metric_service().withheld_for("revenue and overlap in 2026", EXEC)
    assert withheld is not None
    assert withheld.requested.needs_money


@pytest.mark.parametrize(
    "phrase", ["best daypart", "membership penetration", "revenue and overlap in 2026"]
)
def test_resolution_is_stable_across_load_orders(phrase):
    keys = sorted(get_metric_service().metrics)
    forward, backward = _reindexed(keys), _reindexed(list(reversed(keys)))
    for principal in (HOD, EXEC):
        a = forward.resolve(phrase, principal)
        b = backward.resolve(phrase, principal)
        assert (a and a.metric_key) == (b and b.metric_key), phrase


# ------------------------------------------------ planner refusal classification


def test_the_guard_no_longer_pattern_matches_the_question():
    """The identity regex is gone, not merely bypassed.

    It could only recognise phrasings someone had thought of -- "give me example of
    5 customers" matched none of its patterns -- and each miss was refused by the
    planner instead, but reported differently because a different layer decided.
    Extending the pattern list buys the next phrasing and not the one after."""
    import app.service.query_shape_guard as guard

    assert not hasattr(guard, "is_customer_identity_question")
    assert not hasattr(guard, "_IDENTITY_QUESTION")
    assert not hasattr(guard, "_AGGREGATE_INTENT")


def test_an_identity_refusal_goes_to_the_same_terminal_the_regex_used():
    """Removing the regex changed which component decides, and nothing the caller
    sees: the response stays type "out_of_scope" with a customer_identity notice."""
    from app.graph.nodes.planning_nodes import PlanningNodesMixin

    route = PlanningNodesMixin.route_after_plan
    identity = {"plan_status": "unsupported", "unsupported_cause": "customer_identity"}
    schema_limit = {"plan_status": "unsupported", "unsupported_cause": "not_answerable"}

    assert route(None, identity) == "out_of_scope"
    assert route(None, schema_limit) == "unsupported_from_plan"


def test_only_causes_that_name_a_real_control_report_a_policy():
    """not_answerable is a schema limit for ANY caller, so there is nothing to name
    and a client must not offer "request access" for it.

    "out_of_scope" -> "opco_out_of_scope" was here too, and outlived the control it
    described: with no OpCo or category grants the cause cannot occur, and the code
    it produced was absent from PolicyNotice's union and from ENFORCED_BY. A client
    branching on `code` would have received a value the contract said it never
    would.
    """
    from app.graph.nodes.planning_nodes import UNSUPPORTED_CAUSE_POLICY

    assert UNSUPPORTED_CAUSE_POLICY["customer_identity"] == "customer_identity"
    assert "not_answerable" not in UNSUPPORTED_CAUSE_POLICY
    assert "out_of_scope" not in UNSUPPORTED_CAUSE_POLICY

    # Every code this map can emit must be one the response contract declares and
    # ENFORCED_BY can explain. That is the property the removed entry broke.
    from app.models.responses.copilot import PolicyNotice
    from app.service.access_policy import ENFORCED_BY

    declared = set(PolicyNotice.model_fields["code"].annotation.__args__)
    for code in UNSUPPORTED_CAUSE_POLICY.values():
        assert code in declared, f"{code} is not a PolicyNotice.code"
        assert ENFORCED_BY.get(code), f"{code} names no enforcement mechanism"


def test_unsupported_cause_defaults_to_the_claim_that_asserts_least():
    """A model that omits the field must not invent an authorization event."""
    from app.agents.planner_agent import SqlPlan

    plan = SqlPlan(status="unsupported", reason="x")
    assert plan.unsupported_cause == "not_answerable"


def test_unsupported_cause_is_cleared_when_the_plan_is_not_a_refusal():
    """It drives whether a denial is reported, so it must not survive on a plan
    that answered -- a model is free to fill the field in on any response."""
    from app.agents.planner_agent import SqlPlan, _normalize_plan

    plan = _normalize_plan(
        SqlPlan(
            status="answerable",
            sql="SELECT 1",
            reason="x",
            unsupported_cause="customer_identity",
        )
    )
    assert plan.unsupported_cause == "not_answerable"


def test_the_planner_cannot_refuse_for_a_scope_that_does_not_exist():
    """"out_of_scope" is not selectable any more.

    It meant "this needs an OpCo or category the caller was not granted". There are
    no grants, so leaving it in the union let the model refuse an answerable
    question by choosing a reason that cannot be true.
    """
    import pydantic
    import pytest as _pytest

    from app.agents.planner_agent import SqlPlan

    with _pytest.raises(pydantic.ValidationError):
        SqlPlan(status="unsupported", reason="x", unsupported_cause="out_of_scope")


# The "category scope refusal" section lived here. It tested that a category the
# caller's grant excluded came back as a refusal rather than "which category do you
# mean?" -- a distinction that existed only because an excluded category never
# entered the RLS-scoped dictionary and so arrived indistinguishable from a typo.
# Every category is now in every caller's dictionary, so an unresolved name IS a
# typo and a clarification is the correct handling.


# --------------------------------------------------------------- serialisation


def test_to_payload_accepts_both_dataclasses_and_already_serialised_dicts():
    """State crosses LangGraph as plain JSON, so a policy set at one node arrives
    at the terminal as a dict while one built in the terminal is still an object."""
    mixed = [
        denial("customer_identity", "No.", HOD),
        {"code": "role_money_withheld", "effect": SUBSTITUTED},
        None,
        {"no_code": True},
    ]
    payload = to_payload(mixed)
    assert [p["code"] for p in payload] == ["customer_identity", "role_money_withheld"]


def test_every_code_declares_an_enforcing_mechanism():
    """An empty enforced_by would leave "why can't I see this" unanswerable.

    Reads the codes off the response model, so removing one from the union (as the
    two RLS codes were) keeps this honest rather than asserting over a stale list.
    """
    for code in PolicyNotice.model_fields["code"].annotation.__args__:
        assert denial(code, "x", HOD).enforced_by, f"{code} has no enforcing mechanism"


def test_payload_validates_against_the_declared_response_model():
    for policy in (
        denial("customer_identity", "no", HOD),
        money_substitution(EXEC, requested_metric="total_sales", substitute_metric="total_transactions"),
    ):
        PolicyNotice.model_validate(policy.to_dict())


# ------------------------------------------------- customer grain (row != customer)


class CustomerGrainTests(unittest.TestCase):
    """A row count is not a customer count where a customer occupies many rows.

    This rule did not exist while row-level security did, and it did not need to:
    v_customer_opco_monthly was scoped to the caller's OpCos, so a single-OpCo
    caller -- the common case -- saw exactly one row per customer per month and
    COUNT(*) was right for them. Withdrawing the row filter made every caller see
    all four OpCos, which silently turned the common correct shape into a common
    wrong one.

    Nothing else in the validator catches it, because the query never mentions
    customer_key and the identity rule only fires on columns it can see.
    """

    def _reject(self, sql: str) -> None:
        result = validate_sql(sql)
        self.assertFalse(result.is_valid, f"should have been rejected: {sql}")
        self.assertIn("counts ROWS", result.feedback)

    def test_count_star_on_the_opco_grain_is_refused(self) -> None:
        self._reject(
            "SELECT COUNT(*) AS n FROM v_customer_opco_monthly "
            "WHERE month_start_date = DATE '2026-06-01'"
        )

    def test_count_one_and_count_column_are_refused_too(self) -> None:
        """COUNT(1) and COUNT(col) count rows just as surely as COUNT(*)."""
        for expr in ("COUNT(1)", "COUNT(membership_tier)"):
            self._reject(
                f"SELECT {expr} AS n FROM v_customer_opco_monthly "
                "WHERE month_start_date = DATE '2026-06-01'"
            )

    def test_a_group_by_does_not_make_it_safe(self) -> None:
        """Grouping by opco_code happens to be right for a single month and wrong
        for a range, and the validator cannot tell which. Refuse both and steer to
        the form that is right either way."""
        self._reject(
            "SELECT opco_code, COUNT(*) AS n FROM v_customer_opco_monthly "
            "WHERE month_start_date = DATE '2026-06-01' GROUP BY opco_code"
        )

    def test_the_bridge_is_covered_as_well(self) -> None:
        """Once per leaf category per store -- this hole predates the access change."""
        self._reject(
            "SELECT COUNT(*) AS n FROM v_customer_category_monthly "
            "WHERE month_start_date = DATE '2026-06-01'"
        )

    def test_count_distinct_customer_key_is_the_way_through(self) -> None:
        self.assertTrue(
            validate_sql(
                "SELECT COUNT(DISTINCT customer_key) AS n FROM v_customer_opco_monthly "
                "WHERE month_start_date = DATE '2026-06-01'"
            ).is_valid
        )

    def test_count_distinct_of_a_non_identity_column_is_fine(self) -> None:
        """De-duplication is what makes a count independent of row multiplicity;
        it does not have to be the customer key."""
        self.assertTrue(
            validate_sql(
                "SELECT COUNT(DISTINCT store_id) AS n FROM v_customer_category_monthly "
                "WHERE month_start_date = DATE '2026-06-01'"
            ).is_valid
        )

    def test_collapsing_to_one_row_per_customer_first_is_allowed(self) -> None:
        """The rule is per-SELECT, so the outer COUNT(*) sees a derived table that
        is already one row per customer -- not the view."""
        self.assertTrue(
            validate_sql(
                "SELECT count(*) AS n FROM ("
                "  SELECT customer_key FROM v_customer_opco_monthly "
                "  WHERE month_start_date = DATE '2026-06-01' GROUP BY customer_key) x"
            ).is_valid
        )

    def test_sales_views_are_untouched(self) -> None:
        """No customer key, no repetition, nothing to double-count."""
        self.assertTrue(
            validate_sql(
                "SELECT COUNT(*) AS n FROM v_sales_summary_daily "
                "WHERE calendar_date = DATE '2026-06-01'"
            ).is_valid
        )

    def test_per_customer_constants_are_now_aggregatable_on_the_trait_view(self) -> None:
        """The inverse of what this asserted before, and the point of the split.

        age and active_opco_count used to sit on v_customer_opco_monthly, repeated
        on every row the customer occupied, so AVG(age) was weighted by how many
        OpCos they shop with and the validator refused it by name.

        They are on v_customer_traits_monthly now -- one row per customer per month
        -- so the same expressions are simply correct, and refusing them would be a
        false positive that sends the planner hunting a bug that is not there.
        PER_CUSTOMER_CONSTANT_COLUMNS is empty by construction as a result; the
        mechanism stays for the day something is denormalised back.
        """
        for expr in ("AVG(age)", "SUM(active_opco_count)", "SUM(lifetime_transaction_count)"):
            result = validate_sql(
                f"SELECT {expr} AS x FROM v_customer_traits_monthly "
                "WHERE month_start_date = DATE '2026-06-01'"
            )
            self.assertTrue(result.is_valid, f"{expr}: {result.feedback}")

    def test_the_overlap_shape_the_merge_introduced_still_passes(self) -> None:
        """The whole point of folding active_opco_codes onto this grain."""
        self.assertTrue(
            validate_sql(
                "SELECT COUNT(DISTINCT customer_key) AS n FROM v_customer_opco_monthly "
                "WHERE month_start_date = DATE '2026-06-01' AND opco_code = 'NORTHCO' "
                "AND active_opco_codes @> ARRAY['NORTHCO_MART']"
            ).is_valid
        )
