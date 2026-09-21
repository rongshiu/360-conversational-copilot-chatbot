"""v3 facts: sales (daily/daypart) and customer (monthly, opaque key)

Two properties matter more than anything else here:

1. The sales facts carry NO customer key at all. "Who bought item X at store Y
   yesterday" has no column to answer with; it can only ever return a count.
2. The two customer tables carry `customer_key`, an opaque salted-hash
   surrogate whose mapping to the real customer_id lives outside every schema
   the copilot role can reach. The SQL validator permits it only inside
   COUNT(DISTINCT customer_key).

Two tables that were here are gone.

agg_sales_store_monthly rolled fact_sales_daily up to the month. At store x leaf
category grain the atomic table is already sparse, so the saving was ~2.5x rather
than the factor of thirty the cartesian arithmetic suggests -- too thin to earn
20M rows a year. v_sales_store_monthly now aggregates fact_sales_daily directly;
see AGGREGATE_VIEWS in v3_ddl.

fact_customer_group_monthly was the overlap table, one row per customer per month
across the group. It was never a rollup -- 1.2x against the OpCo fact -- and nine
of its sixteen columns were already replicated onto fact_customer_opco_monthly on
purpose. It existed because its row-level policy was different in kind from every
other table's, and there are no policies now. Its four unique columns are now on
fact_customer_traits_monthly, which is a customer-grain table again -- but one
that holds NO measures, which is what stopped the two from drifting last time.

`daypart` is a degenerate dimension (text + seq) rather than a 3-row dimension
table, so daypart questions need no join.

Revision ID: 20260727_03
Revises: 20260727_02
Create Date: 2026-07-27
"""

from __future__ import annotations

from datetime import date
from typing import Sequence, Union

from alembic import op

from app.db.v3_ddl import (
    CORE,
    create_monthly_partitions,
    qualified,
)

revision: str = "20260727_03"
down_revision: Union[str, Sequence[str], None] = "20260727_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed so a migration replay produces identical partitions.
PARTITION_ANCHOR = date(2026, 7, 1)

TABLES = (
    "bridge_customer_category_monthly",
    "fact_customer_traits_monthly",
    "fact_customer_opco_monthly",
    "agg_sales_sku_monthly",
    "agg_sales_daily",
    "fact_sales_daily",
)

DAYPART_CHECK = (
    "daypart text NOT NULL CHECK (daypart IN ('Morning','Afternoon','Evening')), "
    "daypart_seq smallint NOT NULL CHECK (daypart_seq BETWEEN 1 AND 3)"
)
CUSTOMER_TYPE_CHECK = (
    "customer_type text NOT NULL CHECK (customer_type IN ('Member','Non-Member'))"
)


def upgrade() -> None:
    # =================================================================
    # fact_sales_daily -- atomic serving grain
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'fact_sales_daily')} (
            calendar_date      date     NOT NULL,
            {DAYPART_CHECK},
            opco_code          text     NOT NULL,
            store_id           bigint   NOT NULL,
            category_key       bigint   NOT NULL,
            category_l1_key    bigint   NOT NULL,
            category_l2_key    bigint,
            category_l3_key    bigint,
            category_l4_key    bigint,
            {CUSTOMER_TYPE_CHECK},
            payment_type       text     NOT NULL
                               CHECK (payment_type IN ('ACS Credit','Non-ACS Credit',
                                                       'NorthCo Bank-Wallet','Cash-Other')),

            transaction_count  bigint        NOT NULL DEFAULT 0,
            line_item_count    bigint        NOT NULL DEFAULT 0,
            quantity           numeric(18,3) NOT NULL DEFAULT 0,
            customer_count     bigint        NOT NULL DEFAULT 0,

            gross_sales_amount numeric(18,2) NOT NULL DEFAULT 0,
            net_sales_amount   numeric(18,2) NOT NULL DEFAULT 0,
            discount_amount    numeric(18,2) NOT NULL DEFAULT 0,
            gmv_amount         numeric(18,2) NOT NULL DEFAULT 0,

            updated_at         timestamptz   NOT NULL DEFAULT now(),

            PRIMARY KEY (calendar_date, opco_code, store_id, category_key,
                         daypart, customer_type, payment_type)
        ) PARTITION BY RANGE (calendar_date)
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'fact_sales_daily')} IS "
        "'Atomic sales grain. NO customer key by design -- this is what makes "
        "customer-level answers structurally impossible.'"
    )
    op.execute(
        f"COMMENT ON COLUMN {qualified(CORE, 'fact_sales_daily')}.customer_count IS "
        "'Distinct customers AT THIS ROW GRAIN ONLY. NOT additive -- summing across "
        "rows double-counts anyone who shopped on more than one day. For a distinct "
        "count over a period use COUNT(DISTINCT customer_key) on the monthly customer "
        "tables instead.'"
    )

    # =================================================================
    # agg_sales_daily -- default table for performance / penetration / daypart
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'agg_sales_daily')} (
            calendar_date      date     NOT NULL,
            {DAYPART_CHECK},
            opco_code          text     NOT NULL,
            category_l1_key    bigint   NOT NULL,
            category_l2_key    bigint   NOT NULL DEFAULT 0,
            {CUSTOMER_TYPE_CHECK},

            transaction_count  bigint        NOT NULL DEFAULT 0,
            quantity           numeric(18,3) NOT NULL DEFAULT 0,
            customer_count     bigint        NOT NULL DEFAULT 0,

            gross_sales_amount numeric(18,2) NOT NULL DEFAULT 0,
            net_sales_amount   numeric(18,2) NOT NULL DEFAULT 0,
            gmv_amount         numeric(18,2) NOT NULL DEFAULT 0,

            PRIMARY KEY (calendar_date, opco_code, category_l1_key,
                         category_l2_key, daypart, customer_type)
        ) PARTITION BY RANGE (calendar_date)
        """
    )
    # category_l2_key defaults to 0 rather than NULL: it is part of the primary
    # key, and NULLs in a PK are not permitted. 0 is the "no level 2" sentinel.
    op.execute(
        f"COMMENT ON COLUMN {qualified(CORE, 'agg_sales_daily')}.category_l2_key IS "
        "'Level 2 ancestor, or 0 when the branch has no level 2. Part of the primary "
        "key, so a sentinel is used instead of NULL.'"
    )
    # =================================================================
    # agg_sales_sku_monthly -- optional. No store, no customer key.
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'agg_sales_sku_monthly')} (
            month_start_date   date     NOT NULL,
            opco_code          text     NOT NULL,
            sku_key            bigint   NOT NULL,
            category_key       bigint   NOT NULL,
            category_l1_key    bigint   NOT NULL,
            category_l2_key    bigint,
            category_l3_key    bigint,
            category_l4_key    bigint,
            -- Which branch sold it. Added so brand and SKU performance can be asked
            -- about one store or one region; the rule this table enforces is about
            -- CUSTOMER, not store, and no customer_key appears here.
            store_id           bigint   NOT NULL,
            -- Denormalised from dim_product, the same way the category ancestors
            -- are: brand is a property of the SKU, so it adds no rows, and a brand
            -- ranking is then a GROUP BY rather than a join.
            --
            -- Brand lives HERE and on dim_product, and nowhere near a customer. No
            -- table pairs a customer with a brand, so brand answers what SOLD --
            -- revenue, units, receipts, by store, month and member type -- and can
            -- never answer who bought it, brand affinity, or repeat purchase rate
            -- by brand.
            brand_name         text,
            {CUSTOMER_TYPE_CHECK},

            transaction_count  bigint        NOT NULL DEFAULT 0,
            quantity           numeric(18,3) NOT NULL DEFAULT 0,
            gross_sales_amount numeric(18,2) NOT NULL DEFAULT 0,

            PRIMARY KEY (month_start_date, opco_code, store_id, sku_key, customer_type),
            CONSTRAINT ck_agg_sku_month_start CHECK (date_trunc('month', month_start_date) = month_start_date)
        )
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'agg_sales_sku_monthly')} IS "
        "'Optional SKU and BRAND sales. Deliberately NO CUSTOMER KEY -- SKU x "
        "customer is the combination that must never exist, and the customer bridge "
        "stops at category, so nothing pairs a customer with a SKU or a brand. "
        "Brand here answers what sold, never who bought it.'"
    )

    # =================================================================
    # fact_customer_opco_monthly -- opco-scoped customer state
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'fact_customer_opco_monthly')} (
            customer_key                     bigint   NOT NULL,
            month_start_date                 date     NOT NULL,
            opco_code                        text     NOT NULL,

            -- WHAT LIVES HERE, AND WHAT DOES NOT.
            --
            -- This table is the customer's RELATIONSHIP WITH ONE OPCO in one month:
            -- what they did there, where they shopped, what it was worth. One row
            -- per customer per OpCo per month, so a customer active in NorthCo and
            -- NorthCo Mart occupies TWO rows and COUNT(*) is not a customer count --
            -- COUNT(DISTINCT customer_key) is, and the validator enforces it.
            --
            -- The customer's TRAITS -- who they are, what they hold, which segment
            -- they fall in -- are NOT here. They are in fact_customer_traits_monthly
            -- at customer x month, because they are true of the person and not of
            -- the OpCo row. They used to be here, repeated identically on every one
            -- of a customer's rows, and that had two costs: SUM and AVG over any of
            -- them silently weighted by how many OpCos the customer shops with, and
            -- a bucket derived from an OpCo-masked column disagreed with itself
            -- across the same customer's rows.
            --
            -- Join on customer_key AND month_start_date for trait-plus-metric
            -- questions. That join is the whole point of the split: it is one row
            -- on each side per customer-month, so it cannot fan out.

            is_member                        boolean  NOT NULL DEFAULT false,
            {CUSTOMER_TYPE_CHECK},
            is_active_in_opco                boolean  NOT NULL DEFAULT false,
            customer_status_in_opco          text,
            member_status                    text,
            is_primary_opco                  boolean  NOT NULL DEFAULT false,

            last_purchase_date               date,
            days_since_last_purchase         integer,
            active_months_6m                 smallint,
            active_months_12m                smallint,
            purchase_months_12m              smallint,
            longest_consecutive_months_count smallint,

            -- DAYS with a transaction here, which is not transaction_count: eight
            -- trips on one Saturday is one active day and eight transactions.
            active_days_in_month             smallint CHECK (active_days_in_month IS NULL OR active_days_in_month BETWEEN 0 AND 31),

            repeat_purchase_segment          text,
            purchase_ratio_segment           text,

            primary_store_id                 bigint,
            store_count                      smallint,
            preferred_store_type             text,
            nearest_store_change_status      text,

            -- Payment VOLUMES at this OpCo. The customer's preferred method is a
            -- trait and lives on the other table; these are what they actually did
            -- here, and they are fully additive.
            cash_txn_count                   integer,
            card_txn_count                   integer,
            ewallet_txn_count                integer,

            primary_daypart                  text
                                             CHECK (primary_daypart IS NULL OR primary_daypart IN
                                                   ('Morning','Afternoon','Evening')),
            primary_category_l1_key          bigint,
            category_l1_count                smallint,

            transaction_count                bigint        NOT NULL DEFAULT 0,
            quantity                         numeric(18,3) NOT NULL DEFAULT 0,
            total_revenue                    numeric(18,2) NOT NULL DEFAULT 0,
            total_gmv                        numeric(18,2) NOT NULL DEFAULT 0,

            -- Transactions here carrying a NorthValu line. The ADOPTION SEGMENT is a
            -- trait computed across every OpCo and lives on the other table; this is
            -- the per-OpCo count it is built from, kept here with the other
            -- volumes so it stays additive.
            northvalu_transaction_count        integer  CHECK (northvalu_transaction_count IS NULL OR
                                                             northvalu_transaction_count >= 0),

            PRIMARY KEY (customer_key, month_start_date, opco_code),
            CONSTRAINT ck_cust_opco_month_start CHECK (date_trunc('month', month_start_date) = month_start_date)
        ) PARTITION BY RANGE (month_start_date)
        """
    )
    # =================================================================
    # fact_customer_traits_monthly -- who the customer IS, once per month
    # =================================================================
    # ONE ROW PER CUSTOMER PER MONTH. No opco_code, and that is the entire point:
    # every column here is true of the PERSON, so putting an OpCo in the key would
    # force the same answer to be stored two or three times for anyone who shops
    # with more than one of us.
    #
    # These columns did live on fact_customer_opco_monthly, repeated identically on
    # each of a customer's rows. Two things went wrong with that, and both are the
    # kind that return a plausible number rather than an error:
    #
    #   SUM and AVG silently weighted by OpCo count. AVG(age) leaned toward
    #   multi-OpCo customers; SUM(lifetime_transaction_count) counted a two-OpCo
    #   customer's whole history twice. The validator had to carry a blocklist of
    #   column names to stop it, and a blocklist only protects the names on it.
    #
    #   A derived bucket disagreed with itself. financial_product_holding_bucket
    #   read active_credit_card_count, which is NULL outside NorthCo Credit, so the
    #   same customer was a Cross-Holder on their NorthCo Credit row and No Financial
    #   Product on their NorthCo row. "How many cross-holders" depended on which
    #   row you happened to count.
    #
    # At this grain COUNT(*) IS a customer count, which is the property the OpCo
    # table cannot have. Join to it on customer_key AND month_start_date when a
    # question needs a trait and a measure together -- one row each side, so the
    # join cannot fan out.
    #
    # THE RISK THIS RE-INTRODUCES, stated plainly because it has happened here
    # before: fact_customer_group_monthly was a customer-grain table alongside this
    # one, and the two drifted -- 791 customers were NorthCo Credit customers
    # according to one and 500 according to the other. The rule that keeps it from
    # recurring is that NO MEASURE IS DUPLICATED. Every count and every amount stays
    # on the OpCo table; this table holds labels, attributes and lifetime facts that
    # have no per-OpCo meaning. A bucket here is derived from that table by the ETL,
    # never stored on both.
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'fact_customer_traits_monthly')} (
            customer_key                     bigint   NOT NULL,
            month_start_date                 date     NOT NULL,

            -- Demographics. Constant for the person, which is why AVG(age) is
            -- finally safe here and was not on the OpCo table.
            age                              smallint CHECK (age IS NULL OR age BETWEEN 0 AND 120),
            gender_bucket                    text,
            generation_bucket                text,
            residency_status                 text
                                             CHECK (residency_status IS NULL OR
                                                    residency_status IN
                                                   ('Local','Non-local','Not Specified')),

            -- Membership. The programme is group-wide, so tier and tenure describe
            -- the person; whether they SHOPPED at a given OpCo is is_member on the
            -- other table.
            membership_tier                  text,
            member_join_date                 date,
            member_tenure_months             integer,
            tenure_bucket                    text,

            -- Where the customer stands with NorthCo, across every OpCo. Deliberately
            -- NOT per-OpCo: the audience source computes recency and activity over
            -- all participating OpCos, so "Churned" means they stopped shopping with
            -- us, not that they skipped one banner this month. A per-OpCo stage
            -- would also have made "how many churned customers" depend on which row
            -- you counted.
            --
            -- The per-OpCo recency it is judged against -- days_since_last_purchase,
            -- active_months_6m -- stays on the other table, so this will not tie out
            -- to any single OpCo's numbers.
            lifecycle_stage                  text,
            is_at_risk_customer              boolean  NOT NULL DEFAULT false,

            -- Lifetime, and therefore un-derivable from either table: the warehouse
            -- starts later than the customer does, so summing transaction_count
            -- across every partition gives activity SINCE THEN, not since the
            -- beginning.
            first_transaction_date           date,
            lifetime_transaction_count       integer  CHECK (lifetime_transaction_count IS NULL OR lifetime_transaction_count >= 0),

            -- THE SIX PUBLISHED TRAIT DIMENSIONS, all cross-OpCo. Each is computed
            -- by the ETL from the customer's rows on fact_customer_opco_monthly --
            -- across every OpCo, not one -- so "Heavy shopper" means eight trips
            -- with NorthCo, not eight trips at NorthCo. That is the definition the
            -- audience tool uses, and matching it is why they are cross-OpCo here
            -- rather than per row.
            visit_frequency_bucket           text
                                             CHECK (visit_frequency_bucket IS NULL OR
                                                    visit_frequency_bucket IN
                                                   ('Heavy','Regular','Occasional','Inactive')),
            basket_size_bucket               text,
            northvalu_adoption_segment         text,
            family_segment                   text,
            financial_product_holding_bucket text,
            preferred_payment_segment        text,

            -- Which rail they use most across the group. The per-OpCo volumes it is
            -- read from stay on the other table.
            primary_payment_type             text,

            -- Every life-stage profile they score above threshold on, not just the
            -- winning one in family_segment. Containment tests, never equality.
            -- Empty array, never NULL.
            secondary_affinity_tags          text[]   NOT NULL DEFAULT '{{}}',

            -- Product holdings. A holding is a state of the PERSON: an NorthCo Credit
            -- card is held by the customer, not by their NorthCo relationship. On
            -- the OpCo table these were NULL outside NorthCo Credit -- "this OpCo
            -- cannot say" -- and that NULL is what made the bucket contradict
            -- itself. Here there is no OpCo to not say, so 0 means none.
            active_credit_card_count         smallint CHECK (active_credit_card_count IS NULL OR active_credit_card_count >= 0),
            active_loan_count                smallint CHECK (active_loan_count IS NULL OR active_loan_count >= 0),
            active_insurance_count           smallint CHECK (active_insurance_count IS NULL OR active_insurance_count >= 0),
            has_northco_bank                    boolean,
            cross_holder_combo               text,
            no_product_eligibility_status    text,

            -- Which OpCos the customer was active in this month. Definitionally a
            -- customer-month fact, and the reason this table can answer cross-OpCo
            -- overlap with a containment test instead of a self join.
            active_opco_codes                text[]        NOT NULL,
            active_opco_count                smallint      NOT NULL DEFAULT 1,
            is_multi_opco_customer           boolean       NOT NULL DEFAULT false,
            primary_opco_code                text,

            PRIMARY KEY (customer_key, month_start_date),
            CONSTRAINT ck_cust_traits_month_start CHECK (date_trunc('month', month_start_date) = month_start_date)
        ) PARTITION BY RANGE (month_start_date)
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'fact_customer_traits_monthly')} IS "
        "'One row per customer per month -- no OpCo. Every column describes the "
        "PERSON, so COUNT(*) here IS a customer count, unlike on "
        "fact_customer_opco_monthly. Join on customer_key AND month_start_date for "
        "trait-plus-metric questions. Holds no measures: counts and amounts live on "
        "the OpCo table so the two can never disagree.'"
    )
    op.execute(
        f"COMMENT ON COLUMN {qualified(CORE, 'fact_customer_traits_monthly')}.customer_key IS "
        "'IDENTITY COLUMN. Same opaque salted hash as fact_customer_opco_monthly, so "
        "the two join. Never in the outermost SELECT or GROUP BY, and never inside an "
        "aggregate other than COUNT(DISTINCT).'"
    )
    op.execute(
        f"COMMENT ON COLUMN {qualified(CORE, 'fact_customer_traits_monthly')}.active_opco_codes IS "
        "'Every OpCo this customer was active in THIS MONTH. Cross-OpCo overlap is "
        "a containment test on this column, not a join: "
        "active_opco_codes @> ARRAY[''NORTHCO_MART'']. One row per customer here, so no "
        "DISTINCT is needed. The set is per month, so testing two OpCos in one "
        "predicate asks about the same month; overlap anywhere in a longer period "
        "needs a grouped subquery over the range.'"
    )
    op.execute(
        f"COMMENT ON COLUMN {qualified(CORE, 'fact_customer_traits_monthly')}.visit_frequency_bucket IS "
        "'CROSS-OPCO. Banded from the customer''s transactions across every OpCo, not "
        "at one, so it will not tie out to a single OpCo''s transaction_count.'"
    )

    op.execute(
        f"COMMENT ON COLUMN {qualified(CORE, 'fact_customer_opco_monthly')}.customer_key IS "
        "'IDENTITY. Opaque salted-hash surrogate. Legal ONLY inside "
        "COUNT(DISTINCT customer_key). Never project, group, or order by it.'"
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'fact_customer_opco_monthly')} IS "
        "'One row per customer PER OPCO per month. A customer active in two OpCos "
        "occupies two rows, so COUNT(*) is not a customer count -- "
        "COUNT(DISTINCT customer_key) is. Enforced by the validator. Holds what the "
        "customer DID at this OpCo; who they ARE is on fact_customer_traits_monthly, "
        "joined on customer_key AND month_start_date.'"
    )

    # =================================================================
    # bridge_customer_category_monthly -- category-scoped customer counts
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'bridge_customer_category_monthly')} (
            customer_key      bigint   NOT NULL,
            month_start_date  date     NOT NULL,
            opco_code         text     NOT NULL,

            -- LEAF GRAIN. One row per customer per month per store per deepest
            -- category node, with every ancestor denormalised beside it.
            --
            -- This table used to hold levels 1 and 2 only, as a row PER LEVEL, and
            -- both halves of that were trouble. The depth cost precision: asked for
            -- "Fashion Accessories" -- a level 4 node -- the only customer-level
            -- answer available was its level 2 ancestor PAGEMARK, a bookstore
            -- division, and the question came back answered about the wrong thing
            -- with an apology attached. The row-per-level cost correctness: a
            -- customer appeared once at level 1 and again at level 2, so every
            -- query had to remember to filter category_level or silently
            -- double-count them.
            --
            -- Storing the leaf and denormalising its ancestors fixes both at once.
            -- A level 2 question filters category_l2_key, a level 4 question filters
            -- category_l4_key, and a customer appears once either way.
            category_key      bigint   NOT NULL,
            category_level    smallint NOT NULL CHECK (category_level BETWEEN 1 AND 4),
            category_l1_key   bigint   NOT NULL,
            category_l2_key   bigint,
            category_l3_key   bigint,
            category_l4_key   bigint,

            -- WHERE, so that a demographic question can name a store. This was the
            -- single biggest hole in the schema: the only table with store x
            -- category x revenue (the sales facts) has no customer at all,
            -- and the only table with customer x category had no store and no
            -- money -- so "average basket size for Gen Z in Fashion at Inglegate Juniperford"
            -- was unanswerable from either side.
            store_id          bigint   NOT NULL,

            -- NO BRAND, and no SKU. Brand was carried here briefly and taken out:
            -- customer x month x store x LEAF CATEGORY is already the widest object
            -- in the schema, and multiplying it again by the brands within each
            -- category buys one class of question at a cost that lands on every
            -- other. Brand-level SALES live on agg_sales_sku_monthly via
            -- dim_product; what is given up is brand affinity and repeat-purchase
            -- rate PER CUSTOMER, which nothing else can answer.

            transaction_count bigint        NOT NULL DEFAULT 0,
            quantity          numeric(18,3) NOT NULL DEFAULT 0,
            gross_sales_amount numeric(18,2) NOT NULL DEFAULT 0,

            PRIMARY KEY (customer_key, month_start_date, opco_code, category_key,
                         store_id),
            CONSTRAINT ck_bridge_cat_month_start CHECK (date_trunc('month', month_start_date) = month_start_date)
        ) PARTITION BY RANGE (month_start_date)
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'bridge_customer_category_monthly')} IS "
        "'Category-scoped customer activity: who bought what, where, and for how "
        "much. LEAF GRAIN with every ancestor denormalised, so a question filters "
        "category_l1_key..category_l4_key at whatever depth it names and a customer "
        "is counted once. Carries store_id; deliberately carries no brand and no "
        "SKU. Replaces the v2 has_*_line flags, which leaked out-of-scope category "
        "signal.'"
    )

    # =================================================================
    # Partitions
    # =================================================================
    for table, _key in (
        ("fact_sales_daily", "calendar_date"),
        ("agg_sales_daily", "calendar_date"),
        ("fact_customer_opco_monthly", "month_start_date"),
        ("fact_customer_traits_monthly", "month_start_date"),
        ("bridge_customer_category_monthly", "month_start_date"),
    ):
        made = create_monthly_partitions(op.execute, CORE, table, PARTITION_ANCHOR)
        print(f"[v3] {table}: {made} monthly partitions + DEFAULT")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {qualified(CORE, table)} CASCADE")
