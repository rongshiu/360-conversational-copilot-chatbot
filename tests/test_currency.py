"""Money must render as RM, and only money may.

Every money column in ci_core is denominated in one currency, so the unit is a
deployment constant. What is *not* constant is which result column is money: SQL
aliases are written by the planner, so `total_sales`, `revenue`, `amt` and `sales_rm`
are all plausible names for one sum, and `count` appears inside `discount_amount`.
Matching names against money-ish words therefore fails in both directions -- symbols
on counts, bare numbers on amounts -- so the unit is derived from the AST instead.
"""

from __future__ import annotations

import unittest

from app.core import settings
from app.db.v3_ddl import CURRENCY_COLUMNS, MONEY_COLUMNS, NON_CURRENCY_MONEY_COLUMNS
from app.service.currency import (
    CURRENCY_RULE,
    amount_formatter,
    currency_columns,
    currency_note,
    format_amount,
    money_output_names,
)


class CurrencyColumnDeclarationTests(unittest.TestCase):

    def test_currency_columns_partition_money_columns(self) -> None:
        """Adding a money column without classifying it must fail here.

        MONEY_COLUMNS answers "what is withheld from an EXEC", which is a superset of
        "what is an amount of money" -- a sales rank is derived from revenue, so it is
        withheld, but formatting it as RM would be nonsense. Two independent lists
        would drift; one list plus an exception set cannot.
        """
        declared = {column for columns in MONEY_COLUMNS.values() for column in columns}
        classified = CURRENCY_COLUMNS | NON_CURRENCY_MONEY_COLUMNS
        self.assertEqual(
            set(),
            declared - classified,
            "these MONEY_COLUMNS entries are neither currency nor declared non-currency",
        )
        self.assertEqual(set(), CURRENCY_COLUMNS & NON_CURRENCY_MONEY_COLUMNS)

    def test_a_rank_is_not_currency(self) -> None:
        self.assertNotIn("category_sales_rank", CURRENCY_COLUMNS)
        self.assertIn("gross_sales_amount", CURRENCY_COLUMNS)


class CurrencyDetectionTests(unittest.TestCase):

    def test_an_aggregate_over_a_money_column_is_money(self) -> None:
        sql = (
            "SELECT SUM(s.transaction_count) AS total_transactions, "
            "SUM(s.gross_sales_amount) AS total_sales_amount "
            "FROM v_sales_summary_daily AS s"
        )
        self.assertEqual(
            {"total_sales_amount"},
            set(currency_columns(sql, ["total_transactions", "total_sales_amount"])),
        )

    def test_the_alias_may_be_named_anything(self) -> None:
        """The planner picks aliases, so the name carries no information."""
        for alias in ("amt", "x", "revenue", "takings", "transaction_count"):
            sql = f"SELECT SUM(f.net_sales_amount) AS {alias} FROM v_sales_daily f"
            self.assertEqual({alias}, set(currency_columns(sql, [alias])), alias)

    def test_a_count_named_like_money_is_not_money(self) -> None:
        sql = "SELECT SUM(f.transaction_count) AS total_sales_amount FROM v_sales_daily f"
        self.assertEqual(set(), set(currency_columns(sql, ["total_sales_amount"])))

    def test_money_per_count_is_money(self) -> None:
        """Average transaction value is an amount, not a ratio."""
        sql = (
            "SELECT SUM(f.gross_sales_amount) / NULLIF(SUM(f.transaction_count), 0) AS atv "
            "FROM v_sales_daily f"
        )
        self.assertEqual({"atv"}, set(currency_columns(sql, ["atv"])))

    def test_money_over_money_is_a_share_not_an_amount(self) -> None:
        """A dimensionless ratio must not get a symbol even though every column in it is money."""
        sql = (
            "WITH t AS (SELECT category_key, SUM(gross_sales_amount) amt FROM v_sales_daily GROUP BY 1) "
            "SELECT category_key, amt / SUM(amt) OVER () AS share FROM t"
        )
        self.assertEqual(set(), set(currency_columns(sql, ["category_key", "share"])))

    def test_money_propagates_through_a_chain_of_ctes(self) -> None:
        """Two hops, so a single pass in tree order would miss the far end."""
        sql = (
            "WITH a AS (SELECT calendar_date, SUM(gross_sales_amount) AS amt FROM v_sales_daily GROUP BY 1), "
            "b AS (SELECT calendar_date, amt AS daily_take FROM a) "
            "SELECT calendar_date, SUM(daily_take) AS grand FROM b GROUP BY 1"
        )
        self.assertEqual({"grand"}, set(currency_columns(sql, ["calendar_date", "grand"])))
        self.assertIn("daily_take", money_output_names(sql))

    def test_unparseable_sql_produces_no_symbols_rather_than_guesses(self) -> None:
        for sql in (None, "", "this is not sql at all ((("):
            self.assertEqual(set(), set(currency_columns(sql, ["total_sales_amount"])))


class CurrencyRenderingTests(unittest.TestCase):

    def test_amounts_use_the_configured_symbol(self) -> None:
        self.assertEqual(f"{settings.currency_symbol} 21,034,575.95", format_amount(21034575.95))
        self.assertEqual("RM", settings.currency_symbol)
        self.assertEqual("MYR", settings.currency_code)

    def test_the_formatter_leaves_counts_bare(self) -> None:
        sql = (
            "SELECT SUM(f.transaction_count) AS txns, SUM(f.gross_sales_amount) AS amt "
            "FROM v_sales_daily f"
        )
        fmt = amount_formatter(sql, ["txns", "amt"])
        self.assertEqual("325994", fmt("txns", 325994))
        self.assertEqual("RM 900.00", fmt("amt", 900))
        self.assertEqual("null", fmt("amt", None))

    def test_the_static_rule_forbids_the_dollar_sign(self) -> None:
        """The LLM defaults to $ because its training data does."""
        self.assertIn("MYR", CURRENCY_RULE)
        self.assertIn("RM", CURRENCY_RULE)
        self.assertIn("$", CURRENCY_RULE)  # only as the thing it must never write
        self.assertIn("Never write $", CURRENCY_RULE)

    def test_the_per_turn_note_names_the_money_columns(self) -> None:
        """Without this the model has to decide which column is money, and it guesses."""
        sql = (
            "SELECT SUM(f.transaction_count) AS txns, SUM(f.gross_sales_amount) AS amt "
            "FROM v_sales_daily f"
        )
        note = currency_note(sql, ["txns", "amt"])
        self.assertIn("amt", note)
        self.assertNotIn("txns", note)
        self.assertIn("None", currency_note(sql, ["txns"]))


class DeterministicAnswerCurrencyTests(unittest.TestCase):
    """The fallback path runs when the LLM is skipped, so a prompt rule cannot reach it."""

    def _analyse(self, sql: str, rows: list[dict]):
        from app.agents.analysis_agent import _basic_deterministic_analysis

        return _basic_deterministic_analysis("q", sql, rows)

    def test_a_single_row_answer_marks_money_and_not_counts(self) -> None:
        sql = (
            "SELECT SUM(s.transaction_count) AS total_transactions, "
            "SUM(s.gross_sales_amount) AS total_sales_amount FROM v_sales_summary_daily AS s"
        )
        answer = self._analyse(
            sql, [{"total_transactions": 325994.0, "total_sales_amount": 21034575.95}]
        ).natural_answer
        self.assertIn("RM 21,034,575.95", answer)
        self.assertIn("total_transactions is 325994.0", answer)
        self.assertNotIn("$", answer)

    def test_a_trend_marks_levels_but_not_the_percentage_change(self) -> None:
        sql = "SELECT calendar_date, SUM(gross_sales_amount) AS revenue FROM v_sales_daily GROUP BY 1"
        finding = self._analyse(
            sql,
            [
                {"calendar_date": "2026-01-01", "revenue": 100.0},
                {"calendar_date": "2026-02-01", "revenue": 150.0},
            ],
        ).finding
        self.assertIn("RM 100.00", finding)
        self.assertIn("RM 150.00", finding)
        self.assertIn("(50.0%)", finding)
        self.assertNotIn("RM 50.0%", finding)

    def test_a_breakdown_marks_the_leader(self) -> None:
        sql = (
            "SELECT c.category_name, SUM(f.gross_sales_amount) AS sales FROM v_sales_daily f "
            "JOIN dim_product_category c ON c.category_key = f.category_key GROUP BY 1"
        )
        answer = self._analyse(
            sql,
            [{"category_name": "HARD", "sales": 900.0}, {"category_name": "SOFT", "sales": 100.0}],
        ).natural_answer
        self.assertIn("RM 900.00", answer)
        self.assertIn("90.0%", answer)


class ChartCurrencyTests(unittest.TestCase):
    """A frontend must not have to parse the symbol out of prose."""

    def _chart(self, sql: str, analysis: dict, columns: list[str]):
        import asyncio

        from app.agents.answer_agent import build_analytics_response

        rows = [{c: 1 for c in columns}]
        response = asyncio.run(
            build_analytics_response(
                "q", sql, {"columns": columns, "rows": rows}, reason="", analysis_result=analysis
            )
        )
        return response["chart"]

    def _bar(self, y_axis: str) -> dict:
        return {
            "chart_type": "bar_chart",
            "chart_title": "t",
            "y_axis": y_axis,
            "label_field": "label",
            "value_field": "value",
            "chart_data": [{"label": "HARD", "value": 900.0}, {"label": "SOFT", "value": 100.0}],
        }

    def test_a_money_bar_chart_declares_the_currency(self) -> None:
        sql = (
            "SELECT c.category_name, SUM(f.gross_sales_amount) AS sales FROM v_sales_daily f "
            "JOIN dim_product_category c ON c.category_key = f.category_key GROUP BY 1"
        )
        chart = self._chart(sql, self._bar("sales"), ["category_name", "sales"])
        self.assertEqual("currency", chart["value_format"])
        self.assertEqual("MYR", chart["currency"])

    def test_a_count_bar_chart_declares_none(self) -> None:
        sql = (
            "SELECT c.category_name, SUM(f.transaction_count) AS txns FROM v_sales_daily f "
            "JOIN dim_product_category c ON c.category_key = f.category_key GROUP BY 1"
        )
        chart = self._chart(sql, self._bar("txns"), ["category_name", "txns"])
        self.assertNotEqual("currency", chart["value_format"])
        self.assertIsNone(chart["currency"])

    def test_a_declared_unit_beats_the_inferred_format(self) -> None:
        """Money that happens to land in 0-100 looks exactly like a percentage.

        _build_bar_or_donut_points infers the format from the values, which carry no
        unit -- so two small amounts summing to ~100 were labelled "percentage".
        """
        sql = "SELECT store_id, SUM(gross_sales_amount) AS sales FROM v_sales_daily GROUP BY 1"
        analysis = self._bar("sales")
        analysis["chart_data"] = [{"label": "A", "value": 60.0}, {"label": "B", "value": 40.0}]
        chart = self._chart(sql, analysis, ["store_id", "sales"])
        self.assertEqual("currency", chart["value_format"])


if __name__ == "__main__":
    unittest.main()
