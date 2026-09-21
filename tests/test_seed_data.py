"""Guard the shape of the seeded mock data.

The seeder is the only thing standing between "the copilot answers correctly" and
"the copilot answers zero convincingly". A silent gap in it looks exactly like a
business fact: `fact_sales_daily` was loaded with `category_l3_key` and
`category_l4_key` NULL in all 27,513 rows, so any question resolving to a level-3
or level-4 category filtered a column that was empty everywhere and got 0 --
reported as "nobody bought lamb" rather than "this data cannot answer that".

RLS masked how bad it was: its predicate also ORs on `category_key`, and a grant
expands to include descendants, so category grants below level 2 still returned
rows. Only the planner's own `category_lN_key` filters broke, which is the path
no test covered.

Reads through `ci_loader`, not the login user. Every scoped table has FORCE ROW
LEVEL SECURITY, which applies to the owner too, so a plain SELECT as `app`
returns zero rows and every assertion here would pass against an empty result
set.

Requires a live, seeded database. Skips (does not fail) when one is unreachable,
so a unit-test run on a machine with no Postgres stays green. To point it at a
published container port:

    POSTGRES_URI=postgresql://app:app@localhost:5432/customer_intelligence \\
        pytest tests/test_seed_data.py
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core import settings
from app.db.v3_ddl import CATEGORY_DEPTH

CORE = settings.ci_core_schema


@pytest.fixture(scope="module")
def conn():
    url = settings.postgres_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    try:
        engine = sa.create_engine(url, connect_args={"connect_timeout": 3})
        connection = engine.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database reachable for seed check: {exc}")

    # The login user holds no grants of its own -- the loader role is the one that
    # can read and write the raw seeded rows. The policies are gone; the privilege
    # split is not.
    connection.execute(sa.text(f'SET ROLE "{settings.ci_loader_role}"'))
    try:
        yield connection
    finally:
        connection.close()


def _scalar(conn, sql: str):
    return conn.execute(sa.text(sql)).scalar()


@pytest.fixture(scope="module")
def seeded(conn) -> None:
    if not _scalar(conn, f"SELECT count(*) FROM {CORE}.fact_sales_daily"):
        pytest.skip(
            "fact_sales_daily is empty -- run `docker compose --profile seed run "
            "--rm mock-data` first."
        )


def test_atomic_fact_reaches_its_declared_category_depth(conn, seeded) -> None:
    """fact_sales_daily is declared depth 4; every level must actually be present.

    The declaration lives in CATEGORY_DEPTH and drives planner table routing
    and PrincipalContext.tables_satisfying_grant. Data shallower than the
    declaration means the router sends a deep question to a table that cannot
    answer it.
    """
    assert CATEGORY_DEPTH["fact_sales_daily"] == 4

    populated = conn.execute(
        sa.text(
            f"""
            SELECT count(category_l1_key) AS l1, count(category_l2_key) AS l2,
                   count(category_l3_key) AS l3, count(category_l4_key) AS l4,
                   count(*) AS total
            FROM {CORE}.fact_sales_daily
            """
        )
    ).mappings().one()

    # Rows whose leaf sits at level 4 must carry all four keys. In this dataset
    # every leaf is level 4, so "some populated" is not good enough -- assert the
    # count of level-4 rows matches.
    deep_rows = _scalar(
        conn,
        f"""
        SELECT count(*) FROM {CORE}.fact_sales_daily f
        JOIN {CORE}.dim_product_category d ON d.category_key = f.category_key
        WHERE d.category_level = 4
        """,
    )
    assert populated["l3"] >= deep_rows, (
        f"{deep_rows - populated['l3']} rows sit at category level 4 but have "
        "category_l3_key NULL. The seeder is not loading the full ancestor chain."
    )
    assert populated["l4"] >= deep_rows, (
        f"{deep_rows - populated['l4']} rows sit at category level 4 but have "
        "category_l4_key NULL."
    )


def test_fact_category_chain_matches_the_dimension(conn, seeded) -> None:
    """A denormalised chain that disagrees with the dimension is worse than none.

    Every cross-check would look like an application bug.
    """
    mismatched = _scalar(
        conn,
        f"""
        SELECT count(*)
        FROM {CORE}.fact_sales_daily f
        JOIN {CORE}.dim_product_category d ON d.category_key = f.category_key
        WHERE f.category_l1_key IS DISTINCT FROM d.l1_key
           OR f.category_l2_key IS DISTINCT FROM d.l2_key
           OR f.category_l3_key IS DISTINCT FROM d.l3_key
           OR f.category_l4_key IS DISTINCT FROM d.l4_key
        """,
    )
    assert mismatched == 0, (
        f"{mismatched} fact rows carry an ancestor chain that disagrees with "
        "dim_product_category."
    )


def test_shallow_branches_stay_null_rather_than_zero(conn, seeded) -> None:
    """A missing ancestor is NULL, never 0.

    The old loader ran l2 through `COALESCE(l2_key, 0)` and back through
    `(l2 or None)`. Zero is not a category key, so any row that kept it would
    match nothing and silently drop out of every category filter.
    """
    zero_keys = _scalar(
        conn,
        f"""
        SELECT count(*) FROM {CORE}.fact_sales_daily
        WHERE 0 IN (category_l1_key, category_l2_key, category_l3_key, category_l4_key)
        """,
    )
    assert zero_keys == 0, f"{zero_keys} fact rows use 0 as a category key."


def test_a_level_four_filter_returns_rows(conn, seeded) -> None:
    """The failure mode in business terms: filtering a deep category finds data.

    Coverage matters as much as the columns being populated. Random sampling used
    to leave ~500 of 4,855 leaves with no sales anywhere in the period, so a
    question about one of them returned zero for a reason no user could guess.
    """
    leaves_in_dim = _scalar(
        conn, f"SELECT count(*) FROM {CORE}.dim_product_category WHERE is_leaf"
    )
    leaves_with_sales = _scalar(
        conn, f"SELECT count(DISTINCT category_key) FROM {CORE}.fact_sales_daily"
    )
    assert leaves_with_sales == leaves_in_dim, (
        f"{leaves_in_dim - leaves_with_sales} leaf categories have no sales in the "
        "seeded period; a filter on any of them returns an unexplainable zero."
    )

    # And the deep key itself is queryable, not just present.
    sample_l4 = _scalar(
        conn,
        f"""
        SELECT category_l4_key FROM {CORE}.fact_sales_daily
        WHERE category_l4_key IS NOT NULL LIMIT 1
        """,
    )
    assert _scalar(
        conn,
        f"SELECT count(*) FROM {CORE}.fact_sales_daily "
        f"WHERE category_l4_key = {int(sample_l4)}",
    ) > 0




def test_every_l1_division_reaches_the_daily_summary(conn, seeded) -> None:
    """A category grant is answered from agg_sales_daily, so every division must be there.

    The seeder took only the first 12 (l1, l2) pairs per OpCo. NORTHCO has 48, so
    5 of its 7 divisions never appeared in this table -- and it is the table the
    planner prefers for summary questions. A caller granted HARD got a confident
    "0 transactions" for a division with rows in the atomic fact.
    """
    missing = conn.execute(
        sa.text(
            f"""
            SELECT d.opco_code, d.category_key, d.category_name
            FROM {CORE}.dim_product_category d
            WHERE d.category_level = 1
              AND EXISTS (SELECT 1 FROM {CORE}.agg_sales_daily a WHERE a.opco_code = d.opco_code)
              AND NOT EXISTS (
                  SELECT 1 FROM {CORE}.agg_sales_daily a
                  WHERE a.opco_code = d.opco_code AND a.category_l1_key = d.category_key
              )
            ORDER BY 1, 3
            """
        )
    ).fetchall()
    assert not missing, (
        f"{len(missing)} level-1 division(s) have no rows in agg_sales_daily: "
        f"{[(r[0], r[2]) for r in missing[:8]]}. A category grant on one of them "
        "returns zero for a division that does have sales."
    )


def test_every_store_has_sales(conn, seeded) -> None:
    """A store the resolver can match must have rows, or the answer is a false zero."""
    missing = _scalar(
        conn,
        f"""
        SELECT count(*) FROM {CORE}.dim_store s
        WHERE NOT EXISTS (
            SELECT 1 FROM {CORE}.fact_sales_daily f
            WHERE f.opco_code = s.opco_code AND f.store_id = s.store_id
        )
        """,
    )
    assert missing == 0, (
        f"{missing} store(s) in dim_store have no fact rows; the resolver will match "
        "them by name and the query will return an unexplainable zero."
    )




def test_enum_vocabulary_is_loaded(conn, seeded) -> None:
    """Values of low-cardinality columns must be in the catalog as `enum`.

    Without them, a word that IS a real value has no exact hit and falls through to
    fuzzy matching against product names: "Elite, Premium, Growth, Mass, At Risk"
    -- five values of value_segment -- resolved to CORPORATE ELITE, PREMIUM DESSERT
    and FRESHMART PLAZA S12. HOLLISBURN. The dictionary has to hold the whole schema
    vocabulary, or the fuzzy pass gets asked questions it was never meant to answer.

    value_segment itself is no longer in the schema, so the assertion
    moved to lifecycle_stage, which is the same shape of risk and worse: "New",
    "Loyal" and "Declining" are ordinary English words that appear in hundreds of
    product names.
    """
    META = settings.ci_meta_schema
    stages = conn.execute(
        sa.text(
            f"""
            SELECT raw_value FROM {META}.copilot_lookup_value
            WHERE entity_class = 'enum' AND source_column = 'lifecycle_stage'
            ORDER BY raw_value
            """
        )
    ).scalars().all()
    assert set(stages) >= {
        "New", "Activated", "Engaged", "Loyal", "Declining", "Churned",
    }, f"lifecycle_stage vocabulary is incomplete: {stages}"


def test_enum_rows_are_global_and_do_not_shadow_entities(conn, seeded) -> None:
    """Three rules, each with a regression behind it."""
    META = settings.ci_meta_schema
    CORE_S = settings.ci_core_schema

    # Global vocabulary: "Elite" means the same in every OpCo, and the RLS policy
    # admits NULL, so it must not be tenant-scoped.
    scoped = conn.execute(
        sa.text(
            f"SELECT count(*) FROM {META}.copilot_lookup_value "
            "WHERE entity_class='enum' AND (opco_code IS NOT NULL OR category_key IS NOT NULL)"
        )
    ).scalar()
    assert scoped == 0, f"{scoped} enum rows are tenant-scoped; they are global vocabulary"

    # Keys and counts are not vocabulary. Loading opco_code made "northco co" resolve
    # as an enum instead of an OpCo and broke the overlap question; loading
    # daypart_seq made "3" a filter value in "between 3 and 22 june".
    junk = conn.execute(
        sa.text(
            f"""
            SELECT source_column, raw_value FROM {META}.copilot_lookup_value
            WHERE entity_class = 'enum'
              AND (normalized_value ~ '^[0-9 ]+$'
                   OR source_column IN ('opco_code','opco_name','primary_opco_code'))
            """
        )
    ).fetchall()
    assert not junk, f"key/count columns loaded as vocabulary: {junk}"

    # An enum must not shadow a store or category that already carries that name.
    shadowed = conn.execute(
        sa.text(
            f"""
            SELECT e.raw_value
            FROM {META}.copilot_lookup_value e
            JOIN {META}.copilot_lookup_value n
              ON n.normalized_value = e.normalized_value AND n.entity_class <> 'enum'
            WHERE e.entity_class = 'enum'
            """
        )
    ).scalars().all()
    assert not shadowed, f"enum values shadow existing entity names: {sorted(set(shadowed))}"


def test_retail_to_retail_overlap_exists(conn, seeded) -> None:
    """Every cross-OpCo pair a group user might ask about must be answerable.

    The generator only ever paired an OpCo with NORTHCO_CREDIT, so
    "customers who transact with NorthCo and also NorthCo Mart" returned a confident 0
    -- a property of the generator, not of the business -- and the answer explained
    it as "customers tend to be loyal to one OpCo".

    Reads active_opco_codes on fact_customer_opco_monthly. This was
    fact_customer_group_monthly, which was retired: those four columns moved onto
    the OpCo grain, so the same property has to hold there or the generator is not
    populating them consistently.
    """
    CORE_S = settings.ci_core_schema
    pairs = conn.execute(
        sa.text(
            f"""
            SELECT count(DISTINCT customer_key)
            FROM {CORE_S}.fact_customer_opco_monthly
            WHERE opco_code = 'NORTHCO'
              AND active_opco_codes @> ARRAY['NORTHCO_MART']
            """
        )
    ).scalar()
    assert pairs > 0, (
        "no customer holds both retail banners; a retail-to-retail overlap question "
        "would return a zero that belongs to the seeder rather than the business."
    )

    credit = conn.execute(
        sa.text(
            f"""
            SELECT count(DISTINCT customer_key)
            FROM {CORE_S}.fact_customer_opco_monthly
            WHERE opco_code = 'NORTHCO_CREDIT' AND is_multi_opco_customer
            """
        )
    ).scalar()
    assert credit > 0, "the NORTHCO_CREDIT overlap case disappeared"


def test_no_table_the_planner_can_query_is_empty(conn, seeded) -> None:
    """Every advertised view and joinable dimension must have rows.

    An empty table the planner is told about is the worst kind of gap: the query is
    valid, the answer is 0, and nothing distinguishes "no such activity" from "this
    was never seeded". `v_sales_sku_monthly` was advertised with agg_sales_sku_monthly
    empty, so every SKU question answered 0.

    v_sales_store_monthly is skipped: PERSONA_VIEWS maps it to fact_sales_daily,
    which this loop already checks. It aggregates that table rather than mirroring
    one of its own, so there is no separate object to find empty.
    """
    from app.db.v3_ddl import PERSONA_VIEWS
    from app.service.table_registry import ALLOWED_VIEWS, JOINABLE_DIMENSIONS

    CORE_S = settings.ci_core_schema
    targets = {PERSONA_VIEWS[v] for v in ALLOWED_VIEWS if v in PERSONA_VIEWS}
    targets |= set(JOINABLE_DIMENSIONS)

    empty = []
    for table in sorted(targets):
        count = _scalar(conn, f"SELECT count(*) FROM {CORE_S}.{table}")
        if not count:
            empty.append(table)

    assert not empty, (
        f"{len(empty)} table(s) the planner can query are empty: {empty}. "
        "A question routed there answers 0 for a reason no user could guess."
    )


def test_sku_sales_reconcile_with_the_atomic_fact(conn, seeded) -> None:
    """agg_sales_sku_monthly is allocated from fact_sales_daily, so it must tie out.

    Allowed to differ only by per-SKU rounding of the allocation.
    """
    CORE_S = settings.ci_core_schema
    sku = _scalar(conn, f"SELECT COALESCE(sum(gross_sales_amount),0) FROM {CORE_S}.agg_sales_sku_monthly")
    fact = _scalar(conn, f"SELECT COALESCE(sum(gross_sales_amount),0) FROM {CORE_S}.fact_sales_daily")
    assert fact > 0
    drift = abs(float(sku) - float(fact)) / float(fact)
    assert drift < 0.001, (
        f"SKU sales total {sku} differs from the atomic fact {fact} by {drift:.4%}; "
        "the allocation should only lose per-SKU rounding."
    )




def test_category_names_are_ambiguous_but_paths_are_unique(conn, seeded) -> None:
    """Why the permission block resolves paths, not bare names.

    category_keys are surrogates this loader assigns, so no calling system knows that
    1912 means BEAUTY -- a permission payload written by hand or by an IdP carries
    names. But a name does not identify a node: "OTHERS" is 63 separate nodes across
    all four levels of NorthCo. Paths do identify one, which is why an ambiguous name
    is rejected with its candidates rather than resolved by guessing.
    """
    CORE_S = settings.ci_core_schema
    row = conn.execute(
        sa.text(
            f"""
            SELECT count(*) AS nodes,
                   count(DISTINCT category_name) AS names,
                   count(DISTINCT category_path_text) AS paths
            FROM {CORE_S}.dim_product_category WHERE opco_code = 'NORTHCO'
            """
        )
    ).mappings().one()

    assert row["paths"] == row["nodes"], (
        "category_path_text is not unique, so a path cannot identify a grant"
    )
    assert row["names"] < row["nodes"], (
        "names happen to be unique in this dataset; the ambiguity rule is still "
        "correct, but this test no longer demonstrates why"
    )

    worst = conn.execute(
        sa.text(
            f"""
            SELECT category_name, count(*) AS n
            FROM {CORE_S}.dim_product_category WHERE opco_code = 'NORTHCO'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 1
            """
        )
    ).mappings().one()
    assert worst["n"] > 1, f"expected a duplicated name, got {worst}"
