"""The mock-data generator has to agree with the schema it writes into.

It broke silently and stayed broken: agg_sales_store_monthly and
fact_customer_group_monthly were retired, and the generator kept inserting into
both. Nothing caught it, because the only tests that touch seeded
data need a live PostgreSQL and skip without one -- so `python scripts/generate_mock_data.py`
failed at the first INSERT, on a developer machine, at the moment someone needed data.

Everything here runs against a fake cursor, so it runs in the default `pytest`
sweep with no database. What it checks is the join between the two things that
drifted: the statements the generator emits, and the CREATE TABLE statements the
migrations actually apply.
"""
from __future__ import annotations

import collections
import contextlib
import re
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The DDL of record. Read from the migrations rather than from a live database so
# this test says "the generator disagrees with the migrations", which is the
# actionable form of the failure.
#
# This reads CREATE TABLE bodies only, which is sound because the schema is
# consolidated: no revision adds or drops a column on a table another revision
# created, so a CREATE body is the whole shape. If that ever stops being true --
# the first ALTER ... ADD COLUMN on one of these tables -- this parse will not see
# it, and the fix is to replay the ALTERs here rather than to trust a green run.
_DDL = "".join(
    (REPO / "alembic" / "versions" / name).read_text()
    for name in (
        "20260727_02_v3_dimensions.py",
        "20260727_03_v3_facts.py",
        "20260727_05_v3_meta.py",
    )
)

# Columns supplied by string macros in the migrations (DAYPART_COLUMNS,
# CUSTOMER_TYPE_CHECK) that a CREATE TABLE parse cannot see.
_MACRO_COLUMNS = frozenset({"daypart", "daypart_seq", "customer_type"})

OPCOS = ["NORTHCO_MART", "NORTHCO", "NORTHCO_CREDIT"]


def _create_table_body(table: str) -> str | None:
    match = re.search(
        rf"CREATE TABLE \{{qualified\((?:CORE|META), '{table}'\)\}} \((.*?)\n        \)",
        _DDL,
        re.S,
    )
    return match.group(1) if match else None


def _columns_of(table: str) -> frozenset[str] | None:
    """Every column the current schema declares for this table, or None if dropped."""
    body = _create_table_body(table)
    if body is None:
        return None

    columns = set(_MACRO_COLUMNS)
    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith(
            ("--", "PRIMARY KEY", "CONSTRAINT", "CHECK", "FOREIGN", "UNIQUE")
        ):
            continue
        token = line.split()[0]
        if re.fullmatch(r"[a-z_][a-z0-9_]*", token):
            columns.add(token)
    return frozenset(columns)


def _not_null_without_default(table: str) -> frozenset[str]:
    """Columns that must receive a value, because nothing will fill them in."""
    body = _create_table_body(table) or ""
    required = set()
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(("--", "CONSTRAINT", "CHECK", "PRIMARY")):
            continue
        if "NOT NULL" not in stripped or "DEFAULT" in stripped:
            continue
        token = stripped.split()[0]
        if re.fullmatch(r"[a-z_][a-z0-9_]*", token):
            required.add(token)
    return frozenset(required)


class _RecordingCursor:
    """Stands in for a psycopg cursor and remembers what was written."""

    def __init__(self, writes: dict[str, tuple[list[str], list[tuple]]]) -> None:
        self._writes = writes
        self._last = ""

    # -- the parts generate_mock_data actually uses ------------------------
    def execute(self, sql: str, params=None) -> None:
        self._last = sql
        self._record(sql, rows=None)

    def executemany(self, sql: str, rows) -> None:
        self._record(sql, rows=list(rows))

    def fetchall(self):
        sql = self._last
        if "dim_opco" in sql:
            return [(opco,) for opco in OPCOS]
        if "dim_store" in sql:
            return [(opco, 1000 + i) for opco in OPCOS for i in range(6)]
        if "dim_product_category" in sql:
            if "DISTINCT" in sql:  # the (opco, l1, l2) rollup pairs
                return [(opco, 100, 200) for opco in OPCOS]
            return [
                (opco, 2000 + i, 4, 100, 200, 300, 400)
                for opco in OPCOS
                for i in range(6)
            ]
        return []

    def fetchone(self):
        return (0,)

    # ----------------------------------------------------------------------
    def _record(self, sql: str, rows) -> None:
        match = re.search(
            r"INSERT INTO\s+(?:\{?\w+\}?\.)?(\w+)\s*\(([^)]*)\)", sql, re.S
        )
        if not match:
            return
        table = match.group(1)
        columns = [c.strip() for c in match.group(2).replace("\n", " ").split(",") if c.strip()]
        self._writes[table] = (columns, rows or [], sql)


class _FakeConnection:
    def __init__(self, writes) -> None:
        self._writes = writes

    def cursor(self):
        return contextlib.nullcontext(_RecordingCursor(self._writes))

    def commit(self) -> None:
        pass


def _run_generator(*, customers: int = 400, months: int = 2):
    """Run the real generation loop, capturing every write."""
    import app.db.postgres as postgres
    import scripts.generate_mock_data as generator

    writes: dict[str, tuple] = {}
    fake = lambda *a, **k: contextlib.nullcontext(_FakeConnection(writes))  # noqa: E731

    original_conn = generator.get_pg_conn
    original_analyze = generator._analyze
    original_pg = postgres.get_pg_conn
    try:
        generator.get_pg_conn = fake
        postgres.get_pg_conn = fake
        generator._analyze = lambda *a, **k: None
        generator.generate(
            months=months, customers=customers, seed=7, anchor=date(2026, 6, 30)
        )
    finally:
        generator.get_pg_conn = original_conn
        postgres.get_pg_conn = original_pg
        generator._analyze = original_analyze
    return writes


class GeneratorSchemaAgreementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.writes = _run_generator()

    def test_it_writes_something(self) -> None:
        self.assertTrue(self.writes, "the generator wrote no tables at all")

    def test_every_table_it_writes_still_exists(self) -> None:
        """The exact failure that went unnoticed: INSERT into a dropped table."""
        for table in self.writes:
            with self.subTest(table=table):
                self.assertIsNotNone(
                    _columns_of(table),
                    f"{table} is not created by any migration -- it was dropped, and "
                    "the generator still writes to it",
                )

    def test_every_column_it_names_still_exists(self) -> None:
        for table, (columns, _rows, _sql) in self.writes.items():
            declared = _columns_of(table)
            if declared is None:
                continue  # reported by the test above
            with self.subTest(table=table):
                self.assertEqual(
                    set(), set(columns) - declared,
                    f"{table}: columns the schema does not have",
                )

    def test_placeholders_match_the_tuples(self) -> None:
        for table, (columns, rows, sql) in self.writes.items():
            placeholders = sql.count("%s")
            if not rows or not placeholders:
                continue
            with self.subTest(table=table):
                self.assertEqual(placeholders, len(columns))
                self.assertEqual(placeholders, len(rows[0]))

    def test_not_null_columns_receive_a_value(self) -> None:
        """A NOT NULL column with no default has to be sent, not defaulted."""
        for table, (columns, rows, _sql) in self.writes.items():
            if not rows or _columns_of(table) is None:
                continue
            for column in _not_null_without_default(table) & set(columns):
                index = columns.index(column)
                with self.subTest(table=table, column=column):
                    self.assertFalse(
                        any(row[index] is None for row in rows[:500]),
                        f"{table}.{column} is NOT NULL but the generator sent None",
                    )


class OverlapColumnTests(unittest.TestCase):
    """The four cross-OpCo columns, now that they live on a different table.

    They were on the OpCo grain, repeated on each of a customer's rows. They are
    now on fact_customer_traits_monthly, one row per customer per month -- so what
    used to be a within-row consistency check is a CROSS-TABLE one: the array on
    the trait row has to match the OpCo rows that actually exist.

    That is the check worth having. A customer-grain table alongside an OpCo-grain
    table is the exact shape that drifted last time this schema had one -- 791
    customers were NorthCo Credit customers according to one table and 500 according
    to the other -- and this is the assertion that would have caught it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        writes = _run_generator(customers=400)
        opco_cols, opco_rows, _ = writes["fact_customer_opco_monthly"]
        trait_cols, trait_rows, _ = writes["fact_customer_traits_monthly"]
        cls.o = {name: i for i, name in enumerate(opco_cols)}
        cls.t = {name: i for i, name in enumerate(trait_cols)}
        cls.trait_rows = trait_rows

        cls.opco_by_customer_month = collections.defaultdict(list)
        for row in opco_rows:
            cls.opco_by_customer_month[
                (row[cls.o["customer_key"]], row[cls.o["month_start_date"]])
            ].append(row)

        cls.trait_by_customer_month = {}
        for row in trait_rows:
            cls.trait_by_customer_month[
                (row[cls.t["customer_key"]], row[cls.t["month_start_date"]])
            ] = row

    def test_one_trait_row_per_customer_month(self) -> None:
        """The grain claim itself: COUNT(*) is a customer count only if this holds."""
        self.assertEqual(
            len(self.trait_rows),
            len(self.trait_by_customer_month),
            "a customer-month appears twice on fact_customer_traits_monthly",
        )
        self.assertEqual(
            set(self.trait_by_customer_month),
            set(self.opco_by_customer_month),
            "the two tables do not describe the same customer-months",
        )

    def test_the_derived_fields_agree_with_the_array(self) -> None:
        for row in self.trait_rows:
            codes = row[self.t["active_opco_codes"]]
            self.assertEqual(len(codes), row[self.t["active_opco_count"]])
            self.assertEqual(len(codes) > 1, row[self.t["is_multi_opco_customer"]])
            self.assertIn(row[self.t["primary_opco_code"]], codes)

    def test_the_array_matches_the_rows_that_actually_exist(self) -> None:
        """The bug this replaces: the array claimed two OpCos, one row existed.

        A customer who "holds NorthCo Credit" needs an NorthCo Credit ROW to hold it on,
        or every cross-OpCo question is answered from the smaller set. Now that the
        array sits on the other table, this is the join holding.
        """
        for key, group in self.opco_by_customer_month.items():
            declared = self.trait_by_customer_month[key][self.t["active_opco_codes"]]
            actual = sorted(row[self.o["opco_code"]] for row in group)
            # Ordered, not set-compared. This asserted sets, and that is precisely
            # how the array came to be stored in insertion order while every other
            # producer sorts it: membership matched, so the test passed, and a real
            # seed disagreed with array_agg(... ORDER BY opco_code) on 347 rows.
            self.assertEqual(sorted(declared), actual, "membership differs")
            self.assertEqual(list(declared), actual, "array is not sorted")

    def test_exactly_one_row_per_customer_month_is_the_primary_opco(self) -> None:
        for key, group in self.opco_by_customer_month.items():
            primary = [r for r in group if r[self.o["is_primary_opco"]]]
            self.assertEqual(1, len(primary))
            self.assertEqual(
                primary[0][self.o["opco_code"]],
                self.trait_by_customer_month[key][self.t["primary_opco_code"]],
                "is_primary_opco and primary_opco_code disagree across the tables",
            )

    def test_cross_opco_overlap_is_not_trivially_empty(self) -> None:
        """A zero here would belong to the generator, not to the business.

        Retail-to-retail is called out because it was the case that used to be
        missing entirely: NorthCo x NorthCo Mart returned a confident 0 that got
        narrated as "customers tend to be loyal to one OpCo".
        """
        pairs: collections.Counter = collections.Counter()
        for row in self.trait_rows:
            codes = sorted(row[self.t["active_opco_codes"]])
            for i, left in enumerate(codes):
                for right in codes[i + 1:]:
                    pairs[(left, right)] += 1
        self.assertTrue(pairs, "no customer was active in two OpCos")
        self.assertGreater(
            pairs[("NORTHCO", "NORTHCO_MART")],
            0,
            "no retail-to-retail overlap, which is the case that was missing before",
        )


if __name__ == "__main__":
    unittest.main()
