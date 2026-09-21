from __future__ import annotations

import inspect
import json
import unittest
from datetime import date
from unittest import mock

from app.core import settings
from app.agents.planner_agent import SqlPlan
from app.agents.sql_validator_agent import (
    _alias_bindings,
    _extract_referenced_tables,
    _projected_columns,
    _validate_aliases,
    parse_sql,
    validate_sql,
)
from app.agents.answer_agent import (
    _empty_result_filter_summary,
    _fallback_empty_answer,
)
from app.policies.clarification_policy import (
    build_clarification_followup_query,
    is_option_number,
)
from app.utils.time_context import build_relative_time_context
from app.utils.text_utils import (
    INTERNAL_LINE_MARKER,
    parenthesised_token_indices,
    strip_internal_lookup_meta,
)
from app.service.glossary_service import question_vocabulary
from app.service.entity_resolution.dictionary import (
    EntityDictionary,
    division_aliases,
    normalize,
    prefix_keys,
    store_aliases,
)
from app.service.entity_resolution.matcher import (
    max_options_per_span,
    TokenRun,
    drop_bracketed_glosses,
    collapse_enum_columns,
    match_exact,
    restrict_runs,
    match_fuzzy,
    phrase_score,
    span_status,
)
from app.service.entity_resolution.models import (
    EntityCandidate,
    EntitySpan,
    ResolutionPlan,
    ResolutionResult,
    ResolvedSlot,
    public_lookup_context,
    public_lookup_plan,
    public_lookup_slots,
    redact_internal_store_ids,
)
from app.agents.analysis_agent import (
    AnalysisResult,
)
from app.db.v3_ddl import CATEGORY_DEPTH
from app.graph.nodes import lookup_nodes
from app.graph.nodes.lookup_nodes import LookupNodesMixin
from app.service.entity_resolution.service import EntityResolver


def _tree(sql: str):
    """Parse for the validator internals, which now take an AST rather than text."""
    tree = parse_sql(sql)
    assert tree is not None, f"fixture SQL did not parse: {sql[:80]}"
    return tree


def _store(store_id: str, name: str, opco_code: str = "NORTHCO_MART") -> EntityCandidate:
    return EntityCandidate(
        entity_class="store",
        canonical_value=store_id,
        display_value=name,
        target_column="store_id",
        score=100.0,
        match_kind="exact",
        opco_code=opco_code,
        row_context={"store_id": store_id},
    )


def _opco(code: str = "NORTHCO_MART", name: str = "NorthCo Mart") -> EntityCandidate:
    return EntityCandidate(
        entity_class="opco",
        canonical_value=code,
        display_value=name,
        target_column="opco_code",
        score=100.0,
        match_kind="exact",
        opco_code=code,
    )


def _category_l4(key: str, name: str, opco_code: str = "NORTHCO") -> EntityCandidate:
    return EntityCandidate(
        entity_class="category_l4",
        canonical_value=key,
        display_value=name,
        target_column="category_l4_key",
        score=100.0,
        match_kind="exact",
        opco_code=opco_code,
        category_key=int(key),
        category_level=4,
    )


def _enum_value(value: str, column: str) -> EntityCandidate:
    return EntityCandidate(
        entity_class="enum",
        canonical_value=value,
        display_value=value,
        target_column=column,
        score=100.0,
        match_kind="exact",
    )


def _resolved_test_slot(slot_id: str, target_column: str = "store_id") -> ResolvedSlot:
    return ResolvedSlot(
        slot_id=f"store:{slot_id}",
        phrase=slot_id,
        entity_class="store",
        target_column=target_column,
        status="resolved",
        canonical_value=slot_id,
        display_value=f"Store {slot_id}",
    )


def _add(dictionary: EntityDictionary, alias: str, candidate: EntityCandidate) -> None:
    alias = normalize(alias)
    if not alias:
        return
    dictionary.exact.setdefault(alias, []).append(candidate)
    for token in alias.split():
        for key in prefix_keys(token):
            dictionary.alias_prefix.setdefault(key, set()).add(alias)


def _dictionary(*candidates: EntityCandidate) -> EntityDictionary:
    dictionary = EntityDictionary(scope_signature="test")
    # "northco" is weak for OpCo too -- every OpCo name contains it. Measured that
    # way at build time; asserted here so the fixture cannot drift from it.
    dictionary.weak_tokens = {
        "opco": {"northco"},
        "store": {"northco", "mart", "mall"},
    }

    for candidate in candidates:
        if candidate.entity_class == "opco":
            _add(dictionary, normalize(candidate.display_value), candidate)
            _add(dictionary, candidate.canonical_value, candidate)
            continue
        if candidate.entity_class.startswith("category_"):
            _add(dictionary, normalize(candidate.display_value), candidate)
            continue
        if candidate.entity_class == "enum":
            _add(dictionary, normalize(candidate.display_value), candidate)
            continue

        for alias in store_aliases(normalize(candidate.display_value)):
            _add(dictionary, alias, candidate)

    return dictionary


def _resolve(query: str, dictionary: EntityDictionary):
    """Run both passes the way the resolver does."""
    spans, leftover = match_exact(query, dictionary)
    return spans + match_fuzzy(leftover, dictionary)


class EntityResolutionMatchingTests(unittest.TestCase):
    def test_store_alias_resolves_compacted_branch_name(self) -> None:
        dictionary = _dictionary(_opco(), _store("1016", "NORTHCO MART INGLEGATE JUNIPERFORD"))

        spans, _ = match_exact(
            "tell me transactions northco mart inglegatejuniperford in july",
            dictionary,
        )

        stores = [span.best for span in spans if span.best and span.best.entity_class == "store"]
        self.assertEqual("1016", stores[0].canonical_value)
        self.assertEqual("exact", stores[0].match_kind)

    def test_full_location_alias_resolves_exactly(self) -> None:
        dictionary = _dictionary(_store("1013", "NORTHCO MART VELDRA SELBYCROSS"))

        spans, leftover = match_exact(
            "tell me transactions veldra selbycross in july",
            dictionary,
        )

        self.assertEqual(1, len(spans))
        self.assertEqual("1013", spans[0].best.canonical_value)
        self.assertEqual("resolved", span_status(spans[0]))
        self.assertEqual([], leftover)

    def test_partial_branch_name_asks_instead_of_filtering(self) -> None:
        """One of three Veldra branches must not be picked silently."""
        dictionary = _dictionary(
            _store("1013", "NORTHCO MART VELDRA SELBYCROSS"),
            _store("4504", "YENMART S25 VELDRA SELBYCROSS", opco_code="NORTHCO"),
            _store("5156", "FLAT PRICE VELDRA WALK", opco_code="NORTHCO"),
        )

        spans = _resolve("tell me transactions northco mart veldra in july", dictionary)

        self.assertEqual(1, len(spans))
        self.assertEqual("ambiguous", span_status(spans[0]))
        self.assertGreaterEqual(len(spans[0].candidates), 2)

    def test_truncated_branch_name_resolves(self) -> None:
        dictionary = _dictionary(
            _store("2020", "NORTHCO MALL S05 NOVELBURN", opco_code="NORTHCO")
        )

        spans = _resolve("transactions at novelbur in july", dictionary)

        self.assertEqual(1, len(spans))
        self.assertEqual("2020", spans[0].best.canonical_value)
        self.assertEqual("store", spans[0].best.entity_class)
        self.assertEqual("resolved", span_status(spans[0]))

    def test_fuzzy_cannot_find_branch_missing_from_scope(self) -> None:
        dictionary = _dictionary(_store("1016", "NORTHCO MART INGLEGATE JUNIPERFORD"))

        self.assertEqual([], _resolve("transactions at novelbur", dictionary))

    def test_shared_word_ending_is_not_a_match(self) -> None:
        """"veld" must not reach SOVELD. This was option 2 of 5 in production."""
        dictionary = _dictionary(
            _store("1013", "NORTHCO MART VELDRA SELBYCROSS"),
            _store("2025", "NORTHCO MALL SOVELD", opco_code="NORTHCO"),
        )

        spans = _resolve("how many customers visited northco veld nin july 2025", dictionary)

        self.assertEqual(1, len(spans))
        labels = [c.display_value for c in spans[0].candidates]
        self.assertIn("NORTHCO MART VELDRA SELBYCROSS", labels)
        self.assertNotIn("NORTHCO MALL SOVELD", labels)

    def test_one_misspelt_name_produces_one_span(self) -> None:
        """Nested readings of a phrase are alternatives, not separate findings."""
        dictionary = _dictionary(_store("1013", "NORTHCO MART VELDRA SELBYCROSS"))

        spans = _resolve("how many customers visited northco veld nin july 2025", dictionary)

        self.assertEqual(1, len(spans))
        self.assertEqual("veld", spans[0].text)
        # No span may cover a verb, a month or a year.
        for span in spans:
            self.assertNotIn("visited", span.text)
            self.assertNotIn("july", span.text)
            self.assertNotIn("2025", span.text)

    def test_opco_is_never_fuzzy_matched(self) -> None:
        """A misspelt OpCo must not become a filter on a different OpCo.

        "northco veld" scored 75 against "northco bank" and resolved, because every
        OpCo name starts with "northco" and there was no floor on guessing.
        """
        dictionary = _dictionary(_opco("NORTHCO_BANK", "NorthCo Bank"))

        spans = _resolve("how many customers visited northco veld nin july 2025", dictionary)

        self.assertEqual([], spans)

    def test_exact_opco_name_still_resolves(self) -> None:
        dictionary = _dictionary(_opco("NORTHCO_MART", "NorthCo Mart"))

        spans, _ = match_exact("transactions for northco mart in july", dictionary)

        self.assertEqual(1, len(spans))
        self.assertEqual("NORTHCO_MART", spans[0].best.canonical_value)
        self.assertEqual("resolved", span_status(spans[0]))

    def test_store_scoring_ignores_opco_banner_words(self) -> None:
        score = phrase_score(
            ("northco", "mart", "novelbur"),
            ("northco", "mart", "veldra", "selbycross"),
            {"northco", "mart", "mall"},
        )

        self.assertLess(score, settings.lookup_suggest_score)

    def test_generic_request_words_do_not_become_lookups(self) -> None:
        dictionary = _dictionary(
            _store("2020", "NORTHCO MALL S05 NOVELBURN", opco_code="NORTHCO"),
            _store("2021", "NORTHCO MALL QUILLREACH RAVENSHOLLOW", opco_code="NORTHCO"),
            _store("2022", "FACILITY RENTAL", opco_code="NORTHCO"),
        )

        spans, leftover = match_exact(
            "tell me the total number of transactions northco novelburn in july 2025",
            dictionary,
        )

        stores = [span.best for span in spans if span.best and span.best.entity_class == "store"]
        self.assertEqual("2020", stores[0].canonical_value)
        self.assertEqual([], leftover)
        self.assertEqual([], _resolve("tell me the total for july", dictionary))

    def test_customer_dimension_stage_does_not_become_product_lookup(self) -> None:
        dictionary = _dictionary(
            _category_l4(
                "4314",
                "SOFT > INNERWEAR > LADIES INNERWEAR > SCARLETEEN BRA STAGE 1",
            ),
            _category_l4(
                "4315",
                "SOFT > INNERWEAR > LADIES INNERWEAR > SCARLETEEN BRA STAGE 2",
            ),
            _category_l4(
                "4316",
                "SOFT > INNERWEAR > LADIES INNERWEAR > SCARLETEEN BRA STAGE 3",
            ),
        )

        self.assertEqual(
            [],
            _resolve(
                "How many customers were in each lifecycle stage in May 2026?",
                dictionary,
            ),
        )

    def test_prefix_keys_survive_a_transposition(self) -> None:
        self.assertTrue(prefix_keys("bnak") & prefix_keys("bank"))
        self.assertFalse(prefix_keys("veld") & prefix_keys("soveld"))

    def test_public_lookup_payload_redacts_store_ids(self) -> None:
        plan = {
            "status": "pending",
            "slots": [
                {
                    "slot_id": "store:novelbur",
                    "phrase": "novelbur",
                    "entity_class": "store",
                    "target_column": "store_id",
                    "status": "ambiguous",
                    "canonical_value": None,
                    "display_value": None,
                    "options": [
                        {
                            "entity_class": "store",
                            "canonical_value": "1013",
                            "display_value": "NORTHCO MART VELDRA SELBYCROSS",
                            "target_column": "store_id",
                        }
                    ],
                }
            ],
        }

        public_plan = public_lookup_plan(plan)
        option = public_plan["slots"][0]["options"][0]

        self.assertEqual("NORTHCO MART VELDRA SELBYCROSS", option["canonical_value"])
        self.assertEqual("store", option["target_column"])
        self.assertNotIn("1013", str(public_plan))

    def test_public_lookup_context_uses_store_display_name(self) -> None:
        plan = {
            "status": "resolved",
            "slots": [
                {
                    "slot_id": "store:inglegatejuniperford",
                    "phrase": "inglegatejuniperford",
                    "entity_class": "store",
                    "target_column": "store_id",
                    "status": "resolved",
                    "canonical_value": "1016",
                    "display_value": "NORTHCO MART INGLEGATE JUNIPERFORD",
                    "options": [],
                }
            ],
        }

        context = public_lookup_context(plan)
        matches = public_lookup_slots(plan["slots"])

        self.assertIn('store "NORTHCO MART INGLEGATE JUNIPERFORD"', context)
        self.assertNotIn("1016", context)
        self.assertEqual("NORTHCO MART INGLEGATE JUNIPERFORD", matches[0]["canonical_value"])

    def test_public_response_redacts_store_id_text_and_sql(self) -> None:
        plan = {
            "status": "resolved",
            "slots": [
                {
                    "slot_id": "store:veldra",
                    "phrase": "veldra",
                    "entity_class": "store",
                    "target_column": "store_id",
                    "status": "resolved",
                    "canonical_value": "1013",
                    "display_value": "NORTHCO MART VELDRA SELBYCROSS",
                    "options": [],
                }
            ],
        }
        result = {
            "answer": "No transactions for store ID 1013.",
            "intent_reason": "Calculated for store_id = '1013'.",
            "sql": "SELECT 1 FROM v_sales_store_monthly s WHERE s.store_id = '1013'",
            "analysis": {
                "calculation_logic": ["Filtered on store ID '1013'."]
            },
        }

        public = redact_internal_store_ids(result, plan)

        self.assertIsNone(public["sql"])
        self.assertNotIn("1013", str(public))
        self.assertIn("NORTHCO MART VELDRA SELBYCROSS", str(public))

    def test_public_store_plan_restores_internal_value_for_followup(self) -> None:
        dictionary = _dictionary(_store("1013", "NORTHCO MART VELDRA SELBYCROSS"))
        plan = ResolutionPlan.model_validate(
            {
                "status": "resolved",
                "slots": [
                    {
                        "slot_id": "store:veldra",
                        "phrase": "veldra",
                        "entity_class": "store",
                        "target_column": "store",
                        "status": "resolved",
                        "canonical_value": "NORTHCO MART VELDRA SELBYCROSS",
                        "display_value": "NORTHCO MART VELDRA SELBYCROSS",
                        "options": [],
                    }
                ],
            }
        )

        EntityResolver()._restore_internal_store_values(plan, dictionary)

        self.assertEqual("store_id", plan.slots[0].target_column)
        self.assertEqual("1013", plan.slots[0].canonical_value)

    def test_numbered_store_selection_restores_internal_value(self) -> None:
        dictionary = _dictionary(_store("1013", "NORTHCO MART VELDRA SELBYCROSS"))
        plan = ResolutionPlan.model_validate(
            {
                "status": "pending",
                "slots": [
                    {
                        "slot_id": "store:veldra",
                        "phrase": "veldra",
                        "entity_class": "store",
                        "target_column": "store",
                        "status": "ambiguous",
                        "canonical_value": None,
                        "display_value": None,
                        "options": [
                            {
                                "entity_class": "store",
                                "canonical_value": "NORTHCO MART VELDRA SELBYCROSS",
                                "display_value": "NORTHCO MART VELDRA SELBYCROSS",
                                "target_column": "store",
                            }
                        ],
                    }
                ],
            }
        )

        self.assertTrue(EntityResolver()._apply_selection(plan, "1", dictionary))
        self.assertEqual("store_id", plan.slots[0].target_column)
        self.assertEqual("1013", plan.slots[0].canonical_value)

    def test_yes_to_lookup_options_requires_number_reply(self) -> None:
        plan = ResolutionPlan.model_validate(
            {
                "status": "pending",
                "slots": [
                    {
                        "slot_id": "store:veldra",
                        "phrase": "veldra",
                        "entity_class": "store",
                        "target_column": "store",
                        "status": "ambiguous",
                        "canonical_value": None,
                        "display_value": None,
                        "options": [
                            {
                                "entity_class": "store",
                                "canonical_value": "NORTHCO MART VELDRA SELBYCROSS",
                                "display_value": "NORTHCO MART VELDRA SELBYCROSS",
                                "target_column": "store",
                            }
                        ],
                    }
                ],
            }
        )
        resolver = EntityResolver()
        dictionary = _dictionary(_store("1013", "NORTHCO MART VELDRA SELBYCROSS"))

        self.assertFalse(resolver._apply_selection(plan, "yes", dictionary))
        result = resolver._finalize(plan, [], invalid_selection="yes")

        self.assertTrue(result.needs_clarification)
        self.assertIn("option number only", result.clarification_question)
        self.assertIn("not yes/no", result.clarification_question)
        self.assertIn("1. NORTHCO MART VELDRA SELBYCROSS", result.clarification_question)

    def test_lookup_slots_are_capped_by_configured_limit(self) -> None:
        """The cap counts DIMENSIONS, so it takes three distinct columns to trip.

        Several stores are one `store_id IN (...)` predicate and stay within budget;
        what the limit exists to bound is the number of dimensions combined.
        """
        resolver = EntityResolver()
        plan = ResolutionPlan()
        original_limit = settings.lookup_max_slots_per_question
        settings.lookup_max_slots_per_question = 2
        try:
            self.assertTrue(resolver._append_lookup_slot(plan, _resolved_test_slot("1013")))
            self.assertTrue(resolver._append_lookup_slot(plan, _resolved_test_slot("1016")))
            self.assertTrue(
                resolver._append_lookup_slot(
                    plan, _resolved_test_slot("2020", target_column="category_l2_key")
                )
            )
            self.assertFalse(
                resolver._append_lookup_slot(
                    plan, _resolved_test_slot("3030", target_column="membership_tier")
                )
            )

            result = resolver._too_many_lookup_values_result(
                plan, [], [("Store 2020", "store")]
            )
        finally:
            settings.lookup_max_slots_per_question = original_limit

        self.assertTrue(result.needs_clarification)
        self.assertIn("at most 2 kinds of stores", result.clarification_question)
        self.assertNotIn("OpCo", result.clarification_question)
        # The message names what was found rather than only restating the limit.
        self.assertIn("Store 1013", result.clarification_question)
        self.assertIn("Store 2020", result.clarification_question)

    def test_opco_slot_does_not_consume_the_lookup_budget(self) -> None:
        """An OpCo is a scope, not a lookup value; it must not crowd out a store."""
        resolver = EntityResolver()
        plan = ResolutionPlan()
        opco_slot = ResolvedSlot(
            slot_id="opco:northco mart",
            phrase="northco mart",
            entity_class="opco",
            target_column="opco_code",
            status="resolved",
            canonical_value="NORTHCO_MART",
        )
        original_limit = settings.lookup_max_slots_per_question
        settings.lookup_max_slots_per_question = 2
        try:
            self.assertTrue(resolver._append_lookup_slot(plan, opco_slot))
            self.assertTrue(resolver._append_lookup_slot(plan, _resolved_test_slot("1013")))
            self.assertTrue(
                resolver._append_lookup_slot(
                    plan, _resolved_test_slot("1016", target_column="category_l2_key")
                )
            )
            # the OpCo did not consume a slot, so two dimensions still fit; a third
            # is what trips it
            self.assertFalse(
                resolver._append_lookup_slot(
                    plan, _resolved_test_slot("2020", target_column="membership_tier")
                )
            )
        finally:
            settings.lookup_max_slots_per_question = original_limit

    def test_lookup_option_questions_ask_for_number_only(self) -> None:
        plan = ResolutionPlan.model_validate(
            {
                "status": "pending",
                "slots": [
                    {
                        "slot_id": "store:veldra",
                        "phrase": "veldra",
                        "entity_class": "store",
                        "target_column": "store",
                        "status": "ambiguous",
                        "canonical_value": None,
                        "display_value": None,
                        "options": [
                            {
                                "entity_class": "store",
                                "canonical_value": "NORTHCO MART VELDRA SELBYCROSS",
                                "display_value": "NORTHCO MART VELDRA SELBYCROSS",
                                "target_column": "store",
                                "opco_code": "NORTHCO_MART",
                                "opco_name": "NorthCo Mart",
                            }
                        ],
                    }
                ],
            }
        )

        result = EntityResolver()._finalize(plan, [])

        self.assertIn("Reply with the option number only.", result.clarification_question)
        self.assertIn(
            "1. NORTHCO MART VELDRA SELBYCROSS (OpCo: NorthCo Mart)",
            result.clarification_question,
        )

    def test_lookup_options_show_opco_for_each_option(self) -> None:
        plan = ResolutionPlan.model_validate(
            {
                "status": "pending",
                "slots": [
                    {
                        "slot_id": "store:novelburn",
                        "phrase": "novelburn",
                        "entity_class": "store",
                        "target_column": "store",
                        "status": "ambiguous",
                        "canonical_value": None,
                        "display_value": None,
                        "options": [
                            {
                                "entity_class": "store",
                                "canonical_value": "NORTHCO MALL S05 NOVELBURN",
                                "display_value": "NORTHCO MALL S05 NOVELBURN",
                                "target_column": "store",
                                "opco_code": "NORTHCO",
                                "opco_name": "NorthCo",
                            },
                            {
                                "entity_class": "store",
                                "canonical_value": "NorthCo CREDIT NOVELBURN",
                                "display_value": "NorthCo CREDIT NOVELBURN",
                                "target_column": "store",
                                "opco_code": "NORTHCO_CREDIT",
                                "opco_name": "NorthCo Credit",
                            },
                        ],
                    }
                ],
            }
        )

        result = EntityResolver()._finalize(plan, [])

        self.assertIn(
            "1. NORTHCO MALL S05 NOVELBURN (OpCo: NorthCo)",
            result.clarification_question,
        )
        self.assertIn(
            "2. NorthCo CREDIT NOVELBURN (OpCo: NorthCo Credit)",
            result.clarification_question,
        )

    def test_yes_to_numbered_lookup_options_is_not_treated_as_confirmation(self) -> None:
        previous_payload = {
            "type": "clarify",
            "answer": 'Which store did you mean?\n1. NORTHCO MART VELDRA SELBYCROSS',
            "lookup_plan": {
                "status": "pending",
                "slots": [
                    {
                        "status": "ambiguous",
                        "options": [{"display_value": "NORTHCO MART VELDRA SELBYCROSS"}],
                    }
                ],
            },
        }

        _, followup, reason = build_clarification_followup_query(
            current_query="yes",
            history=[
                {
                    "role": "user",
                    "content": "tell me transactions northco mart veldra in july",
                },
                {
                    "role": "assistant",
                    "content": json.dumps(previous_payload),
                },
            ],
            previous_payload=previous_payload,
        )

        self.assertTrue(followup)
        self.assertIn("option number", reason)
        self.assertNotIn("confirmed", reason.lower())

    def test_validator_rejects_raw_store_name_where_filter(self) -> None:
        result = validate_sql(
            """
            SELECT SUM(s.transaction_count) AS total_transactions
            FROM v_sales_store_monthly AS s
            JOIN dim_store AS d
              ON s.store_id = d.store_id
             AND s.opco_code = d.opco_code
            WHERE s.opco_code = 'NORTHCO_MART'
              AND s.month_start_date = DATE '2025-07-01'
              AND LOWER(TRIM(d.store_name)) = LOWER(TRIM('NorthCo mart inglegatejuniperford'))
            """
        )

        self.assertFalse(result.is_valid)
        self.assertIn("store_name", result.feedback)
        self.assertIn("store_id", result.feedback)


if __name__ == "__main__":
    unittest.main()






class EmptyResultAndGuessedFilterTests(unittest.TestCase):
    """Two failures from one request: "how many customers buys hardline in 2026".

    "hardline" is the user's word for the HARD division, so it resolves to nothing.
    The planner filled the gap itself with a category_name literal, the query
    matched no row, and the empty-result answer path then crashed on a leftover v2
    variable.
    """

    def test_empty_result_summary_does_not_crash_on_a_month_filter(self) -> None:
        """`elif month:` survived the v3 rename and raised NameError."""
        summary = _empty_result_filter_summary(
            "SELECT COUNT(DISTINCT c.customer_key) AS n FROM v_customer_category_monthly c "
            "WHERE c.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01'"
        )
        self.assertIsInstance(summary, str)

    def test_empty_result_answer_is_produced_for_the_reported_sql(self) -> None:
        answer = _fallback_empty_answer(
            "SELECT COUNT(DISTINCT c.customer_key) AS distinct_customers, "
            "LOWER(TRIM(pc.category_name)) AS category_name "
            "FROM v_customer_category_monthly c JOIN dim_product_category pc "
            "ON c.category_key = pc.category_key "
            "WHERE c.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01' "
            "AND LOWER(TRIM(pc.category_name)) = LOWER(TRIM('Hardline'))"
        )
        self.assertTrue(answer.strip())

    def test_guessed_category_name_filter_is_rejected(self) -> None:
        result = validate_sql(
            """
            SELECT COUNT(DISTINCT c.customer_key) AS distinct_customers
            FROM v_customer_category_monthly AS c
            JOIN dim_product_category AS pc ON c.category_key = pc.category_key
            WHERE c.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01'
              AND LOWER(TRIM(pc.category_name)) = LOWER(TRIM('Hardline'))
            """
        )

        self.assertFalse(result.is_valid)
        self.assertIn("category_name", result.feedback)
        self.assertIn("category_key", result.feedback)

    def test_resolved_category_key_filter_is_allowed(self) -> None:
        result = validate_sql(
            """
            SELECT COUNT(DISTINCT c.customer_key) AS distinct_customers
            FROM v_customer_category_monthly AS c
            WHERE c.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01'
              AND c.category_l1_key = 1886
            """
        )

        self.assertTrue(result.is_valid, result.feedback)


class DivisionAliasTests(unittest.TestCase):
    def test_single_word_divisions_get_retail_line_forms(self) -> None:
        self.assertEqual(
            {"hardline", "hard line", "hardlines", "hard lines"},
            division_aliases("hard"),
        )

    def test_multi_word_divisions_are_left_alone(self) -> None:
        self.assertEqual(set(), division_aliases("non merchandise"))
        self.assertEqual(set(), division_aliases("h bc"))

    def test_hardline_resolves_to_the_hard_division(self) -> None:
        hard = EntityCandidate(
            entity_class="category_l1",
            canonical_value="1878",
            display_value="HARD",
            target_column="category_l1_key",
            score=100.0,
            match_kind="exact",
            opco_code="NORTHCO",
            category_key=1878,
        )
        dictionary = EntityDictionary(scope_signature="test")
        for alias in {"hard"} | division_aliases("hard"):
            _add(dictionary, alias, hard)

        spans, leftover = match_exact("how many customers buys hardline in 2026", dictionary)

        self.assertEqual(1, len(spans))
        self.assertEqual("1878", spans[0].best.canonical_value)
        self.assertEqual("category_l1_key", spans[0].best.target_column)
        self.assertEqual("resolved", span_status(spans[0]))
        self.assertEqual([], leftover)


class AliasAwareColumnTests(unittest.TestCase):
    """Column existence is per alias, not per query.

    "how many customers buys baby products in 2026" produced
    `JOIN dim_product_category AS pc ON cc.category_key = pc.category_l2_key`.
    `category_l2_key` is real -- on the view aliased `cc`, not on the dimension
    aliased `pc` (which calls it `l2_key`). The union-based check accepted it and
    Postgres raised UndefinedColumn at execution.
    """

    def test_column_from_the_wrong_table_is_rejected(self) -> None:
        result = validate_sql(
            """
            SELECT COUNT(DISTINCT cc.customer_key) AS distinct_customers
            FROM v_customer_category_monthly AS cc
            JOIN dim_product_category AS pc ON cc.category_key = pc.category_l2_key
            WHERE cc.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01'
              AND pc.category_key = 2793
            """
        )

        self.assertFalse(result.is_valid)
        self.assertIn("category_l2_key", result.feedback)
        self.assertIn("dim_product_category", result.feedback)
        # The message must be actionable: name the real column and the mix-up.
        self.assertIn("l2_key", result.feedback)
        self.assertIn("another table in this query", result.feedback)

    def test_correct_dimension_column_is_accepted(self) -> None:
        result = validate_sql(
            """
            SELECT COUNT(DISTINCT cc.customer_key) AS distinct_customers
            FROM v_customer_category_monthly AS cc
            JOIN dim_product_category AS pc ON cc.category_key = pc.category_key
            WHERE cc.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01'
              AND pc.l2_key = 2381
            """
        )

        self.assertTrue(result.is_valid, result.feedback)

    def test_no_join_needed_when_the_key_is_already_resolved(self) -> None:
        result = validate_sql(
            """
            SELECT COUNT(DISTINCT cc.customer_key) AS distinct_customers
            FROM v_customer_category_monthly AS cc
            WHERE cc.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01'
              AND cc.category_l2_key = 2381
            """
        )

        self.assertTrue(result.is_valid, result.feedback)

    def test_alias_bindings_ignore_join_keywords(self) -> None:
        bindings = _alias_bindings(
            _tree(
                "SELECT 1 FROM v_sales_summary_daily s "
                "JOIN dim_store AS d ON s.store_id = d.store_id "
                "JOIN dim_opco ON dim_opco.opco_code = s.opco_code"
            )
        )

        self.assertEqual("v_sales_summary_daily", bindings["s"])
        self.assertEqual("dim_store", bindings["d"])
        self.assertEqual("dim_opco", bindings["dim_opco"])
        self.assertNotIn("on", bindings)
        self.assertNotIn("join", bindings)


class KeywordArgumentFunctionTests(unittest.TestCase):
    """`EXTRACT(YEAR FROM col)` must not read as a table reference.

    The extractor matched "FROM c.month_start_date" and, because the captured name
    contains a dot, rejected the query as schema-qualified -- a valid question
    failing on a rule it had not broken. Pre-existing; surfaced when the planner
    happened to use EXTRACT for a year filter.
    """

    SQL = (
        "SELECT COUNT(DISTINCT c.customer_key) AS n, "
        "EXTRACT(YEAR FROM c.month_start_date) AS y "
        "FROM v_customer_category_monthly AS c "
        "WHERE c.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-01' "
        "AND c.category_l1_key = 1878 GROUP BY y"
    )

    def test_extract_argument_is_not_a_table(self) -> None:
        self.assertEqual({"v_customer_category_monthly"}, _extract_referenced_tables(_tree(self.SQL)))

    def test_query_using_extract_validates(self) -> None:
        result = validate_sql(self.SQL)
        self.assertTrue(result.is_valid, result.feedback)

    def test_the_whole_select_list_is_inspected(self) -> None:
        """The regex version truncated the projection at the inner FROM.

        A truncated projection stopped the identity scan early, so anything after
        an EXTRACT in the SELECT list was never examined.
        """
        names = {c.name.lower() for c in _projected_columns(_tree(self.SQL))}
        self.assertIn("month_start_date", names)
        self.assertIn("customer_key", names)

    def test_identity_projection_is_still_caught_alongside_extract(self) -> None:
        result = validate_sql(
            "SELECT c.customer_key, EXTRACT(YEAR FROM c.month_start_date) AS y "
            "FROM v_customer_category_monthly AS c "
            "WHERE c.month_start_date > DATE '2026-01-01'"
        )
        self.assertFalse(result.is_valid)
        self.assertIn("customer_key", result.feedback)

    def test_real_schema_qualification_is_still_rejected(self) -> None:
        result = validate_sql(
            "SELECT SUM(s.gross_sales_amount) AS t FROM ci_hod.v_sales_summary_daily AS s "
            "WHERE s.calendar_date > DATE '2026-06-01'"
        )
        self.assertFalse(result.is_valid)
        self.assertIn("schema-qualified", result.feedback)

    def test_other_keyword_argument_functions_are_masked(self) -> None:
        self.assertEqual(
            {"dim_store"},
            _extract_referenced_tables(
                _tree(
                    "SELECT TRIM(BOTH ' ' FROM d.store_name) AS n, "
                    "POSITION('x' IN d.store_name) AS p FROM dim_store AS d"
                )
            ),
        )


class OpcoPinAndAskOrderTests(unittest.TestCase):
    """A resolved store fixes the OpCo; later options must respect it.

    Observed: "customers who visited NorthCo inglegatejuniperford purchased lamb", answered with
    WELLNESS INGLEGATEJUNIPERFORD (NorthCo), then still offered "Fresh > Fresh > Lamb
    (NorthCo Mart)" for lamb. Combining them can match no row, so the answer would have
    been another zero that looks real.
    """

    @staticmethod
    def _plan(store_opco: str | None = "NORTHCO") -> ResolutionPlan:
        store = ResolvedSlot(
            slot_id="store:inglegatejuniperford",
            phrase="inglegatejuniperford",
            entity_class="store",
            target_column="store_id",
            status="resolved" if store_opco else "ambiguous",
            canonical_value="1099" if store_opco else None,
            display_value="WELLNESS INGLEGATEJUNIPERFORD" if store_opco else None,
            opco_code=store_opco,
        )
        lamb = ResolvedSlot(
            slot_id="category_l3:lamb",
            phrase="lamb",
            entity_class="category_l3",
            target_column="category_l3_key",
            status="ambiguous",
            options=[
                {"entity_class": "category_l3", "canonical_value": "1125",
                 "display_value": "Fresh > Fresh > Lamb", "target_column": "category_l3_key",
                 "opco_code": "NORTHCO_MART", "match_kind": "exact", "category_key": 1125},
                {"entity_class": "category_l4", "canonical_value": "2479",
                 "display_value": "FOOD > PERISHABLE > MEAT > LAMB", "target_column": "category_l4_key",
                 "opco_code": "NORTHCO", "match_kind": "exact", "category_key": 2479},
                {"entity_class": "category_l4", "canonical_value": "3978",
                 "display_value": "OTHERS > P.C CENTER > P.C CENTER > LAMB", "target_column": "category_l4_key",
                 "opco_code": "NORTHCO", "match_kind": "exact", "category_key": 3978},
            ],
        )
        return ResolutionPlan(status="pending", slots=[store, lamb])

    def test_options_from_other_opcos_are_dropped(self) -> None:
        plan = self._plan("NORTHCO")

        EntityResolver._apply_opco_pin(plan)

        opcos = {o["opco_code"] for o in plan.slots[1].options}
        self.assertEqual({"NORTHCO"}, opcos)
        self.assertEqual(2, len(plan.slots[1].options))

    def test_nothing_is_dropped_before_a_store_is_chosen(self) -> None:
        plan = self._plan(store_opco=None)

        EntityResolver._apply_opco_pin(plan)

        self.assertEqual(3, len(plan.slots[1].options))

    def test_a_single_survivor_resolves_without_asking(self) -> None:
        plan = self._plan("NORTHCO_MART")

        EntityResolver._apply_opco_pin(plan)

        lamb = plan.slots[1]
        self.assertEqual("resolved", lamb.status)
        self.assertEqual("1125", lamb.canonical_value)
        self.assertEqual([], lamb.options)

    def test_impossible_combination_is_explained_not_asked(self) -> None:
        plan = self._plan("NORTHCO")
        plan.slots[1].options = [
            {"entity_class": "category_l3", "canonical_value": "1125",
             "display_value": "Fresh > Fresh > Lamb", "target_column": "category_l3_key",
             "opco_code": "NORTHCO_MART", "match_kind": "exact"},
        ]

        result = EntityResolver()._finalize(plan, [])

        self.assertTrue(result.needs_clarification)
        self.assertIn("NORTHCO", result.clarification_question)
        self.assertIn("different OpCos cannot be combined", result.clarification_question)

    def test_store_is_asked_before_product_regardless_of_word_order(self) -> None:
        """The product appears first in the plan; the store must still be asked first."""
        plan = self._plan(store_opco=None)
        plan.slots = [plan.slots[1], plan.slots[0]]

        order = [s.entity_class for s in EntityResolver._ambiguous_in_ask_order(plan)]

        self.assertEqual(["store", "category_l3"], order)

    def test_a_numbered_reply_targets_the_slot_that_was_asked_about(self) -> None:
        """Question and reply must use one order, or the answer lands on the wrong slot."""
        plan = self._plan(store_opco=None)
        plan.slots = [plan.slots[1], plan.slots[0]]
        plan.slots[1].options = [
            {"entity_class": "store", "canonical_value": "1099",
             "display_value": "WELLNESS INGLEGATEJUNIPERFORD", "target_column": "store_id",
             "opco_code": "NORTHCO", "match_kind": "exact"},
        ]
        dictionary = EntityDictionary(scope_signature="test")

        self.assertTrue(EntityResolver()._apply_selection(plan, "1", dictionary))

        store = next(s for s in plan.slots if s.entity_class == "store")
        self.assertEqual("resolved", store.status)
        self.assertEqual("WELLNESS INGLEGATEJUNIPERFORD", store.display_value)


class AssumedPeriodTests(unittest.TestCase):
    """An invented period must be refused, whatever the wording.

    Observed: asked for "all time", the planner answered with June 2026 data and
    narrated it as fact. The SQL was well-formed and carried a period filter, so
    every deterministic check passed -- the period was simply not the one asked for.

    The first fix was a phrase list plus a date-ish regex, and it broke exactly where
    wording varied: "may i know how many customers all time" passed because `may`
    looked like a month. Now the planner classifies `period_source` and the graph
    refuses anything that is not `stated`, so these tests cover the decision rather
    than a pattern.
    """

    @staticmethod
    def _plan(**kw) -> SqlPlan:
        base = dict(
            status="answerable",
            sql="SELECT SUM(s.transaction_count) AS t FROM v_sales_summary_daily AS s "
                "WHERE s.calendar_date BETWEEN DATE '2026-06-01' AND DATE '2026-06-30'",
            reason="counts transactions",
        )
        base.update(kw)
        return SqlPlan(**base)

    def test_default_is_stated_so_existing_plans_execute(self) -> None:
        self.assertEqual("stated", self._plan().period_source)

    def test_an_assumed_period_is_reported_with_its_label(self) -> None:
        plan = self._plan(period_source="assumed", period_label="June 2026")
        self.assertEqual("assumed", plan.period_source)
        self.assertEqual("June 2026", plan.period_label)

    def test_none_is_available_for_dimension_only_queries(self) -> None:
        plan = self._plan(
            sql="SELECT count(*) AS n FROM dim_store", period_source="none"
        )
        self.assertEqual("none", plan.period_source)

    def test_the_guard_no_longer_pattern_matches_wording(self) -> None:
        """The phrase list is gone; nothing in the guard inspects period wording."""
        import app.service.query_shape_guard as guard

        self.assertFalse(hasattr(guard, "asks_for_unbounded_period"))
        self.assertFalse(hasattr(guard, "_UNBOUNDED_PERIOD"))
        self.assertFalse(hasattr(guard, "_EXPLICIT_PERIOD"))

    def test_an_assumed_period_is_refused_even_when_the_plan_says_answerable(self) -> None:
        """The backstop: the model answering with an invented period must not execute.

        In practice the planner now asks on its own, but that is a prompt rule. This
        is the part that cannot be talked out of: a plan marked `answerable` whose
        period it invented is converted into a clarification, and the SQL is dropped.
        """
        import asyncio
        import app.graph.nodes.planning_nodes as pn

        assumed = self._plan(period_source="assumed", period_label="June 2026")

        async def fake_plan_sql(*a, **kw):
            return assumed

        class Node(pn.PlanningNodesMixin):
            principal = None

        original = pn.plan_sql
        pn.plan_sql = fake_plan_sql
        try:
            out = asyncio.run(Node().make_plan({"query": "how many transactions"}))
        finally:
            pn.plan_sql = original

        self.assertEqual("needs_clarification", out["plan_status"])
        self.assertIsNone(out["sql"], "SQL with an invented period must not be executed")
        self.assertTrue(out["needs_clarification"])
        self.assertEqual(["time_period"], out["missing_slots"])
        # the assumption is offered, so the user answers in one step
        self.assertIn("June 2026", out["clarification_question"])
        # and the dropped SQL is still recorded for debugging
        self.assertIn(assumed.sql, out["sql_attempts"])

    def test_a_stated_period_passes_straight_through(self) -> None:
        import asyncio
        import app.graph.nodes.planning_nodes as pn

        stated = self._plan(period_source="stated", period_label="June 2026")

        async def fake_plan_sql(*a, **kw):
            return stated

        class Node(pn.PlanningNodesMixin):
            principal = None

            def _fact_time_period_guard_question(self, state, sql):
                return None   # the no-period-filter guard is covered separately

        original = pn.plan_sql
        pn.plan_sql = fake_plan_sql
        try:
            out = asyncio.run(Node().make_plan({"query": "how many transactions in june 2026"}))
        finally:
            pn.plan_sql = original

        self.assertEqual("answerable", out["plan_status"])
        self.assertEqual(stated.sql, out["sql"])


class DimensionVocabularyTests(unittest.TestCase):
    """A word that names a COLUMN must not be resolved as a product.

    "How many customers were in each lifecycle stage in May 2026?" offered three
    SCARLETEEN BRA STAGE products, because "stage" is one token of that four-token
    name. Two things were wrong and both are fixed structurally, so the next word
    nobody anticipated is covered too.
    """

    def test_scoring_alone_cannot_separate_these_cases(self) -> None:
        """Why the fix is vocabulary-based and not threshold-based.

        "stage" in a four-token product name and "veldra" in a four-token store name
        are indistinguishable by score. Any threshold that rejects one rejects the
        other, which is why the discrimination cannot live in the scorer.
        """
        weak: set = set()
        stage = phrase_score(("stage",), ("scarleteen", "bra", "stage", "1"), weak)
        veldra = phrase_score(("veldra",), ("yenmart", "ab", "veldra", "selbycross"), weak)
        self.assertAlmostEqual(stage, veldra, places=5)

    def test_a_window_of_only_column_words_is_skipped(self) -> None:
        """Sourced from the glossary, so a new column protects its own vocabulary."""
        bra = EntityCandidate(
            entity_class="category_l4",
            canonical_value="4314",
            display_value="SOFT > INNERWEAR > LADIES INNERWEAR > SCARLETEEN BRA STAGE 1",
            target_column="category_l4_key",
            score=100.0,
            match_kind="exact",
            opco_code="NORTHCO",
            category_key=4314,
        )
        dictionary = EntityDictionary(
            scope_signature="test", schema_terms=frozenset({"lifecycle", "stage"})
        )
        _add(dictionary, "scarleteen bra stage 1", bra)

        # "stage" and "lifecycle stage" are column vocabulary -> no span at all.
        self.assertEqual([], _resolve("customers by lifecycle stage in may 2026", dictionary))

    def test_column_words_do_not_block_a_name_that_merely_contains_one(self) -> None:
        """A store called VALUE CITY still matches on the part that is a name."""
        store = _store("9001", "NorthCo VALUE CITY")
        dictionary = EntityDictionary(
            scope_signature="test", schema_terms=frozenset({"value", "segment"})
        )
        for alias in store_aliases(normalize(store.display_value)):
            _add(dictionary, alias, store)

        spans, _ = match_exact("transactions at value city in june 2026", dictionary)
        self.assertTrue(spans, "an exact alias hit must survive the column-word rule")


class BudgetCountsColumnsTests(unittest.TestCase):
    """Many values of one column is one filter, not many.

    "How many customers are in Elite, Premium, Growth, Mass and At Risk" names five
    values of value_segment -- a single IN list -- and was refused as five filters.
    What makes SQL unpredictable is the number of dimensions combined, not the
    length of an IN list.
    """

    @staticmethod
    def _enum(value: str, column: str = "family_segment") -> ResolvedSlot:
        return ResolvedSlot(
            slot_id=f"enum:{value.lower()}",
            phrase=value.lower(),
            entity_class="enum",
            target_column=column,
            status="resolved",
            canonical_value=value,
            display_value=value,
        )

    @staticmethod
    def _category(key: str, column: str) -> ResolvedSlot:
        return ResolvedSlot(
            slot_id=f"category:{key}",
            phrase=key,
            entity_class="category_l2",
            target_column=column,
            status="resolved",
            canonical_value=key,
            display_value=key,
        )

    def test_five_values_of_one_column_all_fit(self) -> None:
        resolver = EntityResolver()
        plan = ResolutionPlan()
        original = settings.lookup_max_slots_per_question
        settings.lookup_max_slots_per_question = 2
        try:
            for key in ("101", "102", "103", "104", "105"):
                self.assertTrue(
                    resolver._append_lookup_slot(
                        plan, self._category(key, "category_l2_key")
                    ),
                    key,
                )
            self.assertEqual(1, resolver._lookup_slot_count(plan))
        finally:
            settings.lookup_max_slots_per_question = original

    def test_the_limit_still_applies_across_columns(self) -> None:
        resolver = EntityResolver()
        plan = ResolutionPlan()
        original = settings.lookup_max_slots_per_question
        settings.lookup_max_slots_per_question = 2
        try:
            self.assertTrue(
                resolver._append_lookup_slot(
                    plan, self._category("101", "category_l2_key")
                )
            )
            self.assertTrue(
                resolver._append_lookup_slot(plan, _resolved_test_slot("1013"))
            )
            # a third distinct column is refused
            self.assertFalse(
                resolver._append_lookup_slot(
                    plan, self._category("201", "category_l4_key")
                )
            )
        finally:
            settings.lookup_max_slots_per_question = original

    def test_enum_values_do_not_consume_the_budget(self) -> None:
        """A segment or a lifecycle stage is a cheap equality, not a lookup.

        The budget bounds filters drawn from the large catalogues -- 550 stores,
        5,400 categories -- where every filter is a resolution the user may have to
        be asked about. Counting enums refused ordinary questions: "how many hold an
        active NorthCo Credit card" was told "I can use at most 2 kinds of store or
        product filters", having found neither a store nor a product.
        """
        resolver = EntityResolver()
        plan = ResolutionPlan()
        original = settings.lookup_max_slots_per_question
        settings.lookup_max_slots_per_question = 2
        try:
            self.assertTrue(resolver._append_lookup_slot(plan, self._enum("Elite")))
            self.assertTrue(
                resolver._append_lookup_slot(
                    plan, self._enum("Activated", column="lifecycle_stage")
                )
            )
            self.assertTrue(
                resolver._append_lookup_slot(
                    plan, self._enum("3 Star", column="membership_tier")
                )
            )
            self.assertEqual(0, resolver._lookup_slot_count(plan))
            # ... and two real dimensions still fit alongside them
            self.assertTrue(
                resolver._append_lookup_slot(plan, _resolved_test_slot("1013"))
            )
            self.assertTrue(
                resolver._append_lookup_slot(
                    plan, self._category("101", "category_l2_key")
                )
            )
            self.assertFalse(
                resolver._append_lookup_slot(
                    plan, self._category("201", "category_l4_key")
                )
            )
        finally:
            settings.lookup_max_slots_per_question = original


# IdentityGuardPrecisionTests lived here. It pinned the false positive
# "Which customer segment contributed the most revenue?" -- the regex matched
# `which\\s+customers?` because the plural s was optional.
#
# The regex is gone. It could only ever recognise the phrasings someone had
# thought of, and "give me example of 5 customers" matched none of them; the
# planner classifies identity questions now, via SqlPlan.unsupported_cause.
# There is no pattern left to regress, and a model does not need the plural-s
# case pinned to get it right.


class QuestionVocabularyTests(unittest.TestCase):
    """One derived source for "words that describe the schema".

    Three sources feed it -- column names, metric names, metric synonyms -- because
    a question draws on all three, and each produced a real false positive by being
    fuzzy-matched to a product containing the word:

        stage    -> SCARLETEEN BRA STAGE 1     (column lifecycle_stage)
        segment  -> a category                 (column repeat_purchase_segment)
        size     -> DOG APPAREL-L SIZE         (metric synonym "basket size")

    Enum VALUES are subtracted, because a value has to stay resolvable.
    """

    def test_dimension_and_metric_words_are_included(self) -> None:
        vocabulary = question_vocabulary()
        self.assertTrue(vocabulary, "vocabulary should be derivable in tests")
        for word in ("stage", "lifecycle", "segment", "tier", "basket", "size",
                     "penetration", "daypart"):
            self.assertIn(word, vocabulary, word)

    def test_enum_values_are_excluded_so_they_stay_resolvable(self) -> None:
        """Regression: a metric synonym listed its own values.

        `customers_by_segment` carried the synonym "elite premium growth mass",
        which would have excluded exactly the words that must resolve as
        value_segment. Values win over vocabulary, whatever the seed data says.

        Both that metric and value_segment have since been removed, which moved
        "growth" out of this list: with no "Growth" segment to keep
        resolvable it is now an ordinary schema word, earned honestly by
        yoy_sales_growth and yoy_txn_growth. "elite", "premium" and "mass" left
        with it -- nothing names them any more, so asserting their absence proved
        nothing.

        What replaces them are the values that DO still collide with a column name,
        which is the only case where the subtraction does any work: "Active" is a
        member_status value and is_active_in_opco is a column; "Member" is a
        customer_type value and member_status is a column. Both must stay out of the
        vocabulary or the words stop resolving as values.
        """
        vocabulary = question_vocabulary()
        for value in ("active", "member", "churned", "platinum"):
            self.assertNotIn(value, vocabulary, value)

    def test_both_consumers_share_the_same_source(self) -> None:
        """The matcher and the identity guard must not drift apart."""
        from app.service.entity_resolution.dictionary import _schema_terms

        self.assertEqual(question_vocabulary(), _schema_terms())


class AliasParsingTests(unittest.TestCase):
    """A clause keyword after a table name is not an alias.

    `FROM RankedSegmentRevenue WHERE rn = 1` was read as the table
    RankedSegmentRevenue aliased `WHERE`, so a valid CTE query was rejected with
    "`WHERE` is a reserved word and cannot be used as a table alias" -- advice about
    a mistake it had not made, on SQL PostgreSQL would have run. Two alias parsers
    existed and only one filtered keywords; they now share `_NOT_AN_ALIAS`.
    """

    CTE_SQL = (
        "WITH SegmentRevenue AS ("
        "  SELECT EXTRACT(YEAR FROM c.month_start_date) AS sales_year,"
        "         LOWER(TRIM(c.repeat_purchase_segment)) AS customer_segment,"
        "         SUM(c.total_revenue) AS total_segment_revenue"
        "  FROM v_customer_opco_monthly AS c"
        "  WHERE c.month_start_date BETWEEN DATE '2026-01-01' AND DATE '2026-06-01'"
        "  GROUP BY sales_year, customer_segment"
        "), RankedSegmentRevenue AS ("
        "  SELECT sales_year, customer_segment, total_segment_revenue,"
        "         ROW_NUMBER() OVER (PARTITION BY sales_year"
        "                            ORDER BY total_segment_revenue DESC) AS rn"
        "  FROM SegmentRevenue"
        ") SELECT sales_year, customer_segment, total_segment_revenue"
        "  FROM RankedSegmentRevenue WHERE rn = 1 ORDER BY sales_year"
    )

    def test_the_reported_cte_query_validates(self) -> None:
        result = validate_sql(self.CTE_SQL)
        self.assertTrue(result.is_valid, result.feedback)

    def test_clause_keywords_are_not_treated_as_aliases(self) -> None:
        ok, feedback = _validate_aliases(
            _tree("SELECT y FROM v_sales_summary_daily WHERE x = 1 GROUP BY y ORDER BY y")
        )
        self.assertTrue(ok, feedback)

    def test_an_explicit_reserved_alias_is_still_caught(self) -> None:
        """`dim_opco AS do` is the real bug this check exists for."""
        ok, feedback = _validate_aliases(
            _tree("SELECT 1 FROM v_sales_summary_daily AS s JOIN dim_opco AS do ON 1 = 1")
        )
        self.assertFalse(ok)
        self.assertIn("do", feedback)

    def test_a_bare_reserved_alias_is_still_caught(self) -> None:
        ok, feedback = _validate_aliases(
            _tree("SELECT 1 FROM v_sales_summary_daily s JOIN dim_opco do ON 1 = 1")
        )
        self.assertFalse(ok)
        self.assertIn("do", feedback)

    def test_a_column_computed_inside_a_cte_is_not_checked(self) -> None:
        """`FROM RankedSegmentRevenue r ... SELECT r.sales_year` must validate.

        `sales_year` is computed inside the CTE, so no table in the glossary has it
        and none ever will. Only the CTE's own NAME was being skipped, not an alias
        bound to it, so a valid two-CTE ranking query was rejected with
        "column sales_year does not exist on the referenced tables".
        """
        aliased = self.CTE_SQL.replace(
            ") SELECT sales_year, customer_segment, total_segment_revenue"
            "  FROM RankedSegmentRevenue WHERE rn = 1 ORDER BY sales_year",
            ") SELECT r.sales_year, r.customer_segment, r.total_segment_revenue"
            "  FROM RankedSegmentRevenue r WHERE r.rn = 1 ORDER BY r.sales_year",
        )
        self.assertIn("FROM RankedSegmentRevenue r", aliased, "fixture rewrite failed")
        result = validate_sql(aliased)
        self.assertTrue(result.is_valid, result.feedback)

    def test_a_bogus_column_on_a_real_table_is_still_rejected(self) -> None:
        result = validate_sql(
            "SELECT c.does_not_exist FROM v_customer_opco_monthly AS c "
            "WHERE c.month_start_date = DATE '2026-06-01'"
        )
        self.assertFalse(result.is_valid)
        self.assertIn("does_not_exist", result.feedback)


class AstParsingTests(unittest.TestCase):
    """Shapes the hand-rolled parser could not handle.

    Three production failures came from regex parsing being wrong about VALID SQL,
    and each fix revealed the next one. These are the constructs that were queued up
    behind them; an AST handles them without a rule per shape.
    """

    def _valid(self, sql: str, label: str) -> None:
        result = validate_sql(sql)
        self.assertTrue(result.is_valid, f"{label}: {result.feedback}")

    def _rejected(self, sql: str, label: str, expect: str = "customer_key") -> None:
        result = validate_sql(sql)
        self.assertFalse(result.is_valid, label)
        self.assertIn(expect, result.feedback, label)

    def test_window_function(self) -> None:
        self._valid(
            "SELECT s.opco_code, SUM(s.transaction_count) t, "
            "RANK() OVER (PARTITION BY s.opco_code ORDER BY SUM(s.transaction_count) DESC) r "
            "FROM v_sales_summary_daily s WHERE s.calendar_date > DATE '2026-01-01' "
            "GROUP BY s.opco_code",
            "window function",
        )

    def test_union_all(self) -> None:
        self._valid(
            "SELECT SUM(a.transaction_count) t FROM v_sales_summary_daily a "
            "WHERE a.calendar_date = DATE '2026-06-01' UNION ALL "
            "SELECT SUM(b.transaction_count) FROM v_sales_summary_daily b "
            "WHERE b.calendar_date = DATE '2026-06-02'",
            "UNION ALL",
        )

    def test_quoted_alias(self) -> None:
        self._valid(
            'SELECT SUM(s.transaction_count) AS "Total Txns" FROM v_sales_summary_daily AS s '
            "WHERE s.calendar_date > DATE '2026-06-01'",
            "quoted alias",
        )

    def test_derived_table(self) -> None:
        self._valid(
            "SELECT x.seg FROM (SELECT c.repeat_purchase_segment seg FROM v_customer_opco_monthly c "
            "WHERE c.month_start_date = DATE '2026-06-01') x",
            "derived table",
        )

    def test_identity_nested_two_subqueries_deep(self) -> None:
        """Position in the tree, not what is left after stripping legal uses."""
        self._rejected(
            "SELECT y.k FROM (SELECT x.k FROM (SELECT c.customer_key k "
            "FROM v_customer_opco_monthly c WHERE c.month_start_date = DATE '2026-06-01') x) y",
            "identity nested two levels deep",
        )

    def test_identity_inside_a_window_order_by(self) -> None:
        self._rejected(
            "SELECT COUNT(*) n, ROW_NUMBER() OVER (ORDER BY c.customer_key) r "
            "FROM v_customer_opco_monthly c WHERE c.month_start_date = DATE '2026-06-01'",
            "identity in a window ORDER BY",
        )

    def test_unparseable_sql_is_refused_not_run(self) -> None:
        """A parser that cannot read the query cannot vouch for it either."""
        result = validate_sql("SELECT FROM WHERE ((( GROUP")
        self.assertFalse(result.is_valid)
        self.assertIn("could not be parsed", result.feedback)

    def test_count_star_is_not_select_star(self) -> None:
        self._valid(
            "SELECT COUNT(*) AS n FROM v_sales_summary_daily s "
            "WHERE s.calendar_date > DATE '2026-06-01'",
            "COUNT(*)",
        )

    def test_aggregation_rule_sees_through_distinct(self) -> None:
        result = validate_sql(
            "SELECT SUM(DISTINCT s.customer_count) n FROM v_sales_store_monthly s "
            "WHERE s.month_start_date = DATE '2026-06-01'"
        )
        self.assertFalse(result.is_valid)
        self.assertIn("customer_count", result.feedback)










class IdentityAsGroupingKeyTests(unittest.TestCase):
    """Identity may be a grouping key internally; it may never be returned.

    The strict rule -- COUNT(DISTINCT) or a join equality, nothing else -- blocked the
    only correct way to answer a multi-month overlap question, so
    "customers who hold A but not B across a quarter" was unanswerable even for a
    group user with full access. What matters is whether an identifier reaches the
    user, which is a question about position in the query.
    """


    def test_a_grouped_subquery_counted_by_the_outer_query_is_allowed(self) -> None:
        result = validate_sql(
            "SELECT count(*) AS n FROM ("
            "  SELECT c.customer_key FROM v_customer_opco_monthly c "
            "  WHERE c.month_start_date > DATE '2026-01-01' GROUP BY c.customer_key "
            "  HAVING bool_or(c.opco_code = 'NORTHCO_CREDIT')"
            ") x",
        )
        self.assertTrue(result.is_valid, result.feedback)

    def test_everything_that_could_return_an_identifier_is_still_refused(self) -> None:
        for sql, label in [
            ("SELECT x.customer_key FROM (SELECT c.customer_key FROM v_customer_opco_monthly c "
             "WHERE c.month_start_date > DATE '2026-01-01' GROUP BY c.customer_key) x",
             "outer projects identity"),
            ("SELECT max(c.customer_key) AS k FROM v_customer_opco_monthly c "
             "WHERE c.month_start_date > DATE '2026-01-01'",
             "max(identity)"),
            ("SELECT count(*) AS n FROM (SELECT max(c.customer_key) AS k "
             "FROM v_customer_opco_monthly c WHERE c.month_start_date > DATE '2026-01-01') x",
             "identity computed then aliased out"),
            ("SELECT c.customer_key FROM v_customer_opco_monthly c "
             "WHERE c.month_start_date = DATE '2026-01-01'",
             "plain projection"),
            ("SELECT COUNT(*) FROM v_customer_opco_monthly c "
             "WHERE c.month_start_date = DATE '2026-01-01' GROUP BY c.customer_key",
             "GROUP BY at the outermost -- a customer-level result set"),
            ("WITH x AS (SELECT c.customer_key k FROM v_customer_opco_monthly c "
             "WHERE c.month_start_date = DATE '2026-01-01') SELECT x.k FROM x",
             "CTE identity returned under its alias"),
            ("SELECT k FROM (SELECT substr(CAST(c.customer_key AS TEXT), 1, 6) AS k "
             "FROM v_customer_opco_monthly c WHERE c.month_start_date = DATE '2026-01-01') x",
             "identity computed into a partial identifier, then returned"),
        ]:
            result = validate_sql(sql)
            self.assertFalse(result.is_valid, label)
            self.assertIn("identity", result.feedback, label)

    def test_a_semi_join_over_identity_is_allowed(self) -> None:
        """The shape every "compare A-doers with non-A-doers" question needs.

        Refusing an inner projection unless it grouped by itself blocked this, and
        the copilot answered "the data model does not support" to "how does retail
        spend compare between members who use NorthCo Bank and those who do not" -- a
        question the data model supports perfectly well. Nothing identifying is
        returned: the CTE is a set to join against and the outer query aggregates.
        """
        sql = (
            "WITH bank AS ("
            "  SELECT b.customer_key FROM v_customer_opco_monthly b "
            "  WHERE b.opco_code = 'NORTHCO_BANK' AND b.is_active_in_opco "
            "  AND b.month_start_date = DATE '2026-06-01'"
            ") "
            "SELECT bank_user, SUM(spend) AS total_spend FROM ("
            "  SELECT CASE WHEN k.customer_key IS NULL THEN 'no' ELSE 'yes' END AS bank_user, "
            "         r.total_revenue AS spend "
            "  FROM v_customer_opco_monthly r "
            "  LEFT JOIN bank k ON k.customer_key = r.customer_key "
            "  WHERE r.opco_code IN ('NORTHCO','NORTHCO_MART') "
            "  AND r.month_start_date = DATE '2026-06-01'"
            ") x GROUP BY bank_user"
        )

        result = validate_sql(sql)

        self.assertTrue(result.is_valid, result.feedback)

    def test_a_flag_computed_from_identity_may_be_returned(self) -> None:
        """CASE over an identity yields 'yes'/'no', which gives nothing away.

        The planner reaches for this constantly -- it is the natural way to split a
        population into A-doers and everyone else -- and refusing it on the mere
        presence of the column sent "compare retail spend between NorthCo Bank members
        and non-members" round the retry loop until it gave up.
        """
        sql = (
            "SELECT CASE WHEN b.customer_key IS NULL THEN 'no' ELSE 'yes' END AS bank_user, "
            "SUM(r.total_revenue) AS spend "
            "FROM v_customer_opco_monthly AS r "
            "LEFT JOIN v_customer_opco_monthly AS b ON b.customer_key = r.customer_key "
            "WHERE r.month_start_date = DATE '2026-06-01' GROUP BY 1"
        )

        result = validate_sql(sql)

        self.assertTrue(result.is_valid, result.feedback)

    def test_identity_in_an_outermost_predicate_is_allowed(self) -> None:
        """A WHERE returns nothing; only the projection and the grouping do."""
        sql = (
            "SELECT COUNT(DISTINCT r.customer_key) AS n FROM v_customer_opco_monthly r "
            "WHERE r.month_start_date = DATE '2026-06-01' AND r.customer_key IN ("
            "  SELECT b.customer_key FROM v_customer_opco_monthly b "
            "  WHERE b.opco_code = 'NORTHCO_BANK' AND b.month_start_date = DATE '2026-06-01'"
            ")"
        )

        result = validate_sql(sql)

        self.assertTrue(result.is_valid, result.feedback)




class OrdinaryEnglishIsNotAProductTests(unittest.TestCase):
    """The question's verbs and nouns must not be read as catalogue names.

    Every case here comes from one report: "How many hold an active NorthCo Credit
    Card?" -- eight words, no store and no product -- was refused with "I can use
    at most 2 kinds of store or product filters in one question, and this one has
    3: SOFT > SBA > BAG > CREDIT CARD HOLDER, Active, Card."
    """

    @staticmethod
    def _catalogue() -> EntityDictionary:
        dictionary = _dictionary(
            _opco("NORTHCO_CREDIT", "NorthCo Credit"),
            _category_l4("5769", "SOFT > SBA > BAG > CREDIT CARD HOLDER"),
            _category_l4("5796", "SOFT > SBA > BAG > NAME CARD HOLDER"),
            _enum_value("Card", "payment_type"),
            _enum_value("Active", "member_status"),
            _enum_value("Churned", "lifecycle_stage"),
            _enum_value("Moved Closer", "nearest_store_change_status"),
            _enum_value("Moved Further", "nearest_store_change_status"),
        )
        # The display path is what the catalogue indexes; the leaf name is what a
        # user would type, and both must be present for this to be a fair test.
        _add(dictionary, "credit card holder", _category_l4("5769", "SOFT > SBA > BAG > CREDIT CARD HOLDER"))
        _add(dictionary, "name card holder", _category_l4("5796", "SOFT > SBA > BAG > NAME CARD HOLDER"))
        return dictionary

    def test_the_verb_hold_is_not_a_bag(self) -> None:
        spans = _resolve("how many hold an active northco credit card", self._catalogue())
        classes = {span.best.entity_class for span in spans if span.best}
        self.assertNotIn("category_l4", classes)

    def test_the_brand_tail_does_not_become_a_payment_filter(self) -> None:
        """"NorthCo Credit Card" is one name, so it produces one filter."""
        spans = _resolve("how many hold an active northco credit card", self._catalogue())
        resolved = {(s.best.entity_class, s.best.canonical_value) for s in spans if s.best}
        self.assertIn(("opco", "NORTHCO_CREDIT"), resolved)
        self.assertNotIn(("enum", "Card"), resolved)

    def test_card_still_resolves_when_it_is_the_subject(self) -> None:
        """The tail rule is about adjacency to an OpCo, not about the word."""
        spans = _resolve("how many customers pay by card", self._catalogue())
        resolved = {(s.best.entity_class, s.best.canonical_value) for s in spans if s.best}
        self.assertIn(("enum", "Card"), resolved)

    def test_one_word_needs_a_spelling_anchor_not_a_shared_stem(self) -> None:
        """"hold" is 0.80 similar to "holder"; a typo of a real name is 0.9+."""
        self.assertEqual([], _resolve("customers who hold more", self._catalogue()))
        churn = _resolve("what is the churn rate", self._catalogue())
        self.assertEqual(
            [("enum", "Churned")],
            [(s.best.entity_class, s.best.canonical_value) for s in churn],
        )

    def test_a_partial_enum_match_is_not_offered_as_a_choice(self) -> None:
        """An enum resolves confidently or not at all -- there is no list to pick from."""
        self.assertEqual(
            [], _resolve("how many customers moved to a lower tier", self._catalogue())
        )

    def test_a_schema_word_cannot_ride_along_with_a_neighbour(self) -> None:
        dictionary = _dictionary(
            _category_l4("101", "HARD > HOME FASHION > BEDDING > AKEMI"),
            _category_l4("102", "HARD > HOME FASHION > BEDDING > AVANTI"),
        )
        dictionary.schema_terms = frozenset({"fashion"})
        _add(dictionary, "home fashion bedding akemi", _category_l4("101", "HARD > HOME FASHION > BEDDING > AKEMI"))

        # "fashion" alone was already suppressed as schema vocabulary; the bug was
        # that ("buying", "fashion") was not, so a worse reading won by default.
        self.assertEqual(
            [], _resolve("customers who stopped buying fashion", dictionary)
        )


class EnumColumnChoiceTests(unittest.TestCase):
    """One value, several columns: choose, and say what else it could have been.

    "Active" is a lifecycle_stage, a member_status and a customer_status_in_opco;
    "ACS Credit" is both a payment_type and a primary_payment_type. They reached
    the planner as a single pinned filter chosen by nothing but row order, so a
    question about the membership being active could be answered with whether the
    customer shopped that month.

    The original case was "Elite", which was both value_segment and
    previous_value_segment until the prior-period columns were dropped from the
    schema. No pair in the schema now differs by a `previous` prefix, so the
    examples moved to pairs that still exist -- the ranking itself never knew about
    `previous`, it only counts how many words of a column name the question used.
    """

    @staticmethod
    def _span(value: str, *columns: str) -> EntitySpan:
        return EntitySpan(
            text=value.lower(),
            normalized=value.lower(),
            start_token=0,
            end_token=1,
            candidates=[_enum_value(value, column) for column in columns],
        )

    def test_the_word_the_question_used_picks_the_column(self) -> None:
        span = self._span("Active", "customer_status_in_opco", "member_status")
        collapse_enum_columns(span, frozenset({"member", "status"}))

        self.assertEqual(1, len(span.candidates))
        self.assertEqual("member_status", span.candidates[0].target_column)
        self.assertEqual(
            ("customer_status_in_opco",), span.candidates[0].alternate_columns
        )

    def test_asking_for_the_qualified_one_gets_the_qualified_one(self) -> None:
        span = self._span("ACS Credit", "payment_type", "primary_payment_type")
        collapse_enum_columns(span, frozenset({"primary", "payment"}))

        self.assertEqual("primary_payment_type", span.candidates[0].target_column)

    def test_the_plain_column_wins_when_the_question_says_nothing(self) -> None:
        span = self._span("ACS Credit", "primary_payment_type", "payment_type")
        collapse_enum_columns(span, frozenset({"acs"}))

        self.assertEqual("payment_type", span.candidates[0].target_column)

    def test_the_alternatives_reach_the_planner(self) -> None:
        result = ResolutionResult(
            plan=ResolutionPlan(
                slots=[
                    ResolvedSlot(
                        slot_id="enum:active",
                        phrase="active",
                        entity_class="enum",
                        target_column="member_status",
                        status="resolved",
                        canonical_value="Active",
                        display_value="Active",
                        alternate_columns=["customer_status_in_opco"],
                    )
                ]
            ),
            spans=[],
        )

        context = result.filter_context()
        self.assertIn("member_status = 'Active'", context)
        self.assertIn("customer_status_in_opco", context)

    def test_the_catalogue_keeps_every_column_for_a_shared_value(self) -> None:
        """The dictionary used to keep only the first, and the query has no ORDER BY."""
        dictionary = _dictionary(
            _enum_value("Active", "member_status"),
            _enum_value("Active", "customer_status_in_opco"),
        )

        self.assertEqual(
            {"member_status", "customer_status_in_opco"},
            {c.target_column for c in dictionary.lookup_exact("active")},
        )


class OneNameIsOneEntityTests(unittest.TestCase):
    """A name the catalogue only partly knows must not become two entities.

    "NorthCo Inglegate Juniperford Megamall" is one store. Longest-match tagged "inglegate juniperford"
    exactly and left "megamall" over, the fuzzy pass read that as a SECOND store and
    offered WELLNESS JESSAMRIDGE MEGAMALL, and answering the first slot then emptied
    the second by OpCo pin -- so a question naming one place was refused with "I
    could not find megamall within NORTHCO_MART".
    """

    @staticmethod
    def _stores() -> EntityDictionary:
        return _dictionary(
            _store("1007", "INGLEGATE JUNIPERFORD", opco_code="NORTHCO"),
            _store("1016", "NORTHCO MART INGLEGATE JUNIPERFORD", opco_code="NORTHCO_MART"),
            _store("2201", "WELLNESS JESSAMRIDGE MEGAMALL", opco_code="NORTHCO"),
        )

    def _resolve(self, query: str, dictionary: EntityDictionary):
        spans, leftover = match_exact(query, dictionary)
        return spans + match_fuzzy(leftover, dictionary, exact_spans=spans)

    def test_a_trailing_word_of_a_store_name_is_not_a_second_store(self) -> None:
        spans = self._resolve("sales at northco inglegate juniperford megamall", self._stores())
        stores = [s for s in spans if s.best and s.best.entity_class == "store"]

        self.assertEqual(1, len(stores), [s.text for s in stores])
        self.assertEqual("inglegate juniperford", stores[0].text)

    def test_a_neighbouring_word_of_another_class_still_resolves(self) -> None:
        """Adjacency only silences the SAME kind of entity."""
        dictionary = self._stores()
        _add(dictionary, "grocery", _category_l4("900", "FOOD > GROCERY", opco_code="NORTHCO"))

        spans = self._resolve("inglegate juniperford grocery", dictionary)
        families = {
            "category" if s.best.entity_class.startswith("category_") else s.best.entity_class
            for s in spans
            if s.best
        }
        self.assertEqual({"store", "category"}, families)

    def test_an_emptied_slot_of_an_already_resolved_class_is_dropped(self) -> None:
        """The pin cannot refuse half of a name the user just confirmed."""
        resolver = EntityResolver()
        plan = ResolutionPlan(
            slots=[
                ResolvedSlot(
                    slot_id="store:inglegate juniperford",
                    phrase="inglegate juniperford",
                    entity_class="store",
                    target_column="store_id",
                    status="resolved",
                    canonical_value="1016",
                    display_value="NORTHCO MART INGLEGATE JUNIPERFORD",
                    opco_code="NORTHCO_MART",
                ),
                ResolvedSlot(
                    slot_id="store:megamall",
                    phrase="megamall",
                    entity_class="store",
                    target_column="store_id",
                    status="ambiguous",
                    options=[],
                ),
            ]
        )

        result = resolver._finalize(plan, [])

        self.assertFalse(result.needs_clarification, result.clarification_question)
        self.assertEqual(["inglegate juniperford"], [s.phrase for s in result.plan.slots])

    def test_a_genuinely_impossible_pairing_is_still_refused_by_name(self) -> None:
        """A store and a product from different OpCos is still a real refusal."""
        resolver = EntityResolver()
        plan = ResolutionPlan(
            slots=[
                ResolvedSlot(
                    slot_id="store:inglegate juniperford",
                    phrase="inglegate juniperford",
                    entity_class="store",
                    target_column="store_id",
                    status="resolved",
                    canonical_value="1016",
                    display_value="NORTHCO MART INGLEGATE JUNIPERFORD",
                    opco_code="NORTHCO_MART",
                ),
                ResolvedSlot(
                    slot_id="category_l4:lamb",
                    phrase="lamb",
                    entity_class="category_l4",
                    target_column="category_l4_key",
                    status="ambiguous",
                    options=[],
                ),
            ]
        )

        result = resolver._finalize(plan, [])

        self.assertTrue(result.needs_clarification)
        self.assertIn("lamb", result.clarification_question)
        self.assertIn("product", result.clarification_question)


class SelectedOptionCarriesItsOwnClassTests(unittest.TestCase):
    """Picking an option adopts that option's identity, class included.

    A slot is built from the span's BEST candidate, and the user often picks a
    different one: "fashion" offers HARD > HOME FASHION at level 2 and FASHION
    ACCESSORIES at level 4. Everything but entity_class moved across, so the slot
    read category_l2 while its target_column had become category_l4_key -- and
    _category_depth_notes reads the level out of entity_class to decide whether the
    customer tables can answer at that depth. The stale class silenced the warning
    for exactly the case it exists to catch.
    """

    @staticmethod
    def _slot() -> ResolvedSlot:
        return ResolvedSlot(
            slot_id="category_l2:fashion",
            phrase="fashion",
            entity_class="category_l2",
            target_column="category_l2_key",
            status="ambiguous",
            options=[
                {
                    "entity_class": "category_l2",
                    "canonical_value": "1200",
                    "display_value": "HARD > HOME FASHION",
                    "target_column": "category_l2_key",
                    "category_key": 1200,
                    "opco_code": "NORTHCO",
                    "match_kind": "fuzzy",
                },
                {
                    "entity_class": "category_l4",
                    "canonical_value": "3766",
                    "display_value": "HARD > PAGEMARK > NON BOOKS > FASHION ACCESSORIES",
                    "target_column": "category_l4_key",
                    "category_key": 3766,
                    "opco_code": "NORTHCO",
                    "match_kind": "fuzzy",
                },
            ],
        )

    def test_a_deeper_pick_updates_the_class_too(self) -> None:
        resolver = EntityResolver()
        plan = ResolutionPlan(slots=[self._slot()])

        self.assertTrue(resolver._apply_selection(plan, "2", _dictionary()))

        slot = plan.slots[0]
        self.assertEqual("category_l4", slot.entity_class)
        self.assertEqual("category_l4_key", slot.target_column)
        self.assertEqual(3766, slot.category_key)

    def test_no_warning_now_that_the_bridge_carries_every_level(self) -> None:
        """A level-4 pick is answerable exactly, so there is nothing to disclose."""
        resolver = EntityResolver()
        plan = ResolutionPlan(slots=[self._slot()])
        resolver._apply_selection(plan, "2", _dictionary())

        self.assertEqual(4, CATEGORY_DEPTH["bridge_customer_category_monthly"])
        self.assertEqual(
            [],
            LookupNodesMixin._category_depth_notes(
                ResolutionResult(plan=plan, spans=[])
            ),
        )

    def test_the_guard_still_fires_if_the_bridge_depth_is_reduced(self) -> None:
        """The warning tracks the schema rather than asserting a number.

        Kept because the failure it caught was severe and silent: while the bridge
        held levels 1-2, a level-4 pick was answered with its level-2 ancestor and
        narrated as the leaf.
        """
        resolver = EntityResolver()
        plan = ResolutionPlan(slots=[self._slot()])
        resolver._apply_selection(plan, "2", _dictionary())

        with mock.patch.object(lookup_nodes, "CUSTOMER_CATEGORY_DEPTH", 2):
            notes = LookupNodesMixin._category_depth_notes(
                ResolutionResult(plan=plan, spans=[])
            )

        self.assertEqual(1, len(notes))
        self.assertIn("CATEGORY DEPTH LIMIT", notes[0])
        self.assertIn("FASHION ACCESSORIES", notes[0])


class StoresAreAlternativesNotConjunctionsTests(unittest.TestCase):
    """Two stores compared is a different shape from a store plus a product.

    The OpCo pin exists because a store AND a category have to sit on the same row,
    so they must share an OpCo. Two stores do not: they are alternatives in one
    `store_id IN (...)`, and comparing branches across banners is ordinary. Pinning
    them alike made "NORTHCO MART VELDRA SELBYCROSS compared with GLEDEHOLT VANTRYTON" unanswerable --
    picking the NorthCo Mart store emptied every GLEDEHOLT VANTRYTON option, since all of them
    are NorthCo, and the next turn asked again with nothing to offer.
    """

    @staticmethod
    def _plan() -> ResolutionPlan:
        return ResolutionPlan(
            slots=[
                ResolvedSlot(
                    slot_id="store:veldra selbycross",
                    phrase="veldra selbycross",
                    entity_class="store",
                    target_column="store_id",
                    status="resolved",
                    canonical_value="1013",
                    display_value="NORTHCO MART VELDRA SELBYCROSS",
                    opco_code="NORTHCO_MART",
                ),
                ResolvedSlot(
                    slot_id="store:gledeholt vantryton",
                    phrase="gledeholt vantryton",
                    entity_class="store",
                    target_column="store_id",
                    status="ambiguous",
                    options=[
                        {
                            "entity_class": "store",
                            "canonical_value": "1004",
                            "display_value": "GLEDEHOLT VANTRYTON",
                            "target_column": "store_id",
                            "opco_code": "NORTHCO",
                            "match_kind": "exact",
                        }
                    ],
                ),
            ]
        )

    def test_a_store_in_another_opco_survives_the_pin(self) -> None:
        plan = self._plan()
        EntityResolver._apply_opco_pin(plan)

        options = plan.slots[1].options
        self.assertEqual(1, len(options), "the second store must still be offerable")
        self.assertEqual("GLEDEHOLT VANTRYTON", options[0]["display_value"])

    def test_a_product_in_another_opco_is_still_removed(self) -> None:
        """The conjunction case the pin exists for is unchanged."""
        plan = self._plan()
        plan.slots[1] = ResolvedSlot(
            slot_id="category_l4:lamb",
            phrase="lamb",
            entity_class="category_l4",
            target_column="category_l4_key",
            status="ambiguous",
            options=[
                {
                    "entity_class": "category_l4",
                    "canonical_value": "2479",
                    "display_value": "FRESH > FRESH > LAMB",
                    "target_column": "category_l4_key",
                    "opco_code": "NORTHCO",
                    "match_kind": "exact",
                }
            ],
        )

        EntityResolver._apply_opco_pin(plan)

        self.assertEqual([], plan.slots[1].options)

    def test_a_named_opco_still_pins_a_store(self) -> None:
        """"NorthCo Mart Veldra Selbycross" -- the OpCo was said out loud, so it narrows."""
        plan = self._plan()
        plan.slots[0] = ResolvedSlot(
            slot_id="opco:northco mart",
            phrase="northco mart",
            entity_class="opco",
            target_column="opco_code",
            status="resolved",
            canonical_value="NORTHCO_MART",
            opco_code="NORTHCO_MART",
        )

        EntityResolver._apply_opco_pin(plan)

        self.assertEqual([], plan.slots[1].options)


class RouterBoundedGuessingTests(unittest.TestCase):
    """The router says which words NAME something; only guessing is bounded by it.

    Deciding whether "linked", "hold" or "brands" is naming a product is a
    judgement about the sentence, and string similarity cannot make it -- "linked"
    scored 71.0 against Appliance, Bazaar, FOOD, Fresh and Grocery alike. Deciding
    WHICH of 5,900 scoped rows a real name means is the opposite: a closed-set
    lookup that also carries the caller's scope. So the model bounds the first and
    never touches the second.
    """

    @staticmethod
    def _dict() -> EntityDictionary:
        dictionary = _dictionary(
            _store("1007", "INGLEGATE JUNIPERFORD", opco_code="NORTHCO"),
            _category_l4("900", "FOOD > GROCERY", opco_code="NORTHCO"),
        )
        _add(dictionary, "grocery", _category_l4("900", "FOOD > GROCERY", opco_code="NORTHCO"))
        return dictionary

    def test_a_run_the_router_did_not_name_is_not_guessed_at(self) -> None:
        tokens = ["linked", "grocry"]
        runs = [TokenRun(start=0, tokens=("linked", "grocry"))]

        kept = restrict_runs(runs, frozenset({"grocry"}))

        self.assertEqual([("grocry",)], [r.tokens for r in kept])
        self.assertEqual([1], [r.start for r in kept], "offsets must survive the trim")
        self.assertEqual(2, len(tokens))

    def test_a_named_phrase_survives_whole(self) -> None:
        runs = [TokenRun(start=3, tokens=("veldra", "selbycross"))]

        kept = restrict_runs(runs, frozenset({"veldra", "selbycross"}))

        self.assertEqual([("veldra", "selbycross")], [r.tokens for r in kept])
        self.assertEqual(3, kept[0].start)

    def test_nothing_named_means_nothing_guessed(self) -> None:
        runs = [TokenRun(start=0, tokens=("linked",))]

        self.assertEqual([], restrict_runs(runs, frozenset()))

    def test_exact_matches_are_never_gated(self) -> None:
        """A router that misses a phrase must not be able to drop a filter.

        A missing filter is a wrong number; a missing guess is at worst a question
        left unasked. So exact matching runs over the raw tokens regardless.
        """
        spans, _ = match_exact("customers at inglegate juniperford who bought grocery", self._dict())
        resolved = {(s.best.entity_class, s.best.display_value) for s in spans if s.best}

        self.assertIn(("store", "INGLEGATE JUNIPERFORD"), resolved)
        self.assertIn(("category_l4", "FOOD > GROCERY"), resolved)


class SynthesisedAliasTokensAreWeakTests(unittest.TestCase):
    """"line" is added by the loader, not read from the data, so it names nothing.

    division_aliases turns a one-word level 1 name into "hardline", "hard line",
    "hardlines", "hard lines" because the trade says hardline and the catalogue says
    HARD. Document frequency cannot see those as weak -- they are synthesised, over
    only 16 divisions -- so "line" looked as distinguishing as "appliance", and
    every division tied at 71.0 for a query containing "linked".
    """

    def test_a_word_resembling_line_no_longer_matches_every_division(self) -> None:
        dictionary = EntityDictionary(scope_signature="test")
        dictionary.weak_tokens = {"category_l1": set()}
        for name in ("appliance", "bazaar", "grocery"):
            candidate = EntityCandidate(
                entity_class="category_l1",
                canonical_value=name,
                display_value=name.title(),
                target_column="category_l1_key",
                score=100.0,
                match_kind="exact",
                opco_code="NORTHCO_MART",
                category_level=1,
            )
            for alias in {name, *division_aliases(name)}:
                _add(dictionary, alias, candidate)

        spans = match_fuzzy(
            [TokenRun(start=0, tokens=("linked",))], dictionary
        )

        self.assertEqual([], spans, [(s.text, s.best.display_value) for s in spans])

    def test_the_trade_name_still_resolves(self) -> None:
        """The aliases exist for a reason: "hardline" must still find HARD."""
        self.assertIn("hardline", division_aliases("hard"))
        self.assertIn("hard lines", division_aliases("hard"))


class InternalInstructionsNeverResolveTests(unittest.TestCase):
    """The copilot's own words to itself must not become filters.

    Answering a clarification merges the original question with instruction lines
    the copilot writes for its own planner. Those lines were matched for removal by
    literal phrase, and the phrases had drifted: the builder wrote "User confirmed
    the pending clarification. Proceed with the already clarified request." while
    the removal list held "resolved pending clarification" and "proceed to sql
    planning". Close enough to look maintained, different enough to match nothing.

    So a bare "yes" reached the resolver carrying the word "proceed", which
    fuzzy-matched FOOD > PERISHABLE > SEAFOOD > PROCESSED and became a hard filter
    on seafood in a question about a date range.
    """

    HISTORY = [
        {"role": "user", "content": "i want this specific period please"},
        {
            "role": "assistant",
            "content": json.dumps(
                {"type": "clarify", "answer": "Do you want transaction count for 1 Jan 2026 instead?"}
            ),
        },
    ]
    PAYLOAD = {"type": "clarify", "answer": "Do you want transaction count for 1 Jan 2026 instead?"}

    def test_the_builder_marks_every_line_it_writes(self) -> None:
        merged, is_followup, _ = build_clarification_followup_query(
            current_query="yes", history=self.HISTORY, previous_payload=self.PAYLOAD
        )

        self.assertTrue(is_followup)
        synthetic = [ln for ln in merged.splitlines() if ln.strip() != self.HISTORY[0]["content"]]
        self.assertTrue(synthetic, "the merge should add an instruction line")
        for line in synthetic:
            self.assertIn(INTERNAL_LINE_MARKER, line)

    def test_the_marked_line_never_reaches_the_matcher(self) -> None:
        merged, _, _ = build_clarification_followup_query(
            current_query="yes", history=self.HISTORY, previous_payload=self.PAYLOAD
        )

        cleaned = strip_internal_lookup_meta(merged)

        self.assertEqual("i want this specific period please", cleaned)
        self.assertNotIn("proceed", cleaned)
        self.assertNotIn("clarification", cleaned)

    def test_a_marker_is_stripped_whatever_the_wording(self) -> None:
        """The point of the marker: it cannot drift out of step with a list."""
        cleaned = strip_internal_lookup_meta(
            f"how many customers\n{INTERNAL_LINE_MARKER} some future instruction nobody has written yet"
        )

        self.assertEqual("how many customers", cleaned)


class ClarificationRepliesBoundGuessingTests(unittest.TestCase):
    """On a clarification turn, only the words the user just typed are guessable.

    The router is deliberately skipped for these replies, so it cannot say what the
    turn names -- and the query it would have read is a MERGED one carrying the
    whole original question. Everything that question named was resolved, or asked
    about, on the turn before and is carried in the plan, so re-guessing over it can
    only invent slots: "i want this specific period please" offered a FLOOR SPECIFIC
    product after the user answered "yes".
    """

    def test_the_original_questions_words_are_not_re_guessed(self) -> None:
        dictionary = _dictionary(
            _category_l4("2756", "H&BC > BEAUTY CARE > SPECIFIC TREATMENT", opco_code="NORTHCO"),
        )
        _add(dictionary, "specific treatment", _category_l4("2756", "H&BC > BEAUTY CARE > SPECIFIC TREATMENT", opco_code="NORTHCO"))

        _, leftover = match_exact("i want this specific period please", dictionary)

        # What the turn is bounded to: the user typed "yes", and nothing else.
        self.assertEqual([], restrict_runs(leftover, frozenset({"yes"})))
        # Ungated, the same run is still offered up for guessing.
        self.assertTrue(leftover)


class BracketedGlossTests(unittest.TestCase):
    """Words in brackets restate what the sentence already said.

    "combined revenue across NorthCo, NorthCo Mart and NorthCo Credit in the Southern
    Region (Fenwickholt/Tannermere)" resolved store_location = 'Southern' from outside the
    brackets, which is right, and then matched TANNERMERE inside them -- a real store,
    exactly named, score 100. A question about a whole region came back as "which
    TANNERMERE store did you mean?".
    """

    @staticmethod
    def _span(text: str, start: int, end: int, candidate: EntityCandidate) -> EntitySpan:
        return EntitySpan(
            text=text, normalized=text, start_token=start, end_token=end, candidates=[candidate]
        )

    @staticmethod
    def _region() -> EntityCandidate:
        return EntityCandidate(
            entity_class="enum",
            canonical_value="Southern",
            display_value="Southern",
            target_column="store_location",
            score=100.0,
            match_kind="exact",
        )

    def test_a_bracketed_place_does_not_narrow_a_named_region(self) -> None:
        spans = [
            self._span("southern", 0, 1, self._region()),
            self._span("tannermere", 2, 3, _store("1004", "TANNERMERE", opco_code="NORTHCO")),
        ]

        kept = drop_bracketed_glosses(spans, {2})

        self.assertEqual(["southern"], [s.text for s in kept])

    def test_a_bracketed_store_survives_when_nothing_outside_names_a_place(self) -> None:
        """"NorthCo Mart (Veldra Selbycross)" -- the outside names an OpCo, which is a scope."""
        opco = _opco("NORTHCO_MART", "NorthCo Mart")
        spans = [
            self._span("northco mart", 0, 2, opco),
            self._span("veldra selbycross", 2, 4, _store("1013", "NORTHCO MART VELDRA SELBYCROSS")),
        ]

        kept = drop_bracketed_glosses(spans, {2, 3})

        self.assertEqual(["northco mart", "veldra selbycross"], [s.text for s in kept])

    def test_a_place_named_on_its_own_is_untouched(self) -> None:
        """Nothing is in brackets, so nothing is a gloss."""
        spans = [self._span("tannermere", 3, 4, _store("1004", "TANNERMERE", opco_code="NORTHCO"))]

        self.assertEqual(spans, drop_bracketed_glosses(spans, set()))

    def test_bracket_positions_line_up_with_the_normalized_tokens(self) -> None:
        query = "revenue in the Southern Region (Fenwickholt/Tannermere) for 2026"
        tokens = normalize(query).split()

        bracketed = parenthesised_token_indices(query)

        self.assertEqual(["fenwickholt", "tannermere"], [tokens[i] for i in sorted(bracketed)])

    def test_brackets_of_every_shape_count(self) -> None:
        query = "spend in Central [KL] and North {Yarrowburn}"
        tokens = normalize(query).split()

        bracketed = parenthesised_token_indices(query)

        self.assertEqual(["kl", "yarrowburn"], [tokens[i] for i in sorted(bracketed)])


class UnionOrderByTests(unittest.TestCase):
    """A set operation's ORDER BY is resolved against the result, not a branch.

    The planner wrote `... FROM WangsaMajuData AS d UNION ALL ... FROM
    BandarUtamaData AS d ORDER BY d.store_name`, which looks fine and is not:
    PostgreSQL answers "missing FROM-clause entry for table d". Every rule here
    passed it, so it failed in the database, and the retry advice -- "use table
    alias.column format" -- pointed the wrong way and it wrote the same SQL again.
    """

    BRANCH = (
        "SELECT 'a' AS store_name, SUM(s.gross_sales_amount) AS rev "
        "FROM v_sales_daily AS s WHERE s.calendar_date = DATE '2026-06-01' GROUP BY 1"
    )

    def _union(self, tail: str) -> str:
        return f"{self.BRANCH} UNION ALL {self.BRANCH} {tail}".strip()

    def test_an_alias_qualified_order_by_is_refused_before_the_database_sees_it(self) -> None:
        result = validate_sql(self._union("ORDER BY d.store_name"))

        self.assertFalse(result.is_valid)
        self.assertIn("UNION", result.feedback)
        self.assertIn("store_name", result.feedback)

    def test_the_output_column_name_is_the_fix_the_feedback_names(self) -> None:
        result = validate_sql(self._union("ORDER BY store_name"))

        self.assertTrue(result.is_valid, result.feedback)

    def test_a_union_without_ordering_is_untouched(self) -> None:
        self.assertTrue(validate_sql(self._union("")).is_valid)

    def test_an_alias_qualified_order_by_on_a_plain_select_is_fine(self) -> None:
        """The rule is about set operations only -- a single SELECT resolves aliases."""
        sql = (
            "SELECT s.store_id, SUM(s.gross_sales_amount) AS rev FROM v_sales_daily AS s "
            "WHERE s.calendar_date = DATE '2026-06-01' GROUP BY s.store_id ORDER BY s.store_id"
        )

        self.assertTrue(validate_sql(sql).is_valid)


class CrossOpcoCategoryNoteTests(unittest.TestCase):
    """A category key belongs to one OpCo; a two-store comparison may span two.

    NorthCo Mart's Grocery and NorthCo's FOOD > GROCERY are different nodes with
    different keys. Comparing a store in each is ordinary, and the resolver allows
    it because two stores are alternatives rather than a conjunction -- but the
    category resolved against only one side, so the other filters a key no row
    carries. "NORTHCO MART VELDRA SELBYCROSS generated RM 7,883.52 ... no records found for
    GLEDEHOLT VANTRYTON" reads as a fact about Gledeholt Vantryton when it is an artefact of which
    OpCo the category came from.
    """

    @staticmethod
    def _plan(category_opco: str) -> ResolutionPlan:
        return ResolutionPlan(
            slots=[
                ResolvedSlot(
                    slot_id="store:a", phrase="veldra selbycross", entity_class="store",
                    target_column="store_id", status="resolved",
                    canonical_value="1013", display_value="NORTHCO MART VELDRA SELBYCROSS",
                    opco_code="NORTHCO_MART",
                ),
                ResolvedSlot(
                    slot_id="store:b", phrase="gledeholt vantryton", entity_class="store",
                    target_column="store_id", status="resolved",
                    canonical_value="1004", display_value="GLEDEHOLT VANTRYTON",
                    opco_code="NORTHCO",
                ),
                ResolvedSlot(
                    slot_id="cat", phrase="grocery", entity_class="category_l1",
                    target_column="category_l1_key", status="resolved",
                    canonical_value="1025", display_value="Grocery",
                    opco_code=category_opco,
                ),
            ]
        )

    def _notes(self, category_opco: str) -> list[str]:
        return LookupNodesMixin._cross_opco_category_notes(
            ResolutionResult(plan=self._plan(category_opco), spans=[])
        )

    def test_the_uncovered_opco_is_named(self) -> None:
        notes = self._notes("NORTHCO_MART")

        self.assertEqual(1, len(notes))
        self.assertIn("CROSS-OPCO CATEGORY LIMIT", notes[0])
        self.assertIn("NORTHCO", notes[0])
        self.assertIn("grocery", notes[0])

    def test_no_note_when_every_store_shares_the_categorys_opco(self) -> None:
        plan = self._plan("NORTHCO_MART")
        plan.slots = [s for s in plan.slots if s.opco_code != "NORTHCO"]

        self.assertEqual(
            [],
            LookupNodesMixin._cross_opco_category_notes(
                ResolutionResult(plan=plan, spans=[])
            ),
        )

    def test_no_note_without_a_category(self) -> None:
        plan = self._plan("NORTHCO_MART")
        plan.slots = [s for s in plan.slots if s.entity_class == "store"]

        self.assertEqual(
            [],
            LookupNodesMixin._cross_opco_category_notes(
                ResolutionResult(plan=plan, spans=[])
            ),
        )


class OptionListLengthTests(unittest.TestCase):
    """The list shown must be the list the matcher kept.

    There were two numbers: the fuzzy pass kept 5 candidates per window and the
    resolver rendered up to 6. Nobody chose that split -- two constants in two files
    that never met -- so "fashion" offered 5 options out of 9 that scored identically
    at 85.0, and nothing said the other 4 existed. A user for whom none of the five
    fitted had no way to know there were more.
    """

    def test_the_ceiling_comes_from_settings(self) -> None:
        original = settings.lookup_max_options
        settings.lookup_max_options = 3
        try:
            self.assertEqual(3, max_options_per_span())
        finally:
            settings.lookup_max_options = original

    def test_the_fuzzy_pass_reads_the_setting_rather_than_a_default(self) -> None:
        """A default argument would freeze the value at import time."""
        default = inspect.signature(match_fuzzy).parameters["limit_per_window"].default

        self.assertIsNone(default)

    def test_lowering_the_setting_shortens_the_list(self) -> None:
        dictionary = EntityDictionary(scope_signature="test")
        dictionary.weak_tokens = {"category_l4": set()}
        for index in range(9):
            _add(
                dictionary,
                f"fashion {index}",
                _category_l4(str(4000 + index), f"SOFT > LADIES > FASHION {index}"),
            )

        original = settings.lookup_max_options
        try:
            settings.lookup_max_options = 3
            short = match_fuzzy([TokenRun(start=0, tokens=("fashion",))], dictionary)
            settings.lookup_max_options = 10
            full = match_fuzzy([TokenRun(start=0, tokens=("fashion",))], dictionary)
        finally:
            settings.lookup_max_options = original

        self.assertEqual(3, len(short[0].candidates))
        self.assertEqual(9, len(full[0].candidates))

    def test_a_span_carries_up_to_the_ceiling(self) -> None:
        """Nine same-scoring names all survive; the old cap kept five."""
        dictionary = EntityDictionary(scope_signature="test")
        dictionary.weak_tokens = {"category_l4": set()}
        for index in range(9):
            candidate = _category_l4(str(4000 + index), f"SOFT > LADIES > FASHION {index}")
            _add(dictionary, f"fashion {index}", candidate)

        spans = match_fuzzy([TokenRun(start=0, tokens=("fashion",))], dictionary)

        self.assertTrue(spans)
        self.assertEqual(9, len(spans[0].candidates))
        self.assertLessEqual(len(spans[0].candidates), max_options_per_span())

    def test_a_two_digit_reply_selects_the_tenth_option(self) -> None:
        """Raising the ceiling past 9 only works if "10" parses as an option."""
        self.assertTrue(is_option_number("10"))


class BranchNameSuffixAliasTests(unittest.TestCase):
    """A branch is named for the place it sits in, and the place ends the name.

    Stripping only the LEADING banner words was not enough. "FRESHMART QUARRYMERE ROWANHOLT
    SOUTH" keeps "quarrymere" -- a mall, not a banner -- so its shortest alias was
    "quarrymere rowanholt south", while "NORTHCO MART ROWANHOLT SOUTH" reduced cleanly to
    "rowanholt south". One store owned that phrase, so "northco freshmarte rowanholt south"
    resolved silently to the NorthCo Mart branch: the wrong shop, no question asked, in
    a city with four branches carrying the name.
    """

    def test_the_place_at_the_end_of_the_name_is_an_alias(self) -> None:
        aliases = store_aliases(normalize("FRESHMART QUARRYMERE ROWANHOLT SOUTH"))

        self.assertIn("rowanholt south", aliases)
        self.assertIn("quarrymere rowanholt south", aliases)

    def test_two_branches_sharing_a_place_both_answer_to_it(self) -> None:
        shared = store_aliases(normalize("FRESHMART QUARRYMERE ROWANHOLT SOUTH")) & store_aliases(
            normalize("NORTHCO MART ROWANHOLT SOUTH")
        )

        self.assertIn("rowanholt south", shared)

    def test_a_single_word_suffix_is_not_an_alias(self) -> None:
        """"south" or "juniperford" as a store name is how a region question breaks."""
        aliases = store_aliases(normalize("FRESHMART QUARRYMERE ROWANHOLT SOUTH"))

        self.assertNotIn("south", aliases)
        self.assertNotIn("rowanholt", aliases)

    def test_the_shared_place_now_asks_instead_of_guessing(self) -> None:
        dictionary = _dictionary(
            _store("1016", "NORTHCO MART ROWANHOLT SOUTH", opco_code="NORTHCO_MART"),
            _store("2201", "FRESHMART QUARRYMERE ROWANHOLT SOUTH", opco_code="NORTHCO"),
        )

        spans, _ = match_exact("customers at rowanholt south in june 2026", dictionary)
        stores = [s for s in spans if s.best and s.best.entity_class == "store"]

        self.assertEqual(1, len(stores))
        self.assertEqual("ambiguous", span_status(stores[0]))
        self.assertEqual(2, len(stores[0].candidates))

    def test_a_unit_prefixed_location_still_resolves_on_its_own(self) -> None:
        """The existing single-token exception for "S05 NOVELBURN" is untouched."""
        self.assertIn("novelburn", store_aliases(normalize("NORTHCO MALL S05 NOVELBURN")))


class RelativeTimeContextTests(unittest.TestCase):
    """"now" is the present, and "last N months" rolls from today.

    "Show me high-value customers who are NOW inactive" came back asking which
    month was meant -- the question had already said. Relative windows also used to
    end at the last COMPLETE month, so a user asking on the 30th for the last three
    months got a range that stopped four weeks earlier.
    """

    TODAY = date(2026, 7, 30)

    def _block(self) -> str:
        return build_relative_time_context(self.TODAY)

    def _line(self, label: str) -> str:
        for line in self._block().splitlines():
            if line.startswith(f"- {label}"):
                return line
        raise AssertionError(f"no line for {label!r}")

    def test_now_is_today_and_the_current_month(self) -> None:
        block = self._block()

        self.assertIn("calendar_date = DATE '2026-07-30'", block)
        self.assertIn("month_start_date = DATE '2026-07-01'", block)

    def test_the_planner_is_told_not_to_ask_when_the_question_says_now(self) -> None:
        self.assertIn("Never ask which period", self._block())

    def test_last_n_months_ends_today(self) -> None:
        self.assertIn("AND DATE '2026-07-30'", self._line("last 3 months"))
        self.assertIn("DATE '2026-04-30'", self._line("last 3 months"))

    def test_last_month_is_a_calendar_month_not_a_rolling_window(self) -> None:
        """The singular names a month; only the plural rolls."""
        line = self._line("last month")

        self.assertIn("DATE '2026-06-01'", line)
        self.assertIn("DATE '2026-06-30'", line)

    def test_the_partial_month_is_disclosed_rather_than_avoided(self) -> None:
        self.assertIn("PARTIAL", self._block())

    def test_month_arithmetic_survives_a_year_boundary(self) -> None:
        block = build_relative_time_context(date(2026, 2, 15))

        self.assertIn("DATE '2025-11-15'", block)   # last 3 months, daily
        self.assertIn("DATE '2025-12-01'", block)   # last 3 months, monthly
