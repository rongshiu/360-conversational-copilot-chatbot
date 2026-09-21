# app/db/v3_ddl.py
"""Shared helpers for the v3 serving schema.

Imported by the alembic revisions and by runtime code that needs to know the
schema/role/GUC names. Keeping them in one place stops the migration set and
the request path from drifting apart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

import sqlalchemy as sa

from app.core import settings

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

CORE = settings.ci_core_schema
HOD = settings.ci_hod_schema
EXEC = settings.ci_exec_schema
META = settings.ci_meta_schema

CI_SCHEMAS: tuple[str, ...] = (CORE, HOD, EXEC, META)
PERSONA_SCHEMAS: dict[str, str] = {"HOD": HOD, "EXEC": EXEC}

# The read role that every copilot query runs as, chosen by the caller's role
# level. Two roles, not one, because the persona split has to be enforced by
# PostgreSQL: with a single role holding SELECT on both ci_hod and ci_exec, the
# only thing stopping an executive session from reading ci_hod.v_sales_daily was
# the validator's refusal to accept a schema-qualified name.
PERSONA_ROLES: dict[str, str] = {
    "HOD": settings.ci_copilot_hod_role,
    "EXEC": settings.ci_copilot_exec_role,
}

# The persona role that reads each persona schema. Inverted from PERSONA_SCHEMAS
# and PERSONA_ROLES so the pairing is stated once.
SCHEMA_READER: dict[str, str] = {
    PERSONA_SCHEMAS[level]: role for level, role in PERSONA_ROLES.items()
}

# The single read role these two replaced. Referenced only by revisions 01, 06 and
# 07 -- which granted to it -- and by revision 09, which revokes every one of those
# grants. Not used by the request path.
COPILOT_ROLE = settings.ci_copilot_role
LOADER_ROLE = settings.ci_loader_role
ANALYST_ROLE = settings.ci_analyst_role

# Session GUCs the request path sets. Every name must contain a dot or Postgres
# rejects it as a custom setting.
#
# There were six of these while access control was row-level. app.opco_codes,
# app.category_keys and app.is_group_user existed solely to be read by RLS
# predicates, and there is no row-level security; app.min_cell_size drove
# small-cell suppression, which is not implemented. None of the four exist.
#
# Neither of the two left is a scope. principal_id is for the trace, and
# role_level is asserted against the session role so a persona mismatch is an
# error rather than a quiet answer from the wrong schema.
GUC_PRINCIPAL_ID = "app.principal_id"
GUC_ROLE_LEVEL = "app.role_level"

ALL_GUCS: tuple[str, ...] = (
    GUC_PRINCIPAL_ID,
    GUC_ROLE_LEVEL,
)

# ---------------------------------------------------------------------------
# Table inventory. Single source of truth for the grants and the view revisions.
# ---------------------------------------------------------------------------

# Facts and bridges: (table, partition_key or None).
#
# This used to carry a third element, scope_kind, naming the RLS policy shape each
# table got. With no row-level security there is one kind of access -- SELECT for
# the copilot role -- so the distinction between a "scoped" and an "unscoped"
# object is gone, and dimensions and facts differ only in whether they partition.
CORE_FACT_TABLES: tuple[tuple[str, str | None], ...] = (
    ("fact_sales_daily", "calendar_date"),
    ("agg_sales_daily", "calendar_date"),
    ("agg_sales_sku_monthly", None),
    ("fact_customer_opco_monthly", "month_start_date"),
    ("fact_customer_traits_monthly", "month_start_date"),
    ("bridge_customer_category_monthly", "month_start_date"),
)

PARTITIONED_TABLES: tuple[tuple[str, str], ...] = tuple(
    (name, key) for name, key in CORE_FACT_TABLES if key
)

CORE_DIMENSIONS: tuple[str, ...] = (
    "dim_opco",
    "dim_store",
    "dim_product_category",
    "dim_product",
)

# Deepest category level each object's columns can express.
#
# This was TABLE_CATEGORY_DEPTH, and most of what it did is gone: it decided
# whether a category GRANT could be satisfied by a given table, since a grant at
# level 3 cannot be honoured by a rollup whose deepest column is level 2. There
# are no grants now.
#
# It survives because one consumer never had anything to do with access. The
# customer bridge stops at level 4, and a question naming a category DEEPER than
# an object stores has to be answered about the ancestor instead -- which is a
# different question, and the caller has to be told. See
# lookup_nodes._category_depth_notes.
CATEGORY_DEPTH: dict[str, int] = {
    "fact_sales_daily": 4,
    "agg_sales_daily": 2,
    "agg_sales_sku_monthly": 4,
    "bridge_customer_category_monthly": 4,
    "dim_product_category": 4,
    "dim_product": 4,
}

# Every table the copilot role may read, by schema.
CORE_TABLES: tuple[str, ...] = tuple(name for name, _ in CORE_FACT_TABLES) + CORE_DIMENSIONS
META_TABLES: tuple[str, ...] = (
    "copilot_glossary",
    "copilot_lookup_value",
    "metric_definition",
)

# Persona views: (view_name, base_table, money_columns_to_drop_for_exec)
# member_sales_amount is listed alongside the base money columns even though no base
# table has it. It is generated BY the views -- MEMBER_SPLITS for the projection
# views, the aggregate spec for v_sales_store_monthly -- and it is revenue either
# way: absent from every executive view, and denominated in ringgit. Leaving it out
# had one visible consequence, since the views enforce the role split themselves:
# CURRENCY_COLUMNS is derived from this map, so member revenue was the one money
# measure a chart rendered as a bare number.
MONEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "fact_sales_daily": (
        "gross_sales_amount",
        "net_sales_amount",
        "discount_amount",
        "gmv_amount",
        "member_sales_amount",
    ),
    "agg_sales_daily": (
        "gross_sales_amount",
        "net_sales_amount",
        "gmv_amount",
        "member_sales_amount",
    ),
    # Keyed by VIEW name, not base table: v_sales_store_monthly aggregates
    # fact_sales_daily rather than mirroring a table of its own, and its rank
    # columns exist only in the view.
    "v_sales_store_monthly": (
        "gross_sales_amount",
        "gmv_amount",
        "member_sales_amount",
        "category_sales_rank",
        "is_top_100",
    ),
    "agg_sales_sku_monthly": ("gross_sales_amount", "member_sales_amount"),
    "fact_customer_opco_monthly": ("total_revenue", "total_gmv"),
    # The bridge gained revenue when customer x category questions started asking
    # for spend rather than only for counts. It is money, so an executive must not
    # see it -- and because the persona views omit rather than mask, a query that
    # names it as an executive fails with "column does not exist" instead of
    # returning a plausible wrong number.
    "bridge_customer_category_monthly": ("gross_sales_amount",),
}

# MONEY_COLUMNS answers "what does an EXEC not get to see", which is a superset of
# "what is denominated in currency": a sales rank is derived from revenue, so it is
# withheld, but it is a position and formatting it as RM would be nonsense.
#
# Declared as the exception list rather than as a second column list so the two can
# never drift apart -- test_currency_columns_partition_money_columns fails if a new
# MONEY_COLUMNS entry is neither currency nor listed here.
NON_CURRENCY_MONEY_COLUMNS: frozenset[str] = frozenset(
    {
        "category_sales_rank",
        "is_top_100",
    }
)

# Columns whose values are amounts of money. Used to decide whether a result column
# should be rendered with a currency symbol; see app/service/currency.py.
CURRENCY_COLUMNS: frozenset[str] = frozenset(
    column
    for columns in MONEY_COLUMNS.values()
    for column in columns
    if column not in NON_CURRENCY_MONEY_COLUMNS
)

PERSONA_VIEWS: dict[str, str] = {
    "v_sales_daily": "fact_sales_daily",
    "v_sales_summary_daily": "agg_sales_daily",
    # Not a mirror. agg_sales_store_monthly was a 2.5x rollup of fact_sales_daily
    # -- not the 125x that justifies agg_sales_daily -- and its stored ranks were
    # partly protecting against a window function computed inside an RLS-filtered
    # set, which ranks within the caller's own slice. With no row filtering a
    # window function is globally consistent for free, so the table was dropped
    # and this name now aggregates the atomic fact. See AGGREGATE_VIEWS.
    "v_sales_store_monthly": "fact_sales_daily",
    "v_sales_sku_monthly": "agg_sales_sku_monthly",
    "v_customer_opco_monthly": "fact_customer_opco_monthly",
    # One row per customer per month, no OpCo. COUNT(*) on this view IS a customer
    # count, which is why the grain rule that guards v_customer_opco_monthly does
    # not apply to it.
    "v_customer_traits_monthly": "fact_customer_traits_monthly",
    # v_customer_group_monthly retired with fact_customer_group_monthly. The
    # overlap questions are answered from the OpCo grain by active_opco_codes.
    "v_customer_category_monthly": "bridge_customer_category_monthly",
}


@dataclass(frozen=True)
class AggregateViewSpec:
    """A persona view that rolls its base table up instead of projecting it.

    Exactly one view is built this way, and it is worth being explicit about why
    the rest are not. Defining a view as a GROUP BY costs a scan of the base table
    on every call. That is affordable here because the roll-up is ~2.5x and the
    base is partitioned on the same period the view groups by, so a single-month
    question reads one partition either way. It would NOT be affordable for
    v_sales_summary_daily, whose base table is 125x larger than the rollup it
    would have to reproduce -- which is why agg_sales_daily is still a table.

    grain     -- (expression, alias) pairs forming the GROUP BY, in output order.
    measures  -- (expression, alias) aggregate pairs available to every role.
    money     -- aggregate pairs an executive does not get.
    windows   -- (expression, alias) evaluated OVER the aggregate. Money-derived,
                 so they follow `money` into the HOD view only.
    """

    base_table: str
    grain: tuple[tuple[str, str], ...]
    measures: tuple[tuple[str, str], ...]
    money: tuple[tuple[str, str], ...]
    windows: tuple[tuple[str, str], ...] = ()


_STORE_MONTHLY_GRAIN: tuple[tuple[str, str], ...] = (
    ("date_trunc('month', b.calendar_date)::date", "month_start_date"),
    ("b.opco_code", "opco_code"),
    ("b.store_id", "store_id"),
    ("b.category_key", "category_key"),
    ("b.category_l1_key", "category_l1_key"),
    ("b.category_l2_key", "category_l2_key"),
    ("b.category_l3_key", "category_l3_key"),
    ("b.category_l4_key", "category_l4_key"),
    ("b.customer_type", "customer_type"),
)

AGGREGATE_VIEWS: dict[str, AggregateViewSpec] = {
    "v_sales_store_monthly": AggregateViewSpec(
        base_table="fact_sales_daily",
        grain=_STORE_MONTHLY_GRAIN,
        measures=(
            ("SUM(b.transaction_count)", "transaction_count"),
            ("SUM(b.quantity)", "quantity"),
            # customer_count is deliberately NOT here, and this is the one place
            # the aggregating view is not a faithful replacement for the table it
            # replaced.
            #
            # agg_sales_store_monthly carried a customer_count computed by the
            # pipeline from the customer keys behind the month. fact_sales_daily
            # has no customer key -- that absence is what makes customer-level
            # answers structurally impossible on the sales side -- so the only
            # thing this view could offer is SUM(b.customer_count) over days,
            # which double-counts anyone who shopped more than once in the month.
            #
            # A wrong distinct count is the exact failure the persona views exist
            # to prevent: it is a plausible number, it aggregates without error,
            # and nothing in the answer marks it as wrong. Omitting the column
            # makes a query naming it fail with "column does not exist", which the
            # validator turns into a retry. Distinct customers by store and month
            # come from v_customer_category_monthly, which carries store_id and a
            # customer key and is counted with COUNT(DISTINCT customer_key).
            (
                "SUM(b.transaction_count) FILTER (WHERE b.customer_type = 'Member')",
                "member_transaction_count",
            ),
        ),
        money=(
            ("SUM(b.gross_sales_amount)", "gross_sales_amount"),
            ("SUM(b.gmv_amount)", "gmv_amount"),
            (
                "SUM(b.gross_sales_amount) FILTER (WHERE b.customer_type = 'Member')",
                "member_sales_amount",
            ),
        ),
        windows=(
            # Quals on the PARTITION BY columns push down through the window, so
            # "top stores in June" still reads a single partition. A qual on
            # store_id correctly does not push down: the rank has to be computed
            # across all stores before one is singled out.
            (
                "RANK() OVER (PARTITION BY month_start_date, opco_code, category_key "
                "ORDER BY gross_sales_amount DESC)",
                "category_sales_rank",
            ),
            (
                "RANK() OVER (PARTITION BY month_start_date, opco_code, category_key "
                "ORDER BY gross_sales_amount DESC) <= 100",
                "is_top_100",
            ),
        ),
    ),
}

# Generated convenience columns projected by the persona views, per base table, as
# (expression, alias, requires_money). Keeps the planner off
# FILTER (WHERE customer_type = 'Member') for penetration.
MEMBER_SPLITS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "fact_sales_daily": (
        ("transaction_count", "member_transaction_count", False),
        ("quantity", "member_quantity", False),
        ("gross_sales_amount", "member_sales_amount", True),
    ),
    "agg_sales_daily": (
        ("transaction_count", "member_transaction_count", False),
        ("quantity", "member_quantity", False),
        ("gross_sales_amount", "member_sales_amount", True),
    ),
    "agg_sales_sku_monthly": (
        ("transaction_count", "member_transaction_count", False),
        ("gross_sales_amount", "member_sales_amount", True),
    ),
}

# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def safe_ident(value: str, *, fallback: str) -> str:
    """Reject anything that is not a bare SQL identifier.

    Schema and role names reach DDL through f-strings, so they can never be
    attacker-controlled. Everything from settings goes through here.
    """
    return value if _IDENT_RE.match(value or "") else fallback


def qualified(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


# ---------------------------------------------------------------------------
# Capability probes
# ---------------------------------------------------------------------------


def extension_available(bind: Any, name: str) -> bool:
    """True when the extension is installed or installable on this server."""
    return bool(
        bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = :n)"
            ),
            {"n": name},
        ).scalar()
    )


def extension_installed(bind: Any, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = :n)"),
            {"n": name},
        ).scalar()
    )


def table_columns(bind: Any, schema: str, table: str) -> list[str]:
    rows = bind.execute(
        sa.text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            ORDER BY ordinal_position
            """
        ),
        {"schema": schema, "table": table},
    ).fetchall()
    return [r[0] for r in rows]


def persona_view_sql(
    bind: Any,
    schema: str,
    view: str,
    base_table: str,
    *,
    include_money: bool,
    copilot_role: str,
    analyst_role: str,
) -> list[str]:
    """The statements that build one persona view.

    Lives here rather than inside the migration that first created these views
    because the views enumerate columns instead of selecting `*`: every later
    revision that adds a column to a base table has to rebuild them, and a second
    copy of this logic is a second place for the money masking, the member splits
    or the grants to drift.

    NO security_invoker, and this is the load-bearing decision.

    These views were created WITH (security_invoker = true), which makes PostgreSQL
    check the BASE table's privileges against the calling role. That was required
    while the base tables carried row-level policies -- an owner-executed view
    would have handed every row to whoever queried it. It also meant the read role
    had to hold SELECT on ci_core.fact_sales_daily itself, and once it holds that,
    an executive session can read gross_sales_amount by naming the base table. The
    only thing preventing it was the validator's table allowlist.

    Without the flag the view executes as its owner, so the read roles need no
    privilege on the fact tables at all -- and the money split becomes a fact about
    who can SELECT what, which PostgreSQL enforces and a prompt-injected query
    cannot argue with. The flag was kept in case row scoping was reinstated; it was
    withdrawn instead, so the hedge cost more than it protected.

    `copilot_role` is the ONE persona role that may read this schema, not the read
    role generally. Passing the wrong one silently rebuilds the boundary as a
    single shared grant, which is what this replaces.
    """
    spec = AGGREGATE_VIEWS.get(view)
    if spec is not None:
        body, dropped = _aggregate_view_body(spec, view, include_money=include_money)
    else:
        body, dropped = _projection_view_body(bind, base_table, include_money=include_money)

    note = (
        f"Executive view. Money columns physically absent: {', '.join(dropped)}."
        if dropped
        else ("HOD view. All measures." if include_money else "No money columns on the base table.")
    )
    if spec is not None:
        note += f" Aggregated from {spec.base_table}; no base table of its own."
    note += (
        f" Runs with the owner's privileges; {copilot_role} reads it and holds no "
        "grant on the base table."
    )

    return [
        f"CREATE VIEW {qualified(schema, view)} AS {body}",
        # Exactly one persona role per schema. This is the boundary.
        f"GRANT SELECT ON {qualified(schema, view)} TO {copilot_role}",
        # The analyst is a human read-only account and sees both personas.
        f"GRANT SELECT ON {qualified(schema, view)} TO {analyst_role}",
        f"COMMENT ON VIEW {qualified(schema, view)} IS "
        f"'{note.replace(chr(39), chr(39) * 2)}'",
    ]


def _projection_view_body(
    bind: Any, base_table: str, *, include_money: bool
) -> tuple[str, list[str]]:
    """SELECT over one base table at its own grain. Five of the six views."""
    all_columns = table_columns(bind, CORE, base_table)
    if not all_columns:
        raise RuntimeError(f"{CORE}.{base_table} has no columns; migration order is wrong")

    money = set(MONEY_COLUMNS.get(base_table, ()))
    selected = [c for c in all_columns if include_money or c not in money]
    projection = [f"b.{quote_ident(c)}" for c in selected]

    # What the view emits, not what the table has: the generated member splits are
    # money too, so a view that omits one has to be able to say so -- and a view
    # that emits one must not be described as having dropped it.
    produced = set(selected)

    for source, alias, needs_money in MEMBER_SPLITS.get(base_table, ()):
        if (needs_money and not include_money) or source not in all_columns:
            continue
        projection.append(
            f"CASE WHEN b.customer_type = 'Member' THEN b.{quote_ident(source)} "
            f"ELSE 0 END AS {quote_ident(alias)}"
        )
        produced.add(alias)

    body = f"SELECT {', '.join(projection)} FROM {qualified(CORE, base_table)} AS b"
    return body, sorted(money - produced)


def _aggregate_view_body(
    spec: AggregateViewSpec, view: str, *, include_money: bool
) -> tuple[str, list[str]]:
    """GROUP BY over a base table, with window columns applied to the result.

    The windows sit in an outer SELECT rather than beside the aggregates: a window
    function is evaluated after grouping, so it cannot appear in the same select
    list that defines the groups. Wrapping also lets the window ORDER BY name the
    measure alias instead of repeating the SUM.
    """
    money = set(MONEY_COLUMNS.get(view, ()))

    inner_terms = [f"{expr} AS {quote_ident(alias)}" for expr, alias in spec.grain]
    inner_terms += [f"{expr} AS {quote_ident(alias)}" for expr, alias in spec.measures]
    if include_money:
        inner_terms += [f"{expr} AS {quote_ident(alias)}" for expr, alias in spec.money]

    # GROUP BY ordinals, so each grain expression is written once. An output alias
    # is not usable in GROUP BY for a non-trivial expression, and repeating
    # date_trunc(...) is exactly the duplication that drifts.
    group_by = ", ".join(str(i + 1) for i in range(len(spec.grain)))

    inner = (
        f"SELECT {', '.join(inner_terms)} "
        f"FROM {qualified(CORE, spec.base_table)} AS b "
        f"GROUP BY {group_by}"
    )

    outer_terms = [quote_ident(alias) for _, alias in spec.grain]
    outer_terms += [quote_ident(alias) for _, alias in spec.measures]
    if include_money:
        outer_terms += [quote_ident(alias) for _, alias in spec.money]
        outer_terms += [f"{expr} AS {quote_ident(alias)}" for expr, alias in spec.windows]

    body = f"SELECT {', '.join(outer_terms)} FROM ({inner}) AS m"

    produced = {alias for _, alias in spec.grain} | {alias for _, alias in spec.measures}
    if include_money:
        produced |= {alias for _, alias in spec.money} | {alias for _, alias in spec.windows}
    return body, sorted(money - produced)


def ltree_type(bind: Any) -> str:
    return "ltree" if extension_installed(bind, "ltree") else "text"


# ---------------------------------------------------------------------------
# Partition window
# ---------------------------------------------------------------------------


def month_floor(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def partition_months(anchor: date) -> list[date]:
    """Month starts to pre-create, oldest first.

    The anchor is passed in rather than read from the clock so a migration
    replay produces the same partitions.
    """
    start = add_months(month_floor(anchor), -abs(settings.ci_partition_months_back))
    end = add_months(month_floor(anchor), abs(settings.ci_partition_months_forward))
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor = add_months(cursor, 1)
    return out


def partition_name(table: str, month_start: date) -> str:
    return f"{table}_p{month_start.strftime('%Y%m')}"


def create_monthly_partitions(
    execute: Any,
    schema: str,
    table: str,
    anchor: date,
    months: Iterable[date] | None = None,
) -> int:
    """Attach one partition per month plus a DEFAULT catch-all.

    The DEFAULT partition means a row outside the pre-created window is stored
    rather than rejected. Adding a partition later requires Postgres to scan the
    default for conflicting rows, so keep the window ahead of the data.
    """
    created = 0
    for month_start in months or partition_months(anchor):
        month_end = add_months(month_start, 1)
        child = partition_name(table, month_start)
        execute(
            f"CREATE TABLE IF NOT EXISTS {qualified(schema, child)} "
            f"PARTITION OF {qualified(schema, table)} "
            f"FOR VALUES FROM ('{month_start.isoformat()}') TO ('{month_end.isoformat()}')"
        )
        created += 1

    execute(
        f"CREATE TABLE IF NOT EXISTS {qualified(schema, table + '_pdefault')} "
        f"PARTITION OF {qualified(schema, table)} DEFAULT"
    )
    return created
