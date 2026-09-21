# app/agents/planner_agent.py
from __future__ import annotations

import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.agents.sql_validator_agent import describe_validation_rules
from app.service.business_alias_service import format_business_alias_context
from app.service.business_context import NORTHCO_360_BUSINESS_CONTEXT
from app.service.metric_service import get_metric_service
from app.service.table_registry import build_planner_table_context
from app.core import settings
from app.service.llm_service import get_llm
from app.utils.time_context import build_relative_time_context


class SqlPlan(BaseModel):
    status: Literal["answerable", "needs_clarification", "unsupported"] = "answerable"
    sql: Optional[str] = Field(default=None)
    reason: str
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    unsupported_reason: Optional[str] = None
    missing_slots: list[str] = Field(default_factory=list)

    # WHY it is unsupported. Only meaningful when status is "unsupported".
    #
    # The two causes need different handling and read identically in prose. "You
    # only have access to NorthCo. revenue data" is an AUTHORIZATION refusal -- the
    # data exists and someone else may see it -- while "no table pairs a customer
    # with a brand" is a schema limit that no permission grant would lift. A client
    # showing "request access" on the first and "rephrase" on the second cannot tell
    # them apart from the sentence, and neither can an auditor.
    #
    # Classified by the model for the same reason period_source is: which of the two
    # a refusal was turns on reading the question, and a keyword list over the reason
    # text is exactly the fragile heuristic that field's comment warns about. The
    # model classifies; the graph decides what to do about it.
    #
    # Defaults to "not_answerable" so a model that omits the field never invents an
    # authorization event that did not occur.
    #
    # "customer_identity" is listed separately from "not_answerable" even though it
    # is also a schema limit no grant would lift. The reason is consistency across
    # layers: query_shape_guard catches most identity questions by regex and reports
    # customer_identity, but that regex is deliberately not exhaustive -- "give me
    # example of 5 customers" matches none of its patterns -- and whatever it misses
    # lands here. Without this value the identical refusal was reported with a policy
    # or without one depending purely on which guard happened to fire first.
    # "out_of_scope" was a third value, meaning the question needed an OpCo or a
    # product category the caller was not granted. There are no grants, so it can
    # never be true, and leaving it selectable meant the model could refuse an
    # answerable question by picking a reason that no longer exists.
    unsupported_cause: Literal["customer_identity", "not_answerable"] = "not_answerable"

    # Where the period in the SQL came from. The model reports the fact; the graph
    # decides what to do about it.
    #
    # This exists because "all time" was answered with June 2026 data and narrated
    # as fact. The SQL was well-formed and carried a period filter, so every
    # deterministic check passed -- what no check could see was that the period was
    # invented. Only whoever read the question knows that, and that is the model.
    #
    # The first attempt at this was a pair of regexes: a list of unbounded phrases
    # plus a date-ish pattern to test whether a period was already named. It failed
    # exactly where the user predicted, on wording it had not anticipated --
    # "may i know how many customers all time" slipped through because `may` looked
    # like the month. Phrasing variance is what language models are for; policy is
    # what code is for. So the model classifies, and `make_plan` refuses to execute
    # anything it did not classify as `stated`.
    period_source: Literal["stated", "assumed", "none"] = "stated"
    # Plain-language label for the period actually used ("June 2026"), so a
    # clarification can offer it instead of asking an open question.
    period_label: Optional[str] = None


def _sanitize_sql(sql: str) -> str:
    sql = (sql or "").strip()
    sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"```$", "", sql).strip()
    return sql.rstrip(";")


def _normalize_plan(plan: SqlPlan) -> SqlPlan:
    """Make the model's plan internally consistent before the graph acts on it.

    Split out of plan_sql so it can be tested without an LLM call: these rules
    decide whether SQL runs and whether an authorization refusal is reported, and
    both were previously reachable only through a live model.
    """
    if plan.sql:
        plan.sql = _sanitize_sql(plan.sql)

    if plan.status == "answerable" and not plan.sql:
        plan.status = "unsupported"
        plan.unsupported_reason = (
            plan.unsupported_reason
            or "The request could not be grounded in the available customer intelligence tables."
        )
        plan.needs_clarification = False
        plan.clarification_question = None

    if plan.status == "needs_clarification":
        plan.sql = None
        plan.unsupported_reason = None
        plan.needs_clarification = True

    if plan.status == "unsupported":
        plan.sql = None
        plan.needs_clarification = False
        plan.clarification_question = None
    else:
        # The cause describes a refusal, so it is meaningless on any other status.
        # Cleared rather than left at whatever the model returned, because it drives
        # whether an authorization policy is reported -- and a model is free to fill
        # the field in on a plan that answered perfectly well.
        plan.unsupported_cause = "not_answerable"

    return plan


def _history_text(chat_history: list[dict] | None, limit: int = 8) -> str:
    lines: list[str] = []

    for item in (chat_history or [])[-limit:]:
        role = item.get("role", "unknown")
        content = item.get("content", "")
        lines.append(f"{role}: {content}")

    return "\n".join(lines) if lines else "None"


def _compact_previous_rows(previous_rows: list[dict] | None, limit: int = 20) -> str:
    if not previous_rows:
        return "None"

    trimmed = [row for row in previous_rows[:limit] if isinstance(row, dict)]
    if not trimmed:
        return "None"

    try:
        return json.dumps(trimmed, ensure_ascii=False, indent=2)
    except Exception:
        return str(trimmed)


# ---------------------------------------------------------------------------
# Static planner rules.
#
# This block is identical for every request and every user, so it is built once
# at import time and sent to the model as a cached system_instruction (see
# llm_service.GeminiService). Only the small per-turn context (query, history,
# lookup/schema context, retry reason) is sent as the dynamic prompt below.
#
# All interpolated values here (schema name, allowed tables, business aliases,
# row limits) are static runtime configuration, not per-request data.
# ---------------------------------------------------------------------------
_s = settings
_DETAIL_PREVIEW_ROWS = getattr(_s, "detail_preview_rows", _s.max_query_rows)
_BUSINESS_ALIAS_CONTEXT = format_business_alias_context()

PLANNER_SYSTEM_RULES = f"""
You generate ONE PostgreSQL SELECT query for the NorthCo Customer Intelligence Copilot.

{NORTHCO_360_BUSINESS_CONTEXT}

{describe_validation_rules()}

Output contract:
{{
  "status": "answerable" | "needs_clarification" | "unsupported",
  "sql": "the query, or null",
  "reason": "one short sentence",
  "needs_clarification": true | false,
  "clarification_question": "null, or one specific question",
  "unsupported_reason": "null, or why it cannot be answered",
  "unsupported_cause": "customer_identity" | "not_answerable",
  "missing_slots": []
}}

GRAIN -- the thing to get right first
- Sales, transactions, penetration and basket questions: DAILY. Filter calendar_date.
  Any date range works, including a partial month.
- UNLESS the question splits by a CUSTOMER attribute -- generation, gender, tier,
  segment, lifecycle, tenure, product holding. The daily sales tables have no
  customer column at all, so those splits are impossible there. Use
  v_customer_category_monthly, which carries customer_key, store_id, the full
  category chain, transaction_count, quantity and gross_sales_amount, joined to
  v_customer_opco_monthly on customer_key + month_start_date + opco_code for the
  attribute. "Member penetration and basket size for Gen Z versus Millennial in
  Home Fashion at Inglegate Juniperford" is answerable exactly that way -- do NOT report it as
  unsupported.
- Within a day, use `daypart` ('Morning' | 'Afternoon' | 'Evening'). Group by
  daypart AND daypart_seq, and ORDER BY daypart_seq -- ordering by the text sorts
  Afternoon before Morning, which reads as nonsense.
- Distinct customer counts and customer attributes: MONTHLY. Filter
  month_start_date, which is always the 1st of a month.
- If a question wants a distinct customer count over a PARTIAL month, you cannot
  answer it exactly. Use transaction-based membership penetration on
  v_sales_summary_daily instead, and say in `reason` that the figure is
  transaction-based rather than a customer count.

MEMBERSHIP PENETRATION -- always define it
- Default meaning is share of TRANSACTIONS made by members:
    SUM(member_transaction_count) / NULLIF(SUM(transaction_count), 0)
- The views already expose member_transaction_count, member_quantity and (for HOD)
  member_sales_amount, so you never need FILTER (WHERE customer_type = 'Member').
- Sales-based penetration uses member_sales_amount / gross_sales_amount, and is
  HOD-only because it is money.
- Multiply by 100.0 and round if you want a percentage. Never store or average a
  ratio -- compute numerator and denominator, then divide.

PERIOD COMPARISON / YEAR-OVER-YEAR
- Pass BOTH ranges explicitly as two predicates or two FILTER clauses.
- Never derive the prior year with date arithmetic, and never compare on
  day-of-year: it drifts by a day across a leap year.
- Example shape for "this period vs same period last year":
    SUM(transaction_count) FILTER (WHERE calendar_date BETWEEN DATE 'a' AND DATE 'b')

CUSTOMER COUNTS
- Always COUNT(DISTINCT customer_key). Never count rows -- the customer tables have
  one row per customer per month per OpCo, so COUNT(*) over-counts.
- customer_key must never reach the OUTERMOST SELECT list or GROUP BY, and never
  sit inside an aggregate other than COUNT(DISTINCT). Inside a CTE or subquery it
  is free: group by it, join on it, filter on it.

PER-CUSTOMER THEN AGGREGATE
- To measure something ABOUT customers -- repeat purchase rate, visits per
  customer, the share who did X -- group by customer_key in a CTE and aggregate it
  away outside. This is allowed, and it is the only correct shape for these.
    WITH per_customer AS (
      SELECT b.category_l2_key AS category_l2_key, b.customer_key AS customer_key,
             SUM(b.transaction_count) AS txns
      FROM v_customer_category_monthly AS b
      WHERE b.month_start_date BETWEEN DATE '2026-04-01' AND DATE '2026-06-01'
      GROUP BY b.category_l2_key, b.customer_key
    )
    SELECT category_l2_key,
           ROUND(100.0 * COUNT(*) FILTER (WHERE txns > 1) / COUNT(*), 1) AS repeat_rate_pct
    FROM per_customer GROUP BY category_l2_key ORDER BY repeat_rate_pct DESC
- Do not answer "not supported" for a per-customer rate. If the underlying counts
  are on a customer table, the rate is computable this way.
- Never SUM or AVG customer_count. It is distinct customers at one row grain only.
- "How many customers bought <category>": v_customer_category_monthly. It is LEAF
  grained with every ancestor denormalised, so filter category_l1_key ..
  category_l4_key at whatever depth the question names. A customer appears once per
  leaf and store, so COUNT(DISTINCT customer_key) is the only correct count.

CUSTOMER GRAIN -- read this before any customer count
- v_customer_opco_monthly is one row per customer PER OPCO per month. A customer
  active in two OpCos occupies TWO rows. v_customer_category_monthly is one row
  per leaf category per store.
- So COUNT(*) is a ROW count, not a customer count, and it over-counts. Always
  COUNT(DISTINCT customer_key). The validator rejects any COUNT without DISTINCT
  on those two views, including COUNT(1) and COUNT(some_column).
- This applies to GROUP BY too: "customers per segment" is
  SELECT family_segment, COUNT(DISTINCT customer_key) ... GROUP BY family_segment.
- Revenue and volume measures (total_revenue, total_gmv, transaction_count) ARE
  per-OpCo and sum correctly across rows. `age` and `active_opco_count` are not:
  they repeat per row, so aggregate them only over one row per customer.

CROSS-OPCO OVERLAP -- one table, no join
- active_opco_codes on v_customer_opco_monthly is the set of OpCos that customer
  was active in THAT MONTH. There is no separate overlap table.
- "How many of our <OpCo A> customers also shop at <OpCo B>":
    SELECT COUNT(DISTINCT customer_key)
    FROM v_customer_opco_monthly
    WHERE month_start_date = DATE '...'
      AND opco_code = '<OPCO_A>'
      AND active_opco_codes @> ARRAY['<OPCO_B>']
- is_multi_opco_customer and active_opco_count are on the same row for
  "how many shop with more than one of us".
- The array is PER MONTH. Two memberships in one predicate means the SAME month.
  For overlap ANYWHERE in a longer period, group first:
    SELECT count(*) FROM (
      SELECT customer_key FROM v_customer_opco_monthly
      WHERE month_start_date BETWEEN ... GROUP BY customer_key
      HAVING bool_or(active_opco_codes @> ARRAY['<A>'])
         AND bool_or(active_opco_codes @> ARRAY['<B>'])) x
  Asking for both in one month when the user said "in Q1" answers a narrower
  question than the one asked.

WHAT YOU MUST NOT ATTEMPT
- Never try to identify, list, name or describe an individual customer. There is no
  such column. If the question asks who, return status="unsupported" with an
  unsupported_reason that offers the countable version instead.
- Never reference a column that is not in the schema context you were given. If a
  measure you want is absent, it is because this caller may not see it -- use a
  volume measure (transaction_count, quantity, customer counts) and say so.
- Never invent a filter value for a store or category. If the resolved entity
  filters block gives you a canonical key, use that key. If a name is ambiguous and
  unresolved, return needs_clarification.
- For store/branch filters, use resolved `store_id` plus `opco_code`. Never filter
  `store_name` in WHERE from raw user text.
- Never ask the user to provide internal database IDs such as `store_id` or
  `category_key`. Ask them to choose from named options or provide the business
  name instead.
- In `reason`, `unsupported_reason`, and `clarification_question`, use business
  display names only. Never mention store IDs or category keys.

CLARIFY VS UNSUPPORTED
- needs_clarification: the question is answerable but a slot is missing or
  ambiguous -- most often the time period. Ask one specific question.
- unsupported: no combination of the available tables and columns can answer it.
  Say plainly why, and name what you CAN answer.
- When status is "unsupported", also set `unsupported_cause`:
  - "customer_identity" -- the question asks you to identify, list, name, describe
    or give examples of individual customers or members. Any phrasing of it: "who
    bought X", "list our members", "give me 5 example customers", "show me a sample
    of shoppers". Counting them is fine and is not this case.
  - "not_answerable" -- the schema itself cannot answer it, for ANY caller. No table
    pairs a customer with a brand.
  Judge by which of the two is true, not by how the question was worded. When both
  apply, or you are unsure, use "not_answerable" -- it claims less.
- There is no OpCo or category scope. Every caller sees every OpCo and every product
  category, so a question naming one is never a refusal -- answer it, or ask which
  one is meant if the name is ambiguous.
- Revenue you cannot see is NOT an unsupported question. The
  metric block above lists the volume equivalent for exactly this case: answer with
  it and say the figure is volume-based. Refusing "what were sales last month"
  outright, when transaction counts answer the question behind it, throws away an
  answer the caller can use -- and the substitution is already reported to them
  separately, so nothing is hidden by giving it.
- Prefer answering. Only ask when a wrong guess would produce a misleading number.
- NEVER substitute a time period the user did not ask for. If the question names no
  period, or asks for an unbounded one ("all time", "ever", "since inception"),
  return needs_clarification and ask which period. Picking the latest month and
  answering is the worst outcome available: the number is real, the question it
  answers is not the one asked, and nothing in the answer says so.
- A qualitative term the schema does not define is NOT a filter you may compose
  yourself. "high-value", "top customers", "big spenders", "loyal", "our best
  customers", "premium shoppers" are business definitions, and which values they
  cover is the user's to state, not yours to pick. Choosing Elite and Premium for
  "high-value" produces a real count of a group nobody defined -- the same failure
  as inventing a period, and just as invisible in the answer.
  Ask which values are meant, and LIST the documented ones so a single reply
  settles it. If the term is only describing the customers rather than filtering
  them, drop it and break the answer down by that column instead, so every value is
  visible and the reader chooses.
  A term that IS a documented value -- "At Risk", "Elite", "Churned" -- is not
  qualitative. Use it directly.
- "now", "currently", "right now", "as of today", "at the moment" DO name a period:
  the present. So does a plain present-tense question about a customer's state --
  "who is inactive", "how many are Elite". Resolve them from the current-date block
  and answer. Asking "which month did you mean" there re-asks a question the user
  already answered, and it happens most often when something ELSE in the question
  also needs clarifying: ask about that alone, never bundle the period in with it.

PERIOD SOURCE -- always set `period_source`, and set it honestly
- "stated": the user gave the period, in any wording, including relative ones you
  resolved yourself -- "June 2026", "3 to 22 June", "last month", "this year vs
  last year", "ytd", "since March 2026", "now", "currently", "as of today". A
  present-tense question about a customer's state is "stated" too: it names the
  present. Also "stated" when the period came from an earlier turn.
- "assumed": you chose a period the user did not give. This includes defaulting to
  the latest complete month, and it includes an unbounded request such as "all
  time", "ever" or "since inception" that you narrowed to something computable.
  Set `period_label` to what you would have used ("June 2026").
- "none": the query needs no period at all, because it touches only dimensions
  (`SELECT count(*) FROM dim_store`). Never "none" for a fact or customer view.

An "assumed" period is not executed -- the user is asked instead -- so do not use
it as a shortcut to avoid asking. Report it accurately and let the graph decide.

STYLE
- Alias every table. Reference columns as alias.column.
- Name every output column with a readable alias -- these become chart labels.
- Round percentages to 1 or 2 decimals.
- Add ORDER BY whenever the result is a ranking or a time series.
- No LIMIT unless the user asked for a top-N.
""".strip()


async def plan_sql(
    query: str,
    *,
    chat_history: list[dict] | None = None,
    glossary_context: str = "",
    resolved_term: str | None = None,
    previous_sql: str | None = None,
    previous_rows: list[dict] | None = None,
    retry_reason: str | None = None,
    schema_match_confidence: str | None = None,
    lookup_context: str | None = None,
    is_followup: bool = False,
    principal=None,
) -> SqlPlan:
    # Previous result rows are only useful for "profile them"-style follow-ups
    # that re-use the prior customer set. Injecting them on every plan wastes
    # tokens, so they are sent only when this turn is a contextual follow-up.
    previous_rows_text = (
        _compact_previous_rows(previous_rows) if is_followup else "None"
    )

    # The allowed-table block is per-request, not static: views the caller's
    # category grant cannot satisfy are omitted entirely, and money columns are
    # flagged as unavailable when the caller is not HOD. A table the planner cannot
    # see is a table it cannot pick by mistake.
    scope_line = principal.describe_scope() if principal is not None else "unrestricted"

    prompt = f"""
Your access scope for this request:
{scope_line}

{build_planner_table_context(principal)}

{get_metric_service().format_context(principal)}

Current date context (use this to resolve relative time periods):
{build_relative_time_context()}

Resolved shorthand term from prior context:
{resolved_term or 'None'}

Schema match confidence:
{schema_match_confidence or 'unknown'}

Previous failed or rejected SQL:
{previous_sql or 'None'}

Previous result rows from the last assistant answer:
{previous_rows_text}

Why it failed or was rejected:
{retry_reason or 'None'}

Resolved lookup context:
{lookup_context or 'None'}

Relevant schema context:
{glossary_context or 'None'}

Recent chat history:
{_history_text(chat_history)}

User query:
{query}
""".strip()

    plan = await get_llm().generate_json(
        prompt,
        SqlPlan,
        system_instruction=PLANNER_SYSTEM_RULES,
    )

    return _normalize_plan(plan)
