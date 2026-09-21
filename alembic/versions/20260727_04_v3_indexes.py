"""v3 fact indexes

Index strategy follows from how every copilot query is shaped:

  WHERE <scope predicate injected by RLS>   -- opco_code, category_key
    AND <date range from the question>

So the leading columns are the scope keys, then the date. That makes the RLS
predicate the driving access path instead of a filter applied after a scan. BRIN
on the date column is nearly free and helps the wide range scans, since the
loader writes in date order.

On partitioned parents these are declared once and Postgres creates the matching
index on every partition, existing and future.

Revision ID: 20260727_04
Revises: 20260727_03
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.v3_ddl import CORE, qualified

revision: str = "20260727_04"
down_revision: Union[str, Sequence[str], None] = "20260727_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index_name, table, definition)
INDEXES: tuple[tuple[str, str, str], ...] = (
    # ---------------------------------------------------------- fact_sales_daily
    # Scope-first: this is the index RLS drives.
    ("idx_fsd_scope_date", "fact_sales_daily",
     "(opco_code, category_key, calendar_date)"),
    # Grant made at a level above leaf.
    ("idx_fsd_l1_date", "fact_sales_daily", "(opco_code, category_l1_key, calendar_date)"),
    ("idx_fsd_l2_date", "fact_sales_daily", "(opco_code, category_l2_key, calendar_date)"),
    ("idx_fsd_l3_date", "fact_sales_daily", "(opco_code, category_l3_key, calendar_date)"),
    ("idx_fsd_l4_date", "fact_sales_daily", "(opco_code, category_l4_key, calendar_date)"),
    # Store drilldown.
    ("idx_fsd_store_date", "fact_sales_daily", "(opco_code, store_id, calendar_date)"),
    # Daypart questions over a date range.
    ("idx_fsd_daypart", "fact_sales_daily", "(calendar_date, daypart_seq, opco_code)"),
    # Payment mix.
    ("idx_fsd_payment", "fact_sales_daily", "(opco_code, payment_type, calendar_date)"),
    ("brin_fsd_date", "fact_sales_daily", "USING brin (calendar_date)"),

    # ----------------------------------------------------------- agg_sales_daily
    ("idx_asd_scope_date", "agg_sales_daily",
     "(opco_code, category_l1_key, category_l2_key, calendar_date)"),
    # The workhorse for Q1/Q3: date range then membership split.
    ("idx_asd_date_type", "agg_sales_daily", "(calendar_date, opco_code, customer_type)"),
    # Q2: best daypart across a range.
    ("idx_asd_daypart", "agg_sales_daily", "(calendar_date, daypart_seq, opco_code)"),
    ("brin_asd_date", "agg_sales_daily", "USING brin (calendar_date)"),

    # ----------------------------------------------------- agg_sales_sku_monthly
    ("idx_ask_scope_month", "agg_sales_sku_monthly",
     "(opco_code, category_key, month_start_date)"),
    ("idx_ask_sku_month", "agg_sales_sku_monthly", "(opco_code, sku_key, month_start_date)"),

    # ----------------------------------------------- fact_customer_opco_monthly
    # COUNT(DISTINCT customer_key) filtered by month + opco is the whole workload.
    ("idx_fcom_scope_month", "fact_customer_opco_monthly", "(opco_code, month_start_date)"),
    ("idx_fcom_month_active", "fact_customer_opco_monthly",
     "(month_start_date, opco_code) WHERE is_active_in_opco"),
    ("idx_fcom_month_member", "fact_customer_opco_monthly",
     "(month_start_date, opco_code) WHERE is_member"),
    # The trait dimensions are indexed on fact_customer_traits_monthly, below --
    # they are not columns of this table any more.
    ("idx_fcom_store", "fact_customer_opco_monthly",
     "(opco_code, month_start_date, primary_store_id)"),
    ("brin_fcom_month", "fact_customer_opco_monthly", "USING brin (month_start_date)"),


    # ---------------------------------------------- fact_customer_traits_monthly
    # The trait dimensions are what customer questions group by. No opco_code in the
    # prefix -- there is none on this table -- so month_start_date leads, which is
    # also the partition key every query filters on.
    ("idx_fctm_month", "fact_customer_traits_monthly", "(month_start_date)"),
    ("idx_fctm_visit_frequency", "fact_customer_traits_monthly",
     "(month_start_date, visit_frequency_bucket)"),
    ("idx_fctm_basket_size", "fact_customer_traits_monthly",
     "(month_start_date, basket_size_bucket)"),
    ("idx_fctm_northvalu_adoption", "fact_customer_traits_monthly",
     "(month_start_date, northvalu_adoption_segment)"),
    ("idx_fctm_family_segment", "fact_customer_traits_monthly",
     "(month_start_date, family_segment)"),
    ("idx_fctm_product_holding", "fact_customer_traits_monthly",
     "(month_start_date, financial_product_holding_bucket)"),
    ("idx_fctm_preferred_payment", "fact_customer_traits_monthly",
     "(month_start_date, preferred_payment_segment)"),
    ("idx_fctm_lifecycle", "fact_customer_traits_monthly",
     "(month_start_date, lifecycle_stage)"),
    ("idx_fctm_tier", "fact_customer_traits_monthly",
     "(month_start_date, membership_tier)"),
    ("idx_fctm_affinity_gin", "fact_customer_traits_monthly",
     "USING gin (secondary_affinity_tags)"),
    ("idx_fctm_active_opcos_gin", "fact_customer_traits_monthly",
     "USING gin (active_opco_codes)"),
    ("brin_fctm_month", "fact_customer_traits_monthly", "USING brin (month_start_date)"),

    # ----------------------------------------- bridge_customer_category_monthly
    ("idx_bccm_scope_month", "bridge_customer_category_monthly",
     "(opco_code, category_key, month_start_date)"),
    # Join back to the group table on customer_key for overlap questions.
    ("idx_bccm_customer_month", "bridge_customer_category_monthly",
     "(customer_key, month_start_date)"),
    ("idx_bccm_l1", "bridge_customer_category_monthly",
     "(opco_code, category_l1_key, month_start_date)"),
    ("idx_bccm_l2", "bridge_customer_category_monthly",
     "(opco_code, category_l2_key, month_start_date)"),
    ("brin_bccm_month", "bridge_customer_category_monthly", "USING brin (month_start_date)"),
)

ANALYZE_TABLES = (
    "fact_sales_daily",
    "agg_sales_daily",
    "agg_sales_sku_monthly",
    "fact_customer_opco_monthly",
    "fact_customer_traits_monthly",
    "bridge_customer_category_monthly",
)


def upgrade() -> None:
    for name, table, definition in INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {qualified(CORE, table)} {definition}"
        )

    for table in ANALYZE_TABLES:
        op.execute(f"ANALYZE {qualified(CORE, table)}")


def downgrade() -> None:
    # Indexes on partitioned parents cascade to their partitions.
    for name in reversed([index[0] for index in INDEXES]):
        op.execute(f"DROP INDEX IF EXISTS {qualified(CORE, name)}")
