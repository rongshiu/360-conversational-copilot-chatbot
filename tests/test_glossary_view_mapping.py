"""The glossary documents base tables; the planner may only name persona views.

Everything here guards the translation between those two facts. It is the kind of
mapping that fails silently: a view with no glossary columns does not raise, it
just makes the validator's column-ownership check skip -- so the planner writes
whatever it likes against that view and nothing objects until the database does.
"""
from __future__ import annotations

import unittest

from app.db.v3_ddl import AGGREGATE_VIEWS, MEMBER_SPLITS, PERSONA_VIEWS
from app.service.glossary_service import get_glossary_service
from app.service.table_registry import JOINABLE_DIMENSIONS


class GlossaryViewMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.glossary = get_glossary_service()

    def test_every_persona_view_has_columns(self) -> None:
        """Two views share fact_sales_daily as a base.

        The mapping was built by inverting PERSONA_VIEWS into a one-to-one dict, so
        v_sales_store_monthly overwrote v_sales_daily and the atomic sales view
        ended up with nothing.
        """
        for view in PERSONA_VIEWS:
            with self.subTest(view=view):
                self.assertTrue(
                    self.glossary.get_columns_for_table(view),
                    f"{view} has no glossary columns",
                )

    def test_every_joinable_dimension_has_columns(self) -> None:
        for dimension in JOINABLE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                self.assertTrue(self.glossary.get_columns_for_table(dimension))

    def test_atomic_sales_view_keeps_its_own_grain(self) -> None:
        columns = self.glossary.get_columns_for_table("v_sales_daily")

        # The only view with these three. If they land on the monthly view instead,
        # daypart and payment questions become unanswerable.
        self.assertIn("calendar_date", columns)
        self.assertIn("daypart", columns)
        self.assertIn("payment_type", columns)

    def test_aggregating_view_reports_only_what_it_projects(self) -> None:
        """v_sales_store_monthly GROUPs fact_sales_daily; it is not a mirror of it."""
        columns = self.glossary.get_columns_for_table("v_sales_store_monthly")

        self.assertIn("month_start_date", columns)
        self.assertIn("category_sales_rank", columns)

        for absent in ("calendar_date", "daypart", "payment_type", "line_item_count"):
            with self.subTest(column=absent):
                self.assertNotIn(absent, columns)

        # Deliberately omitted: SUM(customer_count) over days double-counts anyone
        # who shopped twice in the month, and a wrong distinct count is the exact
        # failure the persona views exist to prevent.
        self.assertNotIn("customer_count", columns)

    def test_generated_member_columns_are_documented(self) -> None:
        """planner_agent tells the model to use these, so the validator must know them."""
        for view, base in PERSONA_VIEWS.items():
            if view in AGGREGATE_VIEWS:
                continue
            for _source, alias, _needs_money in MEMBER_SPLITS.get(base, ()):
                with self.subTest(view=view, column=alias):
                    self.assertIn(alias, self.glossary.get_columns_for_table(view))

    def test_aggregate_view_columns_match_the_view_definition(self) -> None:
        for view, spec in AGGREGATE_VIEWS.items():
            projected = {
                alias
                for _, alias in (
                    *spec.grain,
                    *spec.measures,
                    *spec.money,
                    *spec.windows,
                )
            }
            with self.subTest(view=view):
                self.assertEqual(projected, self.glossary.get_columns_for_table(view))

    def test_money_columns_keep_their_role_level(self) -> None:
        """A derived column inherits governance from its source, or states its own.

        member_sales_amount is money however it was generated; if it came through as
        EXEC-visible the planner would offer an executive a column their view does
        not have.
        """
        rows = {
            row["field_name"]: row
            for row in self.glossary.rows_by_table["v_sales_store_monthly"]
        }

        self.assertEqual(rows["member_sales_amount"]["min_role_level"], "HOD")
        self.assertEqual(rows["category_sales_rank"]["min_role_level"], "HOD")
        self.assertEqual(rows["member_transaction_count"]["min_role_level"], "EXEC")


if __name__ == "__main__":
    unittest.main()
