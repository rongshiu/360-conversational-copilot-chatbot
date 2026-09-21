# app/service/currency.py
"""One place that knows what money is and what it is called.

Two problems, one module:

1. The narrative answer is written by an LLM, which defaults to `$` because most of
   its training data does. Left alone it renders Northland Ringgit as US Dollars --
   the same class of failure as the confident zero, since a wrong unit on a right
   number reads as a right answer.

2. `ChartSpec.value_format` could never be "currency", because the chart builder
   inferred the format from the *values* (are they 0-100? then percentage) and
   values carry no unit. A frontend therefore had no way to know it was formatting
   money.

Neither is fixed by matching column names against a list of money-ish words: SQL
aliases are chosen by the planner, so `total_sales`, `revenue`, `sales_rm` and
`amt` are all plausible names for the same sum, and `count` appears in both
`transaction_count` and `discount_amount`. What is stable is the *real* column the
expression reads. So the unit is derived from the AST: an output column is money if
its expression reaches a declared currency column, propagated through CTEs and
subqueries.
"""

from __future__ import annotations

from typing import Iterable

import sqlglot
from sqlglot import exp

from app.core import settings
from app.db.v3_ddl import CURRENCY_COLUMNS

# A ratio of money to money is a share, not an amount, so it must not be labelled
# currency. Anything else that touches money is: revenue, revenue per customer, and
# average transaction value are all amounts.
_MAX_PROPAGATION_PASSES = 8


def _parse(sql: str | None) -> exp.Expression | None:
    if not sql or not sql.strip():
        return None
    try:
        return sqlglot.parse_one(sql, read="postgres")
    except Exception:
        # Unparseable SQL is not this module's problem -- the validator already
        # rejected it, or it is a shape sqlglot does not know. Formatting simply
        # falls back to unitless numbers rather than guessing.
        return None


def _touches_money(node: exp.Expression, money: set[str]) -> bool:
    return any(
        (column.name or "").lower() in money for column in node.find_all(exp.Column)
    )


def _is_dimensionless(node: exp.Expression, money: set[str]) -> bool:
    """True when the expression divides money by money.

    `gross_sales_amount / transaction_count` is an average basket -- money.
    `gross_sales_amount / SUM(gross_sales_amount) OVER ()` is a share -- not money,
    even though every column in it is a money column.
    """
    for div in node.find_all(exp.Div):
        left, right = div.this, div.expression
        if left is None or right is None:
            continue
        if _touches_money(left, money) and _touches_money(right, money):
            return True
    return False


def money_output_names(sql: str | None) -> frozenset[str]:
    """Names in `sql` that carry a monetary amount.

    Includes intermediate CTE aliases, not just the final projection, because that
    is how the propagation works. Callers that only care about the response payload
    should intersect with the columns actually returned -- `currency_columns` does.
    """
    tree = _parse(sql)
    if tree is None:
        return frozenset()

    money = {name.lower() for name in CURRENCY_COLUMNS}

    # Repeat to a fixpoint rather than sorting selects by depth: an alias defined in
    # one CTE may be consumed by a later CTE, which is consumed by the outer select,
    # and a single pass in tree order would miss the far end of that chain.
    for _ in range(_MAX_PROPAGATION_PASSES):
        added = False
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                name = (projection.alias_or_name or "").lower()
                if not name or name in money:
                    continue
                if _touches_money(projection, money) and not _is_dimensionless(
                    projection, money
                ):
                    money.add(name)
                    added = True
        if not added:
            break

    return frozenset(money)


def currency_columns(sql: str | None, columns: Iterable[str] | None) -> frozenset[str]:
    """The subset of `columns` that should be rendered as currency."""
    names = money_output_names(sql)
    if not names:
        return frozenset()
    return frozenset(
        column for column in (columns or []) if (column or "").lower() in names
    )


def is_currency_column(sql: str | None, column: str | None) -> bool:
    if not column:
        return False
    return bool(currency_columns(sql, [column]))


def format_amount(value: object) -> str:
    """Render a monetary value the same way the prompt rule asks the LLM to.

    Used by the deterministic answer paths, which run when the LLM is skipped or
    fails -- exactly the paths a prompt rule cannot reach.
    """
    try:
        amount = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return f"{settings.currency_symbol} {amount:,.2f}"


def amount_formatter(sql: str | None, columns: Iterable[str] | None):
    """Returns fmt(column, value) -> str, adding the symbol only to money columns."""
    money = currency_columns(sql, columns)

    def fmt(column: str | None, value: object) -> str:
        if value is None:
            return "null"
        if column in money:
            return format_amount(value)
        return str(value)

    return fmt


def currency_note(sql: str | None, columns: Iterable[str] | None) -> str:
    """Per-turn instruction naming the money columns in this result.

    Given to the answer writers alongside the static rule. Naming the columns
    matters: without it the model has to decide for itself which of
    `total_transactions` and `total_sales` is money, and it gets that wrong in both
    directions -- symbols on counts, bare numbers on amounts.
    """
    found = sorted(currency_columns(sql, columns))
    if not found:
        return "None -- no column in this result is a monetary amount."
    return (
        f"{', '.join(found)} -- amounts in {settings.currency_code}, "
        f"write them as {settings.currency_symbol} 1,234.56."
    )


# Static rule, safe to embed in a cached system instruction: the currency is a
# deployment constant, not per-request state.
CURRENCY_RULE = f"""
Currency:
- Every monetary figure in this warehouse is denominated in {settings.currency_code}
  ({settings.currency_symbol}). Never write $, USD, or any other symbol or code, and
  never convert to another currency.
- Format amounts as "{settings.currency_symbol} 1,234.56": symbol, space, thousands
  separators, two decimals. Large amounts may be abbreviated as
  "{settings.currency_symbol} 21.0 million" when that reads better.
- Put the symbol on monetary amounts only. Counts, quantities, percentages and ranks
  are not money and must stay bare. The "Monetary columns" section of the prompt
  names the money columns in the current result; trust it over the column name.
""".strip()
