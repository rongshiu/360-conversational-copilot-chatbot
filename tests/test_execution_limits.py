"""MAX_QUERY_ROWS has to be enforced by the database, not by the answer.

The executor used to call .all() and let the driver buffer the whole result, then
trim to a preview much later. A query that grouped badly materialised every row in
the application first, so the setting named a limit it did not impose.
"""
from __future__ import annotations

import unittest

import sqlglot

from app.agents.analysis_agent import AnalysisResult, _with_truncation_disclosure
from app.service.sql_executor import _with_row_cap


class RowCapTests(unittest.TestCase):
    def test_cap_is_applied_by_the_database(self) -> None:
        capped = _with_row_cap("SELECT transaction_count FROM v_sales_daily", 500)

        self.assertIn("LIMIT 501", capped)
        sqlglot.parse_one(capped, read="postgres")

    def test_one_row_past_the_cap_is_requested(self) -> None:
        """Otherwise "exactly n rows" and "n rows and more" are indistinguishable."""
        self.assertIn("LIMIT 11", _with_row_cap("SELECT 1", 10))

    def test_an_inner_limit_still_parses(self) -> None:
        inner = (
            "SELECT store_id, SUM(transaction_count) AS t FROM v_sales_store_monthly "
            "WHERE month_start_date = DATE '2026-07-01' "
            "GROUP BY store_id ORDER BY t DESC LIMIT 20"
        )
        sqlglot.parse_one(_with_row_cap(inner, 500), read="postgres")

    def test_a_cte_still_parses(self) -> None:
        inner = (
            "WITH monthly AS (SELECT store_id, SUM(quantity) AS q "
            "FROM v_sales_store_monthly WHERE month_start_date = DATE '2026-07-01' "
            "GROUP BY store_id) SELECT store_id, q FROM monthly"
        )
        sqlglot.parse_one(_with_row_cap(inner, 500), read="postgres")

    def test_a_trailing_line_comment_does_not_swallow_the_cap(self) -> None:
        capped = _with_row_cap("SELECT 1 -- chosen grain", 500)

        self.assertTrue(capped.rstrip().endswith("LIMIT 501"))
        sqlglot.parse_one(capped, read="postgres")


class TruncationDisclosureTests(unittest.TestCase):
    def test_truncation_is_stated_as_a_limitation(self) -> None:
        result = _with_truncation_disclosure(
            AnalysisResult(natural_answer="Sales rose."),
            "Only the first 500 rows are included; the query returned more.",
        )

        self.assertTrue(
            any("rows are included" in item for item in result.limitations),
            "a trimmed result must not be presented as a complete one",
        )

    def test_a_complete_result_gains_nothing(self) -> None:
        result = _with_truncation_disclosure(AnalysisResult(natural_answer="ok"), "")

        self.assertEqual([], result.limitations)


if __name__ == "__main__":
    unittest.main()
