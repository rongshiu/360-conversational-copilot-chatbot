"""Query-shape guard: does the SQL actually answer the question asked?

Repurposed for v3. The v2 guard existed to catch one specific bad shape:

    SELECT DISTINCT customer_id ... LIMIT 500

being narrated as "500 distinct customers". It then asked the planner to add a
`total_customer_count` window and a stable rank so the preview was honest.

That shape cannot occur now. `customer_id` does not exist, and the validator
rejects any projection of `customer_key`, so there is no customer preview list to
mis-describe.

What remains here is one check, and it is deliberately the only one: a
distinct-customer count over a sub-month period. The customer tables are monthly,
so "how many customers between 3 and 22 June" cannot be answered exactly; the
guard steers to transaction-based penetration rather than letting the planner
silently widen the period to the whole month. That is a fact about the SQL --
which columns it counts and which it filters -- so a pattern over the SQL reads it
exactly.

TWO CHECKS USED TO LIVE HERE AND BOTH WERE REMOVED, for the same reason.

Customer-identity questions ("who bought X", "list the customers") were caught by
a regex over the QUESTION. It could only ever match the phrasings someone thought
of: "give me example of 5 customers" matched none of its patterns, reached the
planner, and was correctly refused there -- but reported differently, because the
guard had not been the one to catch it. Extending the pattern list buys the next
phrasing and not the one after. The planner now classifies it as
`SqlPlan.unsupported_cause = "customer_identity"`, which handles arbitrary wording,
and `route_after_plan` sends it to the same refusal terminal the guard used.

An unbounded period ("all time", "ever") went the same way earlier: a phrase list
plus a date-ish pattern, which failed on "may i know how many customers all time"
because `may` looked like the month. It is now `SqlPlan.period_source`.

The rule those two cases teach: recognising what a question MEANS, in arbitrary
phrasing, is a language problem and belongs to the model. Deciding what to do
about it is policy and belongs to code. Nothing is lost by removing them, because
neither was ever the guarantee -- the schema has no customer name, id or contact
column at any grain, and sql_validator_agent rejects any projection of
`customer_key`. Those are the guarantee, and they are structural.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryShapeValidation:
    is_valid: bool
    feedback: str = ""


_CUSTOMER_KEY_COUNT = re.compile(
    r"count\s*\(\s*distinct\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?customer_key\s*\)",
    re.IGNORECASE,
)
_MONTH_START_FILTER = re.compile(r"month_start_date\s*(?:=|between|in\s*\()", re.IGNORECASE)
_CALENDAR_DATE_FILTER = re.compile(r"calendar_date\s*(?:=|<|>|between|in\s*\()", re.IGNORECASE)


def validate_query_shape(query: str, sql: str | None) -> QueryShapeValidation:
    text = sql or ""
    if text.strip() and _CUSTOMER_KEY_COUNT.search(text):
        # A distinct-customer count must sit on a monthly table. If the SQL filters
        # a calendar_date range instead, the planner has mixed grains: it is
        # counting customers from a monthly table using a daily predicate, or has
        # silently widened a sub-month period to the whole month.
        if _CALENDAR_DATE_FILTER.search(text) and not _MONTH_START_FILTER.search(text):
            return QueryShapeValidation(
                is_valid=False,
                feedback=(
                    "This counts distinct customers but filters on calendar_date. The "
                    "customer tables are monthly and have no calendar_date column. Either "
                    "filter month_start_date to whole months, or -- if the question needs "
                    "a sub-month period -- answer it with transaction-based membership "
                    "penetration on v_sales_summary_daily instead and say that the figure "
                    "is transaction-based rather than customer-based."
                ),
            )

    return QueryShapeValidation(is_valid=True)
