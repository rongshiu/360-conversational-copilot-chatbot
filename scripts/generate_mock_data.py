#!/usr/bin/env python
"""Generate mock fact data for the v3 serving schema.

    python scripts/generate_mock_data.py --months 6 --customers 5000

Run AFTER `alembic upgrade head` and `python -m app.scripts.load_reference_data`,
because every fact row references a real category_key, store_id and opco_code.

The data is deliberately shaped so the questions this copilot exists to answer
have visibly correct answers rather than noise:

  - Evening outsells Morning and Afternoon, so "which daypart performs best" has a
    stable answer instead of a coin flip.
  - Membership penetration sits near 70% of transactions, so a penetration figure
    is recognisably plausible.
  - Sales grow ~12% year over year, so a YoY question returns a clear direction.
  - Weekends run ~35% above weekdays, so daily series look like retail.
  - Customers hold more than one OpCo: ~18% of non-credit customers also hold
    NORTHCO_CREDIT, and ~9% of retail customers shop the other retail banner. Both
    cross-OpCo overlap questions therefore return a non-trivial number instead of a
    zero that belongs to the generator rather than to the business.

The window ends YESTERDAY, so the month in progress is present and a question
meaning "now" has something to answer with. Pass --anchor to pin it instead.

Deterministic for a given window: the RNG is seeded, so two runs on the same day
produce identical data and a regression in an answer is a code change rather than
a data change. Across days the window itself moves, which is the price of having
current data; --anchor buys the old fixed behaviour back.
"""
from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import settings  # noqa: E402
from app.db.postgres import get_pg_conn  # noqa: E402
from app.scripts.load_reference_data import read_enum_values  # noqa: E402
from app.utils.text_utils import safe_schema_name  # noqa: E402

CORE = safe_schema_name(settings.ci_core_schema)

DAYPARTS = (("Morning", 1, 0.28), ("Afternoon", 2, 0.31), ("Evening", 3, 0.41))
MEMBER_SHARE = 0.70
WEEKEND_LIFT = 1.35
YOY_GROWTH = 1.12

def _enum(column: str) -> tuple[str, ...]:
    """The documented values of one column, from the glossary.

    These used to be five literal tuples here, and they drifted the moment the
    glossary was edited: the seeder kept writing "Gold" and "Reactivated" after
    those stopped being values of membership_tier and lifecycle_stage. Nothing
    fails when that happens -- the columns are plain text -- so the copilot
    resolves "1 Star" from the catalogue, filters on it, and reports a confident
    zero for a tier that every customer in the table actually has.

    The glossary is the definition of these columns, so it is what the generator
    reads. Missing values are fatal rather than defaulted: seeding a column with
    something the copilot cannot resolve produces exactly the silent-zero class of
    bug this file exists to exercise.
    """
    values = ENUM_VALUES.get(column) or []
    if not values:
        raise SystemExit(
            f"{column} has no enum_values in the glossary "
            f"({settings.glossary_csv_path} or ci_meta.copilot_glossary). "
            "Load the glossary before seeding mock data."
        )
    return tuple(values)


ENUM_VALUES = read_enum_values()

TIERS = _enum("membership_tier")
LIFECYCLES = _enum("lifecycle_stage")
VISIT_FREQUENCY = _enum("visit_frequency_bucket")
BASKET_SIZES = _enum("basket_size_bucket")
NORTHVALU_ADOPTION = _enum("northvalu_adoption_segment")
PRODUCT_HOLDING = _enum("financial_product_holding_bucket")
PAYMENT_SEGMENTS = _enum("preferred_payment_segment")
AFFINITY_TAGS = _enum("secondary_affinity_tags")
RESIDENCY = _enum("residency_status")
GENDERS = _enum("gender_bucket")
GENERATIONS = _enum("generation_bucket")
FAMILY_SEGMENTS = _enum("family_segment")
TENURE_BUCKETS = _enum("tenure_bucket")

# Payment mix. The VALUES come from the glossary like every other enum -- these
# were a literal tuple of ("Card", "Cash", "E-wallet", "Other") until payment_type
# was rebucketed onto media_code, which is the drift the docstring above describes.
# Only the WEIGHTS live here, because the glossary documents a domain, not a
# distribution; a value the weights do not name still gets seeded, uniformly, so a
# future enum change cannot silently drop a bucket out of the mock data.
PAYMENT_WEIGHTS = {
    "Cash-Other": 0.38,
    "Non-ACS Credit": 0.28,
    "ACS Credit": 0.22,
    "NorthCo Bank-Wallet": 0.12,
}
PAYMENTS = tuple(
    (value, PAYMENT_WEIGHTS.get(value, 1.0 / len(_enum("payment_type"))))
    for value in _enum("payment_type")
)

# How many ACTIVE holdings of each product an NorthCo Credit customer has, weighted.
#
# The zero row matters as much as the rest. NorthCo Credit sells cards, loans and
# insurance, and a real population holds some and not others -- which is exactly
# what made the old proxy wrong: `opco_code = 'NORTHCO_CREDIT' AND is_active_in_opco`
# counted loan-only and insurance-only customers as cardholders. If every seeded
# NorthCo Credit customer held a card, that difference would be invisible and no test
# could catch the regression.
HOLDING_WEIGHTS: dict[str, tuple[tuple[int, float], ...]] = {
    "credit_card": ((0, 0.38), (1, 0.49), (2, 0.13)),
    "loan": ((0, 0.62), (1, 0.31), (2, 0.07)),
    "insurance": ((0, 0.71), (1, 0.26), (2, 0.03)),
}

# The OpCo that issues these products. Every other OpCo's row leaves the counts
# NULL: NorthCo issues no cards, and a 0 there would read as "this customer has no
# card" rather than "this OpCo cannot say".
HOLDING_OPCO = "NORTHCO_CREDIT"

# The digital bank. A relationship with it is OpCo membership, nothing more -- it
# needs no holding column of its own.
BANK_OPCO = "NORTHCO_BANK"

# Ages that agree with the generation a row is given. Random-and-independent was
# not neutral: "average age of Gen Z customers" came back at 47 because the two
# columns were drawn separately, and an answer that self-contradicts is worse than
# a NULL the copilot can say it does not have.
GENERATION_AGE_RANGE: dict[str, tuple[int, int]] = {
    "Gen Z": (18, 28),
    "Millennial": (29, 44),
    "Gen X": (45, 60),
    "Boomer": (61, 79),
    "Silent Generation": (80, 95),
}


# One leaf category with its full ancestor chain: (leaf, l1, l2, l3, l4).
#
# l2..l4 are Optional because a branch may legitimately stop short -- a leaf at
# level 2 has no level 3 or 4 ancestor of its own. They must NOT be forced to
# NULL wholesale, which is what the seeder used to do: it loaded only (leaf, l1,
# l2) and inserted `None, None` for the deeper pair, so all 27,513 fact rows had
# category_l3_key and category_l4_key empty even though every leaf in
# dim_product_category is a level-4 node with a complete chain.
#
# RLS survived that, because its predicate also ORs on `category_key` and the
# grant expansion includes descendants -- but a planner filter did not. A question
# resolving to LAMB (category_l4_key = 2479) filtered a column that was NULL in
# every row and got zero, which reads as "nobody bought lamb" rather than "the
# seed data cannot answer this".
LeafChain = tuple[int, int, Optional[int], Optional[int], Optional[int]]

# Leaf categories given sales per store per day. Total draws across the period
# must exceed the leaf count for coverage to be complete: 181 days x 6 stores x
# 8 = 8,688 draws against 4,159 NORTHCO leaves.
LEAVES_PER_STORE_DAY = 8

# Stores given sales per OpCo per day. Sized so a full rotation completes inside a
# month, so every store has rows in every month rather than only in the months its
# turn happened to fall in.
MIN_STORES_PER_DAY = 6
DAYS_FOR_FULL_STORE_COVERAGE = 28

# Retail customers who also shop the other retail banner. Kept lower than the
# NORTHCO_CREDIT rate: cross-banner shopping is real but less common than holding a
# credit product alongside a retail relationship.
RETAIL_CROSS_SHOP: dict[str, tuple[str, ...]] = {
    "NORTHCO": ("NORTHCO_MART",),
    "NORTHCO_MART": ("NORTHCO",),
}
RETAIL_CROSS_SHOP_RATE = 0.09

# Customers who also hold an NorthCo Bank relationship. Without it, NorthCo Bank was
# reachable only as somebody's home OpCo, so "retail members who shopped at both
# banners AND are NorthCo Bank members" was 0.0% by construction -- a real answer to a
# question the ecosystem exists to ask, produced by the generator rather than by
# the business.
BANK_LINK_RATE = 0.15

# Stores a customer can call their primary one, taken from the head of each OpCo's
# store list (ordered by store_id, so this is stable across runs and includes the
# flagship branches -- INGLEGATE JUNIPERFORD is 1007 at NorthCo and 1016 at NorthCo Mart).
#
# Customers are concentrated rather than spread across all 489 NorthCo stores
# because customer x category x store is otherwise too thin to answer anything:
# 500 customers over 489 stores put roughly one customer in each store x category
# cell, so "average basket size for Gen Z in Home Fashion at Inglegate Juniperford" returned
# an empty result -- not because the schema could not express it, but because the
# generator had scattered the population. Real customer bases concentrate on
# flagship stores; this makes the seed behave that way.
#
# Sales data still covers every store: fact_sales_daily rotates through all of
# them, so store-level SALES questions work for any branch. Only the customer-level
# demographic breakdowns are limited to these.
STORE_CONCENTRATION = 8

# Category pairs each customer buys in a month. Raised from 2 because a three-way
# breakdown -- store x category x generation -- divides the population a long way
# down: 500 NorthCo customers over 48 category pairs, 8 stores and 6 generations
# leaves well under one customer per cell, so most of the breakdown is 0 or 1.
# Raising it does not make the data denser per customer so much as it makes the
# joint distribution wide enough to ask a real question of.
#
# For a demo that needs every cell populated, seed more customers rather than more
# categories each: MOCK_CUSTOMERS=20000 in .env, or --customers 20000.
CATEGORIES_PER_CUSTOMER = 5



@dataclass
class Ref:
    opcos: list[str]
    stores: dict[str, list[int]]                       # opco -> store ids
    leaf_by_opco: dict[str, list[LeafChain]]           # opco -> leaf chains
    l1l2_by_opco: dict[str, list[tuple[int, int]]]     # opco -> (l1, l2)
    # (opco, l1, l2) -> the leaves under that division, each with its own level and
    # full ancestor chain. The customer bridge is leaf-grained, so seeding it needs
    # to descend from the division a customer shops into the actual nodes beneath.
    leaf_by_l2: dict[tuple[str, int, int], list[tuple]]


def load_reference(cur) -> Ref:
    # opco_type, not is_group_entity. That column marked N360 as the holding entity
    # so group access could be derived from it; it went with that access model and
    # is not in the schema. The holding entity still carries no fact rows, so it
    # is still excluded here -- the test is just spelled differently.
    cur.execute(
        f"SELECT opco_code FROM {CORE}.dim_opco WHERE opco_type <> 'GROUP' ORDER BY 1"
    )
    opcos = [r[0] for r in cur.fetchall()]

    cur.execute(f"SELECT opco_code, store_id FROM {CORE}.dim_store ORDER BY 1, 2")
    stores: dict[str, list[int]] = {}
    for opco, store_id in cur.fetchall():
        stores.setdefault(opco, []).append(int(store_id))

    # Leaf nodes carry the full ancestor chain, which is what the atomic fact
    # needs: fact_sales_daily is declared at category depth 4 in
    # app/db/v3_ddl.py and app/service/table_registry.py, so every level the
    # dimension knows about has to reach the fact table.
    #
    # Selected raw, not through COALESCE: a NULL ancestor means "this branch stops
    # here" and the fact column must hold NULL too. Substituting 0 would create a
    # key that matches no category.
    cur.execute(
        f"""
        SELECT opco_code, category_key, category_level, l1_key, l2_key, l3_key, l4_key
        FROM {CORE}.dim_product_category
        WHERE is_leaf
        ORDER BY 1, 2
        """
    )
    leaf_by_opco: dict[str, list[LeafChain]] = {}
    leaf_by_l2: dict[tuple[str, int, int], list[tuple]] = {}
    for opco, leaf, level, l1, l2, l3, l4 in cur.fetchall():
        chain = (
            int(leaf),
            int(l1),
            int(l2) if l2 is not None else None,
            int(l3) if l3 is not None else None,
            int(l4) if l4 is not None else None,
        )
        leaf_by_opco.setdefault(opco, []).append(chain)
        leaf_by_l2.setdefault((opco, int(l1), int(l2 or 0)), []).append(
            (*chain, int(level))
        )

    # The rollup is keyed on l1/l2 only, so its grain must be the distinct pairs.
    cur.execute(
        f"""
        SELECT DISTINCT opco_code, l1_key, COALESCE(l2_key, 0)
        FROM {CORE}.dim_product_category
        WHERE category_level <= 2
        ORDER BY 1, 2, 3
        """
    )
    l1l2_by_opco: dict[str, list[tuple[int, int]]] = {}
    for opco, l1, l2 in cur.fetchall():
        l1l2_by_opco.setdefault(opco, []).append((int(l1), int(l2)))

    if not opcos or not stores or not leaf_by_opco:
        raise RuntimeError(
            "Reference data is empty. Run `python -m app.scripts.load_reference_data` first."
        )
    return Ref(opcos, stores, leaf_by_opco, l1l2_by_opco, leaf_by_l2)


def _today() -> date:
    """The business date, from the same timezone the planner's prompt uses."""
    from zoneinfo import ZoneInfo

    tz_name = getattr(settings, "copilot_timezone", "Asia/Kuala_Lumpur")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def _weighted(rng: random.Random, options):
    total = sum(w for *_, w in options)
    roll = rng.random() * total
    upto = 0.0
    for item in options:
        upto += item[-1]
        if roll <= upto:
            return item
    return options[-1]


# _shifted, _tier_change and _direction lived here, along with a CHANGE_RATE of
# 0.18. They manufactured last month's tier, segment and lifecycle stage, and the
# movement flag each pair implied, so that "how many upgraded to 4 Star" had
# something to filter on. The prior-period columns they populated are no longer
# in the schema: a transition is now a self-join across two month_start_date
# values, so the previous state is a row in the same table rather than a column on
# this one. Their care about ordered scales -- a customer goes 3 Star to 4 Star,
# never 1 Star to 4 Star in a month, and the flag must never contradict the pair
# it describes -- is the constraint to restore if per-month state is ever carried
# forward across the month loop.


def _require(column: str, values: tuple[str, ...], *expected: str) -> None:
    """Fail loudly when a bucket's labels no longer match the glossary.

    The derivations below band a number into a NAMED bucket, so unlike every other
    enum here they cannot take their values from the glossary alone -- a threshold
    has to know which side of it is called what. _enum keeps the value SET honest;
    this keeps the mapping honest. Without it a renamed bucket would leave the
    seeder writing a label the copilot can no longer resolve, which is the same
    silent-zero failure _enum exists to prevent, one level up.
    """
    missing = [value for value in expected if value not in values]
    if missing:
        raise SystemExit(
            f"{column} no longer offers {missing}. The derivation in "
            f"generate_mock_data.py bands numbers into these labels by name, so it "
            f"needs updating to match the glossary before mock data can be seeded."
        )


_require("visit_frequency_bucket", VISIT_FREQUENCY,
         "Heavy", "Regular", "Occasional", "Inactive")
_require("basket_size_bucket", BASKET_SIZES,
         "Small Basket", "Medium Basket", "Large Basket", "Premium Basket", "Inactive")
_require("northvalu_adoption_segment", NORTHVALU_ADOPTION,
         "Frequently Buy", "Rarely Buy", "Never Buy", "Insufficient Data")
_require("financial_product_holding_bucket", PRODUCT_HOLDING,
         "No Financial Product", "ACS Credit Card only", "NorthCo Personal Loan only",
         "NorthCo Bank only", "Cross-Holder (2+ Products)")
_require("preferred_payment_segment", PAYMENT_SEGMENTS,
         "ACS Loyalist", "ACS Off-Card Spender", "High-Intent Competitor Cardholder",
         "NorthCo Bank Native", "Uncaptured Shopper")
_require("residency_status", RESIDENCY, "Local", "Non-local", "Not Specified")


# The six trait dimensions are DERIVED, not drawn. Drawn independently, a row
# could say "Heavy" while carrying two transactions, and the two answers to "how
# many Heavy shoppers" -- one counting the bucket, one banding transaction_count
# -- would disagree with no way to tell which was right. This is the same rule the
# retired _tier_change followed, and the reason it is worth keeping: the bucket
# and the measure behind it are written from one number.

def _visit_frequency(txns: int) -> str:
    if txns >= 8:
        return "Heavy"
    if txns >= 3:
        return "Regular"
    if txns >= 1:
        return "Occasional"
    return "Inactive"


def _basket_size(txns: int, revenue: float) -> str:
    if txns < 1:
        return "Inactive"
    average = revenue / txns
    if average < 30:
        return "Small Basket"
    if average <= 130:
        return "Medium Basket"
    if average <= 450:
        return "Large Basket"
    return "Premium Basket"


def _northvalu_adoption(txns: int, northvalu_txns: int) -> str:
    """Insufficient Data is a thin month, not low adoption -- see the glossary."""
    if txns < 3:
        return "Insufficient Data"
    if northvalu_txns == 0:
        return "Never Buy"
    return "Frequently Buy" if northvalu_txns / txns >= 0.5 else "Rarely Buy"


def _product_holding(cards: int | None, loans: int | None, has_bank: bool) -> str:
    held = (bool(cards), bool(loans), has_bank)
    if sum(held) >= 2:
        return "Cross-Holder (2+ Products)"
    if held[0]:
        return "ACS Credit Card only"
    if held[1]:
        return "NorthCo Personal Loan only"
    if held[2]:
        return "NorthCo Bank only"
    return "No Financial Product"


def _cross_holder_combo(cards: int | None, loans: int | None, has_bank: bool) -> str | None:
    """Which combination, or None below two products -- so it agrees with the bucket."""
    parts = [
        name
        for name, held in (
            ("Credit Card", bool(cards)),
            ("NorthCo Credit Financing", bool(loans)),
            ("NorthCo Bank", has_bank),
        )
        if held
    ]
    return " + ".join(parts) if len(parts) >= 2 else None


def _payment_segment(has_card: bool, payment: str) -> str:
    """Card ownership crossed with the rail actually used.

    The whole point of the column is that neither input alone gives it: an ACS
    Loyalist and an ACS Off-Card Spender hold the same card, and an Off-Card
    Spender and a Competitor Cardholder pay the same way.
    """
    if has_card:
        return "ACS Loyalist" if payment == "ACS Credit" else "ACS Off-Card Spender"
    if payment == "Non-ACS Credit":
        return "High-Intent Competitor Cardholder"
    if payment == "NorthCo Bank-Wallet":
        return "NorthCo Bank Native"
    return "Uncaptured Shopper"


def _eligibility(holding: str, residency: str | None) -> str | None:
    """Populated only for the customers it describes: those holding nothing."""
    if holding != "No Financial Product" or not residency:
        return None
    if residency == "Local":
        return "Local (eligible)"
    return "Non-local (ineligible)" if residency == "Non-local" else None


def _day_factor(day: date, base_year: int) -> float:
    factor = WEEKEND_LIFT if day.weekday() >= 5 else 1.0
    if day.year > base_year:
        factor *= YOY_GROWTH
    return factor


def generate(months: int, customers: int, seed: int, anchor: date | None) -> None:
    """Seed the facts, ending YESTERDAY unless an explicit anchor is given.

    The window used to stop at the last COMPLETE month, which left the copilot with
    nothing for the month in progress -- so every question meaning "now" answered a
    confident 0 while "last month" answered normally. The two are indistinguishable
    to a reader, and the zero is the more believable of them, which is the worst way
    for a data gap to present.

    Yesterday rather than today because a day still in progress would be seeded as a
    full day of trading and read as a real, complete figure.

    The cost is reproducibility: the window now moves with the calendar, so two runs
    a day apart differ by a day. `--anchor` pins it for anyone who needs the old
    fixed behaviour, and the RNG seed still makes a given window identical run to
    run.
    """
    rng = random.Random(seed)

    end = (anchor - timedelta(days=1)) if anchor else (_today() - timedelta(days=1))
    start = end.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    base_year = start.year

    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET LOCAL ROLE "{settings.ci_loader_role}"')
            ref = load_reference(cur)

            # agg_sales_store_monthly and fact_customer_group_monthly were here.
            # Neither is in the schema: the store rollup is now
            # v_sales_store_monthly aggregating fact_sales_daily, and the overlap
            # table's four unique columns are on fact_customer_traits_monthly.
            #
            # fact_customer_traits_monthly MUST be truncated with the rest. The
            # inserts below are ON CONFLICT DO NOTHING, so a re-run with fewer
            # customers would silently keep the previous run's trait rows -- leaving
            # customers with a trait row and no OpCo rows, which is exactly the
            # cross-table disagreement the split was supposed to make impossible.
            for table in (
                "agg_sales_daily",
                "fact_sales_daily",
                "bridge_customer_category_monthly",
                "fact_customer_opco_monthly",
                "fact_customer_traits_monthly",
                "agg_sales_sku_monthly",
            ):
                cur.execute(f"TRUNCATE TABLE {CORE}.{table}")

            # ---------------------------------------------------------------
            # agg_sales_daily -- the workhorse. Full daily x daypart coverage.
            # ---------------------------------------------------------------
            rows: list[tuple] = []
            day = start
            while day <= end:
                factor = _day_factor(day, base_year)
                for opco in ref.opcos:
                    # Every (l1, l2) pair, not the first 12. NORTHCO has 48, so
                    # truncating dropped 5 of its 7 divisions from this table
                    # entirely -- and this is the table the planner prefers for
                    # summary questions. A caller granted HARD therefore got a
                    # confident "0 transactions" for a division with 1,640 rows in
                    # the atomic fact.
                    for l1, l2 in ref.l1l2_by_opco.get(opco, []):
                        for daypart, seq, share in DAYPARTS:
                            for ctype, weight in (("Member", MEMBER_SHARE), ("Non-Member", 1 - MEMBER_SHARE)):
                                txn = max(1, int(rng.gauss(900, 160) * factor * share * weight))
                                qty = round(txn * rng.uniform(1.7, 2.6), 3)
                                cust = max(1, int(txn * rng.uniform(0.72, 0.9)))
                                atv = rng.uniform(38, 92)
                                gross = round(txn * atv, 2)
                                rows.append((
                                    day, daypart, seq, opco, l1, l2, ctype,
                                    txn, qty, cust,
                                    gross, round(gross * 0.94, 2), gross,
                                ))
                day += timedelta(days=1)

            cur.executemany(
                f"""INSERT INTO {CORE}.agg_sales_daily
                    (calendar_date, daypart, daypart_seq, opco_code, category_l1_key,
                     category_l2_key, customer_type, transaction_count, quantity,
                     customer_count, gross_sales_amount, net_sales_amount, gmv_amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
            agg_rows = len(rows)

            # ---------------------------------------------------------------
            # fact_sales_daily -- atomic. Sampled, because the full cross product
            # of store x leaf category x daypart is far larger than a mock set
            # needs and would take minutes to insert.
            # ---------------------------------------------------------------
            # The deliberate single-customer probe row was here. It existed so
            # small-cell suppression had a guaranteed case to catch end to end;
            # suppression is not implemented, so the probe has
            # nothing left to prove. Single-customer cells still arise naturally --
            # around 700 of them -- and are now simply answered.
            rows: list[tuple] = []
            leaf_cursor: dict[str, int] = {opco: 0 for opco in ref.opcos}
            store_cursor: dict[str, int] = {opco: 0 for opco in ref.opcos}
            day = start
            while day <= end:
                factor = _day_factor(day, base_year)
                for opco in ref.opcos:
                    all_stores = ref.stores.get(opco, [])
                    leaves = ref.leaf_by_opco.get(opco, [])
                    if not all_stores or not leaves:
                        continue

                    # Rotate stores instead of always using the first six. With 489
                    # NORTHCO stores, a fixed slice left 483 of them with no rows at
                    # all -- so a question about a store the resolver happily matched
                    # returned zero, which reads as "no sales" rather than "not in
                    # the sample". The per-day count is sized so every store appears
                    # at least once a month, keeping row counts in the same range.
                    per_day = max(
                        MIN_STORES_PER_DAY,
                        -(-len(all_stores) // DAYS_FOR_FULL_STORE_COVERAGE),
                    )
                    take_stores = min(per_day, len(all_stores))
                    scursor = store_cursor[opco]
                    stores = [
                        all_stores[(scursor + i) % len(all_stores)]
                        for i in range(take_stores)
                    ]
                    store_cursor[opco] = (scursor + take_stores) % len(all_stores)

                    for store in stores:
                        # Rotate through the leaf list instead of sampling it. Random
                        # sampling left ~500 of 4,855 leaves with no sales anywhere in
                        # the period, so a filter on one of them -- LAMB, for instance
                        # -- returned zero even once the L3/L4 columns were populated.
                        # For a dataset whose purpose is exercising category filters,
                        # complete coverage matters more than sporadic appearance, and
                        # rotation keeps it deterministic.
                        take = min(LEAVES_PER_STORE_DAY, len(leaves))
                        cursor = leaf_cursor[opco]
                        picked = [leaves[(cursor + i) % len(leaves)] for i in range(take)]
                        leaf_cursor[opco] = (cursor + take) % len(leaves)

                        for leaf, l1, l2, l3, l4 in picked:
                            daypart, seq, share = _weighted(rng, DAYPARTS)
                            ctype = "Member" if rng.random() < MEMBER_SHARE else "Non-Member"
                            payment, _ = _weighted(rng, PAYMENTS)
                            txn = max(1, int(rng.gauss(24, 8) * factor * share))
                            qty = round(txn * rng.uniform(1.6, 2.8), 3)
                            atv = rng.uniform(35, 95)
                            gross = round(txn * atv, 2)
                            rows.append((
                                day, daypart, seq, opco, store, leaf, l1,
                                l2, l3, l4, ctype, payment,
                                txn, int(txn * rng.uniform(1.4, 2.2)), qty,
                                max(1, int(txn * rng.uniform(0.7, 0.95))),
                                gross, round(gross * 0.94, 2),
                                round(gross * 0.06, 2), gross,
                            ))
                day += timedelta(days=1)

            cur.executemany(
                f"""INSERT INTO {CORE}.fact_sales_daily
                    (calendar_date, daypart, daypart_seq, opco_code, store_id,
                     category_key, category_l1_key, category_l2_key, category_l3_key,
                     category_l4_key, customer_type, payment_type,
                     transaction_count, line_item_count, quantity, customer_count,
                     gross_sales_amount, net_sales_amount, discount_amount, gmv_amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                rows,
            )
            atomic_rows = len(rows)

            # ---------------------------------------------------------------
            # Customer tables -- monthly
            # ---------------------------------------------------------------
            months_list: list[date] = []
            cursor = start
            while cursor <= end:
                months_list.append(cursor)
                nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
                cursor = nxt

            opco_rows: list[tuple] = []
            traits_rows: list[tuple] = []
            bridge_rows: list[tuple] = []

            for month in months_list:
                for ck in range(1, customers + 1):
                    home = ref.opcos[ck % len(ref.opcos)]
                    is_member = rng.random() < MEMBER_SHARE
                    active = rng.random() < 0.78

                    generation = rng.choice(GENERATIONS)
                    low, high = GENERATION_AGE_RANGE.get(generation, (0, 0))
                    age = rng.randint(low, high) if high else None
                    # One person, so the demographics must agree across the two
                    # tables. Drawn independently, the group view disagreed with the
                    # OpCo view about the same customer's gender in roughly a third
                    # of rows, and two questions that should match did not.
                    gender = rng.choice(GENDERS)
                    family = rng.choice(FAMILY_SEGMENTS)
                    lifecycle = rng.choice(LIFECYCLES)

                    # Per-customer constants, drawn once so every OpCo row for this
                    # customer agrees -- the same reason gender is drawn here.
                    residency = rng.choices(RESIDENCY, weights=(80, 15, 5))[0]
                    affinity = sorted(
                        rng.sample(AFFINITY_TAGS, rng.choices((0, 1, 2), weights=(45, 40, 15))[0])
                    )
                    first_txn = month - timedelta(days=rng.randint(30, 2200))
                    lifetime_txns = rng.randint(1, 900)

                    # Cards, loans and insurance are issued by NorthCo Credit only, so
                    # every other OpCo's row leaves these NULL.
                    home_stores = (ref.stores.get(home) or [None])[:STORE_CONCENTRATION]
                    primary_store = rng.choice(home_stores)

                    # No prior-period state. It used to be generated here, because
                    # the prior-period COLUMNS existed and left NULL they answered
                    # every migration question -- "how many upgraded to 4 Star",
                    # "how many moved into Declining" -- with a confident zero.
                    # Those columns are not in the schema: a transition is
                    # now a self-join across two month_start_date values, which the
                    # generator satisfies simply by seeding several months. The
                    # churn rate that used to be manufactured here is whatever an
                    # independent draw per month produces, which is higher than the
                    # 18% the old CHANGE_RATE simulated -- if a migration question
                    # needs a realistic rate, carry each customer's state forward
                    # across the month loop rather than reinstating the columns.
                    tier = rng.choice(TIERS) if is_member else None

                    # Which OpCos this customer belongs to. Built BEFORE the rows,
                    # because a customer who holds NorthCo Credit needs an NorthCo Credit
                    # ROW to hold it on.
                    #
                    # They did not get one. Every customer had exactly one opco row,
                    # for their home OpCo, while the group table's opco_codes array
                    # said they belonged to two. So 791 customers were NorthCo Credit
                    # customers according to the overlap table and 500 according to
                    # the fact -- and every cross-OpCo question, which is most of
                    # what this schema exists for, was answered from the smaller set.
                    # "NorthCo Mart appliance spend of customers who use NorthCo Credit
                    # financing" returned nothing at all: no NorthCo Mart customer had
                    # anywhere to carry a loan.
                    codes = [home]
                    if (
                        HOLDING_OPCO in ref.opcos
                        and home != HOLDING_OPCO
                        and rng.random() < 0.18
                    ):
                        codes.append(HOLDING_OPCO)
                    for other in RETAIL_CROSS_SHOP.get(home, ()):
                        if other in ref.opcos and rng.random() < RETAIL_CROSS_SHOP_RATE:
                            codes.append(other)
                    if (
                        BANK_OPCO in ref.opcos
                        and home != BANK_OPCO
                        and rng.random() < BANK_LINK_RATE
                    ):
                        codes.append(BANK_OPCO)

                    # Holdings are issued by NorthCo Credit, so they sit on that OpCo's
                    # row and are NULL on every other. A customer with no NorthCo Credit
                    # relationship has no row to be NULL on, which is the truthful
                    # shape: absent, not zero.
                    # SORTED before it is stored or iterated. It was in insertion
                    # order -- home OpCo first -- so a customer whose home is
                    # NORTHCO_CREDIT and who also shops NORTHCO_MART got
                    # {NORTHCO_CREDIT,NORTHCO_MART}, while every other producer of this
                    # column uses array_agg(DISTINCT opco_code ORDER BY opco_code).
                    # Membership was right, so containment tests were unaffected and
                    # nothing failed; array EQUALITY and GROUP BY on the array
                    # silently disagreed, on 347 of 2,400 rows in a real seed.
                    codes.sort()

                    if HOLDING_OPCO in codes:
                        cards, _ = _weighted(rng, HOLDING_WEIGHTS["credit_card"])
                        loans, _ = _weighted(rng, HOLDING_WEIGHTS["loan"])
                        policies, _ = _weighted(rng, HOLDING_WEIGHTS["insurance"])
                    else:
                        cards = loans = policies = None

                    # The four columns that used to live on fact_customer_group_monthly.
                    # They describe the customer's month, not the OpCo row, so they
                    # are CONSTANT across the rows below -- which is exactly why the
                    # schema can carry them here and why PER_CUSTOMER_CONSTANT_COLUMNS
                    # forbids summing them across rows.
                    #
                    # is_primary_opco goes with them, and is NOT constant: exactly one
                    # of the customer's rows is their home OpCo. It was never set at
                    # all, so it defaulted to false on every row -- every "primary
                    # OpCo" question answered zero, and it would have contradicted
                    # primary_opco_code the moment that column arrived.
                    multi_opco = len(codes) > 1

                    # PRODUCT HOLDING IS A PROPERTY OF THE CUSTOMER, NOT OF THE ROW.
                    # Computed here, once, from the UNMASKED holdings, so both of a
                    # two-OpCo customer's rows carry the same answer.
                    #
                    # The counts themselves stay per-row and stay NULL outside NorthCo
                    # Credit -- "this OpCo cannot say" -- but the derived bucket must
                    # not. Computed inside the row loop it read the masked NULLs, so
                    # one customer was a Cross-Holder on their NorthCo Credit row and No
                    # Financial Product on their NorthCo row, and "how many
                    # cross-holders" depended on which row you happened to count.
                    has_bank = "NORTHCO_BANK" in codes
                    has_acs_card = bool(cards)
                    holding = _product_holding(cards, loans, has_bank)
                    combo = _cross_holder_combo(cards, loans, has_bank)
                    eligibility = _eligibility(holding, residency)

                    cust_txns = 0
                    cust_revenue = 0.0
                    cust_northvalu = 0

                    for code in codes:
                        holds = code == HOLDING_OPCO

                        # transaction_count is gated on `active` because the
                        # glossary defines is_active_in_opco as "the customer
                        # TRANSACTED with this OpCo in the month". It used to be
                        # rng.randint(1, 40) unconditionally, so 22% of rows said
                        # Inactive while carrying up to forty transactions -- and
                        # the trait buckets, which band this number, would have
                        # inherited that contradiction and published it as a
                        # segment.
                        # Skewed, not uniform, and the reason is the buckets these
                        # feed. rng.randint(1, 40) put 66% of customers in Heavy
                        # (>= 8 trips), and uniform(18, 190) per basket made Premium
                        # Basket (> RM450) UNREACHABLE -- a "Premium Basket" audience
                        # returned a confident zero that belonged to the generator
                        # rather than to the business, which is the exact failure
                        # this file exists to avoid.
                        #
                        # Triangular with a low mode and a long tail gives a retail
                        # shape: most members shop a handful of times for a middling
                        # basket, a few shop constantly, and every bucket including
                        # the extremes is reachable.
                        # These are PER-OPCO draws, but visit_frequency_bucket and
                        # basket_size_bucket band the customer's TOTAL across OpCos --
                        # so the per-OpCo mode has to sit well below the Heavy
                        # threshold or every multi-OpCo customer clears it by
                        # arithmetic. A mode of 2.5 still produced 54% Heavy.
                        txns = max(1, round(rng.triangular(1, 22, 1.4))) if active else 0
                        revenue = round(txns * rng.triangular(5, 600, 28), 2)

                        # Baskets carrying a NorthValu line, bounded by txns so the
                        # adoption share can never exceed 1. Zero for a third of
                        # customers, which is what makes "Never Buy" reachable.
                        northvalu_txns = (
                            0 if rng.random() < 0.33
                            else rng.randint(1, txns) if txns else 0
                        )

                        # Per-OPCO: which rail they used most AT THIS OpCo. The card
                        # side of preferred_payment_segment is per-customer and comes
                        # from has_acs_card above, which is what makes "holds our card
                        # but pays another way HERE" expressible per OpCo.
                        payment_method, _ = _weighted(rng, PAYMENTS)

                        opco_rows.append((
                            ck, month, code, is_member,
                            "Member" if is_member else "Non-Member",
                            active, "Active" if active else "Inactive",
                            "Active" if is_member and active else ("Inactive" if is_member else None),
                            code == home,
                            primary_store if code == home else rng.choice(
                                (ref.stores.get(code) or [None])[:STORE_CONCENTRATION]
                            ),
                            txns,
                            revenue,
                            northvalu_txns,
                            min(txns, rng.randint(1, 28)) if txns else 0,
                        ))

                        # Accumulate for the trait row. The six dimensions are
                        # CROSS-OPCO -- a Heavy shopper made eight trips with NorthCo,
                        # not eight at one OpCo -- so they are banded once, below,
                        # from the customer's totals rather than per row.
                        cust_txns += txns
                        cust_revenue += revenue
                        cust_northvalu += northvalu_txns

                    # ONE trait row per customer per month, after every OpCo row.
                    traits_rows.append((
                        ck, month,
                        age, gender, generation, residency,
                        tier,
                        rng.choice(TENURE_BUCKETS) if is_member else None,
                        lifecycle, lifecycle in ("Declining", "Churned"),
                        first_txn, lifetime_txns,
                        _visit_frequency(cust_txns),
                        _basket_size(cust_txns, cust_revenue),
                        _northvalu_adoption(cust_txns, cust_northvalu),
                        family,
                        holding,
                        _payment_segment(has_acs_card, payment_method),
                        payment_method,
                        affinity,
                        cards, loans, policies,
                        has_bank, combo, eligibility,
                        codes, len(codes), multi_opco, home,
                    ))

                    # Leaf categories the customer bought this month, at their own
                    # store. Leaf grain with the ancestors denormalised is what lets
                    # a question name any level -- "Home Fashion" or the narrower
                    # "Fashion Accessories" -- and get an exact answer rather than a
                    # roll-up to whatever depth the bridge happened to store.
                    for l1, l2 in rng.sample(
                        ref.l1l2_by_opco.get(home, []) or [(0, 0)],
                        min(CATEGORIES_PER_CUSTOMER, len(ref.l1l2_by_opco.get(home, []) or [1])),
                    ):
                        # The customer's own store, so the bridge agrees with
                        # primary_store_id on the same row rather than describing a
                        # branch the customer is not otherwise associated with.
                        store = primary_store or 0
                        leaves = ref.leaf_by_l2.get((home, l1, l2 or 0)) or []
                        chosen = (
                            rng.sample(leaves, min(2, len(leaves)))
                            if leaves
                            else [(l2 or l1, l1, (l2 or None), None, None, 2 if l2 else 1)]
                        )
                        for leaf, a1, a2, a3, a4, level in chosen:
                            txn = rng.randint(1, 9)
                            bridge_rows.append((
                                ck, month, home, leaf, level,
                                a1, a2, a3, a4, store,
                                txn,
                                round(txn * rng.uniform(1.4, 2.6), 3),
                                round(txn * rng.uniform(28, 140), 2),
                            ))

            cur.executemany(
                f"""INSERT INTO {CORE}.fact_customer_opco_monthly
                    (customer_key, month_start_date, opco_code, is_member, customer_type,
                     is_active_in_opco, customer_status_in_opco, member_status,
                     is_primary_opco, primary_store_id,
                     transaction_count, total_revenue, northvalu_transaction_count,
                     active_days_in_month)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                opco_rows,
            )
            cur.executemany(
                f"""INSERT INTO {CORE}.fact_customer_traits_monthly
                    (customer_key, month_start_date,
                     age, gender_bucket, generation_bucket, residency_status,
                     membership_tier, tenure_bucket,
                     lifecycle_stage, is_at_risk_customer,
                     first_transaction_date, lifetime_transaction_count,
                     visit_frequency_bucket, basket_size_bucket,
                     northvalu_adoption_segment, family_segment,
                     financial_product_holding_bucket, preferred_payment_segment,
                     primary_payment_type, secondary_affinity_tags,
                     active_credit_card_count, active_loan_count,
                     active_insurance_count, has_northco_bank, cross_holder_combo,
                     no_product_eligibility_status,
                     active_opco_codes, active_opco_count,
                     is_multi_opco_customer, primary_opco_code)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                traits_rows,
            )
            cur.executemany(
                f"""INSERT INTO {CORE}.bridge_customer_category_monthly
                    (customer_key, month_start_date, opco_code, category_key,
                     category_level, category_l1_key, category_l2_key,
                     category_l3_key, category_l4_key,
                     store_id, transaction_count, quantity, gross_sales_amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                bridge_rows,
            )

            # agg_sales_store_monthly was populated here, by the same GROUP BY the
            # planner now reads through v_sales_store_monthly. The table was dropped
            # because a ~2.5x rollup did not earn 20M rows a
            # year, so there is nothing left to seed: the view aggregates
            # fact_sales_daily on demand, which this script has already filled.
            #
            # dim_product is REFERENCE data and lives in
            # app/scripts/load_reference_data.py, not here.
            #
            # They were briefly generated in this script and every `docker compose up`
            # silently undid it: the reference loader truncates dim_product_category
            # CASCADE, dim_product has a foreign key to it, so the SKUs vanished and
            # agg_sales_sku_monthly was left referencing 63,192 rows that no longer
            # existed. A dimension belongs to the loader that owns its parent; this
            # script generates facts.

            # agg_sales_sku_monthly -- monthly SKU sales, allocated from the atomic
            # fact so the SKU total for a category matches that category's total.
            # v_sales_sku_monthly is advertised to the planner, so leaving it empty
            # made every SKU question answer 0.
            cur.execute(
                f"""
                INSERT INTO {CORE}.agg_sales_sku_monthly (
                    month_start_date, opco_code, store_id, sku_key, brand_name,
                    category_key, category_l1_key, category_l2_key, category_l3_key,
                    category_l4_key, customer_type, transaction_count, quantity,
                    gross_sales_amount
                )
                SELECT m.month_start_date, m.opco_code, m.store_id, p.sku_key,
                       p.brand_name, m.category_key,
                       m.category_l1_key, m.category_l2_key, m.category_l3_key,
                       m.category_l4_key, m.customer_type,
                       GREATEST(1, (m.transaction_count / p.n_skus)::bigint),
                       ROUND(m.quantity / p.n_skus, 3),
                       ROUND(m.gross_sales_amount / p.n_skus, 2)
                FROM (
                    SELECT date_trunc('month', calendar_date)::date AS month_start_date,
                           opco_code, store_id, category_key, category_l1_key,
                           category_l2_key, category_l3_key, category_l4_key,
                           customer_type,
                           SUM(transaction_count) AS transaction_count,
                           SUM(quantity) AS quantity,
                           SUM(gross_sales_amount) AS gross_sales_amount
                    FROM {CORE}.fact_sales_daily
                    GROUP BY 1,2,3,4,5,6,7,8,9
                ) m
                JOIN (
                    SELECT sku_key, opco_code, category_key, brand_name,
                           COUNT(*) OVER (PARTITION BY opco_code, category_key) AS n_skus
                    FROM {CORE}.dim_product
                ) p ON p.opco_code = m.opco_code AND p.category_key = m.category_key
                ON CONFLICT DO NOTHING
                """
            )
            # Report what LANDED, not what was attempted. Five of these six counts
            # used to be len(rows), and ON CONFLICT DO NOTHING drops duplicate keys
            # silently -- so the summary said 27,513 while the table held 27,512. A
            # seeding report that overstates by a row or two is a seeding report
            # that cannot be used to check the seeding.
            loaded: dict[str, int] = {}
            for table in (
                "agg_sales_daily",
                "fact_sales_daily",
                "fact_customer_opco_monthly",
                "fact_customer_traits_monthly",
                "bridge_customer_category_monthly",
                "agg_sales_sku_monthly",
            ):
                cur.execute(f"SELECT count(*) FROM {CORE}.{table}")
                loaded[table] = cur.fetchone()[0]

            attempted = {
                "agg_sales_daily": agg_rows,
                "fact_sales_daily": atomic_rows,
                "fact_customer_opco_monthly": len(opco_rows),
                "fact_customer_traits_monthly": len(traits_rows),
                "bridge_customer_category_monthly": len(bridge_rows),
            }


    _analyze(
        tuple(
            f"{CORE}.{table}"
            for table in (
                "agg_sales_daily", "fact_sales_daily",
                "fact_customer_opco_monthly",
                "fact_customer_traits_monthly",
                "bridge_customer_category_monthly",
                "agg_sales_sku_monthly",
            )
        )
    )

    print(f"period                           : {start} .. {end} ({months} months)")
    for table, count in loaded.items():
        skipped = attempted.get(table, count) - count
        note = f"  ({skipped:,} duplicate key(s) skipped)" if skipped > 0 else ""
        print(f"{table:<33}: {count:,}{note}")


def _analyze(tables: tuple[str, ...]) -> None:
    """Refresh planner statistics as the table OWNER, on a fresh connection.

    ANALYZE requires ownership in PostgreSQL 16 -- the MAINTAIN privilege that
    would allow delegating it only arrives in 17. Loading runs as the loader role
    because every scoped table has FORCE ROW LEVEL SECURITY, and that role owns
    nothing, so ANALYZE issued inside the load transaction was skipped for all 160
    tables and partitions:

        WARNING: permission denied to analyze "agg_sales_daily_p202606", skipping it

    Harmless in the sense that the rows still loaded, but it left the whole
    serving schema with no statistics -- so the planner sized every scan off
    defaults.

    Run on a separate connection once the load has committed: the login user owns
    the tables and has no role set, and the rows being analysed are visible to
    everyone rather than sitting in an open transaction.
    """
    with get_pg_conn() as analyze_conn:
        with analyze_conn.cursor() as cur:
            skipped: list[str] = []
            for table in tables:
                try:
                    cur.execute(f"ANALYZE {table}")
                except Exception as exc:  # noqa: BLE001
                    skipped.append(f"{table} ({exc.__class__.__name__})")
            if skipped:
                print(
                    "WARNING: could not ANALYZE "
                    + ", ".join(skipped)
                    + " -- the login user must own these tables. Statistics are stale."
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--customers", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20260727)
    parser.add_argument(
        "--anchor",
        default=None,
        help=(
            "Data ends the day BEFORE this date. Omit to end yesterday, so the "
            "month in progress is present and questions meaning 'now' have "
            "something to answer with."
        ),
    )
    args = parser.parse_args()
    generate(
        args.months,
        args.customers,
        args.seed,
        date.fromisoformat(args.anchor) if args.anchor else None,
    )


if __name__ == "__main__":
    main()
