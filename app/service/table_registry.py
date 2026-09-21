# app/service/table_registry.py
"""Which table answers which question.

The planner sees persona VIEW names, never base tables: `v_sales_summary_daily`,
not `ci_core.agg_sales_daily`. The view is resolved by search_path to the
caller's role level, so one generated query works for both HOD and executive.

One routing rule shapes the prompt:

  Money availability. An executive cannot see revenue columns at all -- they are
  absent from the ci_exec views, so referencing one is a hard SQL error. Ranking
  and "best performing" questions must fall back to volume measures.

A second rule used to sit alongside it and is gone. A category grant deeper than
a table's category columns could not be satisfied by that table -- RLS correctly
returned nothing, because a coarser rollup row aggregates siblings outside the
grant -- so the view was withheld from the planner entirely rather than listed
with a warning. With no grants, every caller of a given role sees the same six
views and the planner prompt is identical for all of them, which also makes it
cacheable in a way it was not before.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.db.v3_ddl import PERSONA_VIEWS


@dataclass(frozen=True)
class TableProfile:
    view_name: str
    base_table: str
    table_type: str
    grain: str
    category_depth: int
    has_money: bool
    has_identity: bool
    best_for: str
    avoid_for: str = ""


# The only names the planner may emit in a FROM clause.
SALES_VIEWS: tuple[str, ...] = (
    "v_sales_summary_daily",
    "v_sales_daily",
    "v_sales_store_monthly",
    "v_sales_sku_monthly",
)
CUSTOMER_VIEWS: tuple[str, ...] = (
    "v_customer_opco_monthly",
    "v_customer_traits_monthly",
    "v_customer_category_monthly",
)
ALLOWED_VIEWS: tuple[str, ...] = SALES_VIEWS + CUSTOMER_VIEWS

# Every v3 serving view is period-grained -- there is no customer snapshot table
# to fall back on the way v2 had gold_customer_360_copilot_dim_v2. A question
# with no time period is therefore ambiguous on all of them, and the planner must
# ask rather than silently scan all history.
PERIOD_REQUIRED_VIEWS: tuple[str, ...] = ALLOWED_VIEWS

# The date/period column each view filters on.
PERIOD_COLUMN: dict[str, str] = {
    "v_sales_summary_daily": "calendar_date",
    "v_sales_daily": "calendar_date",
    "v_sales_store_monthly": "month_start_date",
    "v_sales_sku_monthly": "month_start_date",
    "v_customer_opco_monthly": "month_start_date",
    "v_customer_traits_monthly": "month_start_date",
    "v_customer_category_monthly": "month_start_date",
}

# Dimensions the planner may join.
#
# dim_date was here and is gone: holiday and fiscal-period questions are out of
# scope, so neither the table nor the holiday_period_sales metric exists. Year, month, weekday and day-of-year were always
# EXTRACT / date_trunc on the date column already present on every fact, so
# nothing else moved.
# dim_product is here because v_sales_sku_monthly carries sku_key and nothing
# else does: without the join a SKU ranking can only be a list of opaque numbers.
# It was left out while the allowlist was being trimmed, which made the SKU-name
# instruction in TABLE_PROFILES unfollowable -- the planner wrote the join the
# profile asked for and the validator rejected the table.
JOINABLE_DIMENSIONS: tuple[str, ...] = (
    "dim_opco",
    "dim_store",
    "dim_product_category",
    "dim_product",
)

# Tables that exist but must never appear in generated SQL.
#
# dim_product_category_closure was here too and is dropped. copilot_access_log
# remains: the copilot role has no grant on it, so naming it would fail anyway,
# but rejecting it here produces a usable retry message instead of a permission
# error -- and a generated query reaching the audit log is a bug worth naming.
FORBIDDEN_TABLES: tuple[str, ...] = ("copilot_access_log",)

# Columns that may only ever appear inside COUNT(DISTINCT ...).
IDENTITY_COLUMNS: tuple[str, ...] = ("customer_key",)

# Non-additive measures. SUM or AVG over these is wrong, not merely imprecise.
#
# customer_count is distinct-at-this-row-grain, so summing it double counts anyone
# who appears at two grains. category_sales_rank is a rank.
NEVER_AGGREGATE_COLUMNS: tuple[str, ...] = (
    "customer_count",
    "category_sales_rank",
)

# Repeated on every row a customer occupies rather than measured per row. Kept
# apart from the rest of NEVER_AGGREGATE_COLUMNS so the retry message can name the
# right fix -- de-duplicate to one row per customer, rather than "this is a rank".
#
# EMPTY, AND EMPTY BY CONSTRUCTION. age, active_opco_count, member_tenure_months
# and lifetime_transaction_count were here. They are now on
# v_customer_traits_monthly, which carries one row per customer per month, so
# AVG(age) and SUM(lifetime_transaction_count) are simply correct there and
# refusing them would be a false positive. Nothing on v_customer_opco_monthly is a
# per-customer constant any more -- that was the point of the split.
#
# The mechanism stays because the failure it catches is silent: if a constant is
# ever denormalised back onto a repeating grain, add it here and the retry message
# tells the planner to de-duplicate first.
PER_CUSTOMER_CONSTANT_COLUMNS: frozenset[str] = frozenset()


TABLE_PROFILES: dict[str, TableProfile] = {
    "v_sales_summary_daily": TableProfile(
        view_name="v_sales_summary_daily",
        base_table="agg_sales_daily",
        table_type="sales_rollup",
        grain="calendar_date x daypart x opco x category_l1 x category_l2 x customer_type",
        category_depth=2,
        has_money=True,
        has_identity=False,
        best_for=(
            "DEFAULT for sales performance, membership penetration, daily trend, "
            "period comparison and best-daypart questions. Smallest table that "
            "answers them, so prefer it whenever store detail and category depth "
            "below level 2 are not required."
        ),
        avoid_for=(
            "Store-level questions, category depth below level 2, and payment-type "
            "breakdowns."
        ),
    ),
    "v_sales_daily": TableProfile(
        view_name="v_sales_daily",
        base_table="fact_sales_daily",
        table_type="sales_atomic",
        grain=(
            "calendar_date x daypart x opco x store x category_key x customer_type "
            "x payment_type"
        ),
        category_depth=4,
        has_money=True,
        has_identity=False,
        best_for=(
            "Daily questions that need store detail, full category depth, or a "
            "payment-type breakdown."
        ),
        avoid_for=(
            "Chain-level totals and trends that v_sales_summary_daily can answer "
            "far more cheaply."
        ),
    ),
    "v_sales_store_monthly": TableProfile(
        view_name="v_sales_store_monthly",
        # Aggregated from fact_sales_daily, not a table of its own. The rollup it
        # used to mirror was only 2.5x, and its stored ranks existed partly to
        # avoid a window function computed inside an RLS-filtered set. Both reasons
        # went with row-level security.
        base_table="fact_sales_daily",
        table_type="sales_rollup",
        grain="month_start_date x opco x store x category_key x customer_type",
        category_depth=4,
        has_money=True,
        has_identity=False,
        best_for=(
            "Monthly store and category league tables, top/bottom stores, top "
            "categories, category rank and top-100 flags. category_sales_rank is "
            "computed across ALL stores in the OpCo and category, so filtering to "
            "one store still returns its true rank."
        ),
        avoid_for=(
            "Daily or daypart questions -- no date or daypart column. And DISTINCT "
            "CUSTOMER COUNTS: this view has no customer_count column, because it "
            "aggregates a fact with no customer key and summing daily counts would "
            "double-count anyone who shopped twice in the month. Distinct customers "
            "by store and month come from v_customer_category_monthly with "
            "COUNT(DISTINCT customer_key)."
        ),
    ),
    "v_sales_sku_monthly": TableProfile(
        view_name="v_sales_sku_monthly",
        base_table="agg_sales_sku_monthly",
        table_type="sales_rollup",
        grain="month_start_date x opco x store_id x sku_key x customer_type",
        category_depth=4,
        has_money=True,
        has_identity=False,
        best_for=(
            "Monthly SKU and BRAND sales, by store. brand_name is on this table, so "
            "a brand ranking is a GROUP BY rather than a join. Join dim_product for "
            "the SKU name, dim_store for the branch or region."
        ),
        avoid_for=(
            "Daypart or daily grain, and anything about WHO bought -- this table "
            "deliberately has no customer key. Nothing pairs a customer with a SKU "
            "or a brand, so brand affinity, brand repeat-purchase rate and brand by "
            "demographic are not answerable anywhere."
        ),
    ),
    "v_customer_opco_monthly": TableProfile(
        view_name="v_customer_opco_monthly",
        base_table="fact_customer_opco_monthly",
        table_type="customer",
        grain="customer_key x month_start_date x opco_code",
        category_depth=0,
        has_money=True,
        has_identity=True,
        best_for=(
            "WHAT THE CUSTOMER DID AT ONE OPCO: revenue, transactions, quantity, "
            "stores, categories, dayparts, payment volumes, per-OpCo recency and "
            "activity. Always COUNT(DISTINCT customer_key) -- a customer active in "
            "two OpCos occupies two rows here.\n"
            "WHO THE CUSTOMER IS is NOT here. Age, tier, tenure, lifecycle, product "
            "holdings and all six trait dimensions are on v_customer_traits_monthly, "
            "one row per customer per month. Join on customer_key AND "
            "month_start_date to combine a trait with a measure; the join is one row "
            "each side per customer-month, so it cannot fan out.\n"
            "Cross-OpCo overlap is also on that view: active_opco_codes @> "
            "ARRAY['NORTHCO_MART'] there, with no DISTINCT needed."
        ),
        avoid_for=(
            "Customer traits and segments -- they are on v_customer_traits_monthly. "
            "Also sub-month periods: this table is monthly, so a 3-22 June question "
            "cannot be answered here. Use transaction-based penetration on "
            "v_sales_summary_daily instead."
        ),
    ),
    "v_customer_traits_monthly": TableProfile(
        view_name="v_customer_traits_monthly",
        base_table="fact_customer_traits_monthly",
        table_type="customer",
        grain="customer_key x month_start_date",
        category_depth=0,
        has_money=False,
        has_identity=True,
        best_for=(
            "WHO THE CUSTOMER IS, one row per customer per month. The six trait "
            "dimensions live here and nowhere else: visit_frequency_bucket, "
            "basket_size_bucket, northvalu_adoption_segment, family_segment, "
            "financial_product_holding_bucket and preferred_payment_segment. So do "
            "age, generation, tier, tenure, residency, product holdings and the "
            "cross-OpCo columns.\n"
            "ONE ROW PER CUSTOMER, so COUNT(*) is a customer count here -- unlike "
            "v_customer_opco_monthly, where it counts rows. AVG(age) and "
            "SUM(lifetime_transaction_count) are correct here for the same reason.\n"
            "THE TRAITS ARE CROSS-OPCO: a Heavy shopper made eight trips with NorthCo, "
            "not eight at one OpCo, so these buckets will NOT tie out to a single "
            "OpCo's transaction_count. That is the definition the audience tool "
            "uses.\n"
            "Cross-OpCo overlap is a containment test: active_opco_codes @> "
            "ARRAY['NORTHCO_MART'] -- and here it needs no DISTINCT, because the "
            "customer appears once."
        ),
        avoid_for=(
            "Anything measured: revenue, transactions, quantity, stores, categories "
            "and dayparts are all per-OpCo and live on v_customer_opco_monthly. Join "
            "on customer_key AND month_start_date to combine a trait with a measure "
            "-- one row each side per customer-month, so the join cannot fan out. "
            "Also sub-month periods: this table is monthly."
        ),
    ),
    "v_customer_category_monthly": TableProfile(
        view_name="v_customer_category_monthly",
        base_table="bridge_customer_category_monthly",
        table_type="customer_bridge",
        grain=(
            "customer_key x month_start_date x opco_code x leaf category_key "
            "x store_id, with category_l1_key..category_l4_key denormalised"
        ),
        category_depth=4,
        has_money=True,
        has_identity=True,
        best_for=(
            "THE table for a customer attribute crossed with what they bought. "
            "Distinct customers in a category, their spend, and both broken down by "
            "store -- 'average basket size for Gen Z in Home Fashion at Inglegate Juniperford'. "
            "Full category depth: filter category_l1_key..category_l4_key at "
            "whatever level the question names. Join v_customer_opco_monthly on "
            "customer_key + month_start_date + opco_code for demographics, tier, "
            "lifecycle and product holdings. A customer appears once per leaf "
            "category and store, so COUNT(DISTINCT customer_key) is the only "
            "correct customer count."
        ),
        avoid_for=(
            "SKU-level and brand-level questions -- category is as fine as "
            "customer-level product data goes, by design. Brand SALES live on "
            "v_sales_sku_monthly, which has no customer."
        ),
    ),
}


# One customer, many rows. See the note beside IDENTITY_COLUMNS.
# Views where ONE CUSTOMER OCCUPIES MORE THAN ONE ROW, so COUNT without DISTINCT
# is a row count wearing a customer count's clothes.
#
# Derived from has_identity until v_customer_traits_monthly arrived, which broke
# the equivalence: it carries customer_key and is one row per customer per month,
# so COUNT(*) on it is exactly right and refusing it would send the planner
# looking for a bug that is not there. Identity and multiplicity are different
# properties; this set is the second one, named explicitly.
CUSTOMER_GRAIN_VIEWS: frozenset[str] = frozenset({
    "v_customer_opco_monthly",       # one row per OpCo
    "v_customer_category_monthly",   # one row per leaf category per store
})


def normalize_table_name(table_name: str | None) -> str:
    return (table_name or "").strip().replace('"', "").replace("`", "").split(".")[-1]


def get_table_profile(table_name: str | None) -> TableProfile | None:
    return TABLE_PROFILES.get(normalize_table_name(table_name))


def is_forbidden_table(table_name: str | None) -> bool:
    """True for objects that exist but must never appear in generated SQL.

    Includes the base tables themselves: the copilot role has no grant on
    ci_core, so naming one would fail anyway, but rejecting it in the validator
    produces a far better retry message than a permission error.
    """
    name = normalize_table_name(table_name)
    if name in FORBIDDEN_TABLES:
        return True
    # Base tables are reachable only through their persona view.
    return name in set(PERSONA_VIEWS.values())


def format_table_profile(table_name: str | None) -> str:
    profile = get_table_profile(table_name)
    if not profile:
        return ""
    return "\n".join(
        [
            f"- Table type: {profile.table_type}",
            f"- Row grain: {profile.grain}",
            f"- Category depth: level {profile.category_depth}"
            if profile.category_depth
            else "- Category depth: not category-scoped",
            f"- Best for: {profile.best_for}",
            f"- Avoid for: {profile.avoid_for or 'none'}",
        ]
    )


def build_planner_table_context(principal=None) -> str:
    """The allowed-table block for the planner prompt.

    Every caller of a given role gets the same block. It used to be narrowed a
    second time by the caller's category grant -- views whose category columns
    could not satisfy the grant were omitted entirely rather than listed with a
    warning, because a table the planner cannot see is a table it cannot pick by
    mistake. With no grants there are two possible outputs, HOD and EXEC, and this
    is cheap enough to cache.
    """
    can_see_money = getattr(principal, "can_see_money", True)

    lines: list[str] = ["Allowed tables (use these exact names, unqualified):"]
    for view in ALLOWED_VIEWS:
        profile = TABLE_PROFILES[view]
        lines.append(f"\n{view}")
        lines.append(f"  grain: {profile.grain}")
        lines.append(f"  best for: {profile.best_for}")
        if profile.avoid_for:
            lines.append(f"  avoid for: {profile.avoid_for}")
        if profile.has_identity:
            lines.append(
                "  NOTE: customer_key is an identity column. It is legal ONLY inside "
                "COUNT(DISTINCT customer_key)."
            )
        if profile.has_money and not can_see_money:
            lines.append(
                "  NOTE: revenue columns are not available to you. Use "
                "transaction_count, quantity or customer counts instead."
            )

    lines.append(f"\nJoinable dimensions: {', '.join(JOINABLE_DIMENSIONS)}")
    lines.append(
        "dim_store is keyed by (opco_code, store_id) -- store_id repeats across "
        "OpCos, so ALWAYS join on BOTH: "
        "JOIN dim_store s ON s.opco_code = f.opco_code AND s.store_id = f.store_id. "
        "Joining on store_id alone attributes one OpCo's sales to another's store."
    )
    lines.append(
        "dim_product_category joins on category_key, or on the matching l1..l4 key "
        "when you need a level above the leaf."
    )
    lines.append(
        "There is no date dimension. Derive year, month, weekday, day-of-year and "
        "prior-year dates with EXTRACT or date_trunc on the date column already "
        "present on every fact. Public-holiday and fiscal-calendar questions are "
        "not answerable -- say so rather than approximating with calendar dates."
    )
    return "\n".join(lines)
