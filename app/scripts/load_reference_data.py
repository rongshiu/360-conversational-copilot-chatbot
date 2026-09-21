# app/scripts/load_reference_data.py
"""Load v3 reference data from the seed CSVs.

    python -m app.scripts.load_reference_data

Builds, in dependency order:

  ci_core.dim_opco                        from the OpCos present in the seeds
  ci_core.dim_product_category            4 levels from product_catalog.csv
  ci_core.dim_store                       from store_name.csv
  ci_meta.copilot_lookup_value            entity grounding, derived from the above

Replaces the v2 load_lookup_values.py, which inferred a two-valued "scope"
(store|product) from glossary remarks text. v3 needs typed entity classes and the
category scope keys that row-level security filters on, and both come from the
hierarchy itself rather than from prose.

Category keys are assigned deterministically: nodes are sorted by
(opco, level, path) and numbered from 1000. A full truncate-and-reload therefore
produces identical keys every run, so a permission payload holding category_keys
stays valid across reloads. Change the sort and every stored grant breaks.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.core import settings
from app.db.postgres import get_pg_conn
from app.utils.text_utils import normalize_lookup_text, safe_schema_name

CORE = safe_schema_name(settings.ci_core_schema)
META = safe_schema_name(settings.ci_meta_schema)

CATEGORY_KEY_BASE = 1000
SKU_KEY_BASE = 500_000

# SKUs per leaf category. Two is enough for a SKU question to have a ranking to
# report without multiplying the fact tables.
SKUS_PER_LEAF = 2
# Brands carried by dim_product and denormalised onto agg_sales_sku_monthly. They
# are deliberately absent from every customer table: brand x customer multiplied
# the customer bridge, and brand answers what SOLD rather than who bought it.
SKU_BRANDS = ("NorthValu", "Best Price", "House Brand", "National", "Import")
SKU_UOMS = ("EA", "PK", "KG", "L")

# HOLIDAYS, CALENDAR_START and CALENDAR_END were here, feeding dim_date. That table
# does not exist -- holiday and fiscal-period questions are out
# of scope -- so there is no calendar to generate. Year, month, weekday and
# day-of-year were always EXTRACT / date_trunc on the date column already present on
# every fact.

# Level 1..4 column names in product_catalog.csv, shallowest first.
LEVEL_COLUMNS = ("product_line", "product_division", "product_group", "product_category")

# is_group_entity was the third element. It marked N360 as the holding entity and
# drove the see-everything exception, which is gone with OpCo scoping. N360 stays in
# the roster as an ordinary row: it carries no fact data, so it resolves as a name
# and returns nothing, which is now simply true rather than an access boundary.
OPCO_META: dict[str, tuple[str, str, str]] = {
    # code: (name, type, business_domain)
    "NORTHCO": ("NorthCo", "OPERATING", "RETAIL"),
    "NORTHCO_MART": ("NorthCo Mart", "OPERATING", "RETAIL"),
    "NORTHCO_CREDIT": ("NorthCo Credit", "OPERATING", "FINANCIAL_SERVICES"),
    "NORTHCO_BANK": ("NorthCo Bank", "OPERATING", "BANKING"),
    "N360": ("N360", "GROUP", "RETAIL"),
}


# store_location as it arrives from the store feed is an operational label rather
# than a geography, and the same area appears under several of them: 'CENTRAL',
# 'CENTER REGION' and 'RETAIL - CENTRAL METRO VALLEY' are all the middle of the
# country, while 'REGION 1'..'REGION 3' say nothing at all. Nobody asks a question
# in those terms, so "how did Metro Valley stores do last quarter" could not be
# answered even though every store carries a location.
#
# This folds them into the regions the business actually names. Matched on a
# substring of the normalized label, longest rule first, so 'RETAIL - NORTH OAKENTON
# JUNIPERFORD' resolves to Metro Valley rather than Northern.
#
# Anything unmatched becomes 'Unclassified' rather than a guess: 'REGION 2' cannot
# be placed from the data available, and inventing a region for it would put real
# stores in the wrong place in a real answer.
STORE_REGIONS: tuple[tuple[str, str], ...] = (
    ("metro valley", "Metro Valley"),
    ("east coast", "East Coast"),
    ("eastern isles", "Eastern Isles"),
    ("north", "Northern"),
    ("south", "Southern"),
    ("central", "Central"),
    ("center", "Central"),
)

UNCLASSIFIED_REGION = "Unclassified"

# The values dim_store.store_location may hold, matching the enum_values documented
# for it in the glossary -- which is what puts them in the copilot's vocabulary.
CANONICAL_REGIONS: frozenset[str] = frozenset(
    {region for _, region in STORE_REGIONS} | {UNCLASSIFIED_REGION}
)


def store_region(location: Any) -> str:
    normalized = normalize_lookup_text(_clean(location))
    if not normalized:
        return UNCLASSIFIED_REGION
    for needle, region in STORE_REGIONS:
        if needle in normalized:
            return region
    return UNCLASSIFIED_REGION


def resolve_store_region(store: dict[str, Any]) -> str:
    """The store's region, canonical.

    data/store_name.csv now holds standardized values, so this is usually a
    pass-through. The fold still runs for anything that is not already canonical --
    an older copy of the file, or a newly added store carrying a raw operational
    label -- so one unstandardized row cannot quietly create an eighth region that
    no question will ever match.
    """
    declared = _clean(store.get("store_location"))
    if declared in CANONICAL_REGIONS:
        return declared
    return store_region(declared)


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _read(path_env: str, default: str) -> pd.DataFrame:
    path = Path(os.getenv(path_env, default))
    if not path.exists():
        raise FileNotFoundError(f"Seed CSV not found: {path} (set {path_env})")
    return pd.read_csv(path).rename(columns=lambda c: str(c).strip())


# ---------------------------------------------------------------------------
# Category hierarchy
# ---------------------------------------------------------------------------

@dataclass
class CategoryNode:
    opco_code: str
    level: int
    code: str
    name: str
    path_codes: tuple[str, ...]
    key: int = 0
    parent_key: int | None = None
    is_leaf: bool = True
    ancestors: dict[int, int] = field(default_factory=dict)   # level -> key
    ancestor_names: dict[int, str] = field(default_factory=dict)

    @property
    def path_id(self) -> tuple[str, ...]:
        return (self.opco_code, *self.path_codes)

    def ltree_path(self) -> str:
        # ltree labels allow only [A-Za-z0-9_], so everything else collapses to _.
        parts = [
            "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in p.lower())
            for p in self.path_id
        ]
        return ".".join(p or "x" for p in parts)

    def path_text(self) -> str:
        return " > ".join(self.path_codes)


def build_category_nodes(df: pd.DataFrame) -> list[CategoryNode]:
    nodes: dict[tuple[str, ...], CategoryNode] = {}

    for record in df.to_dict("records"):
        opco = _clean(record.get("opco_code")).upper()
        if not opco:
            continue

        path: list[str] = []
        for level, column in enumerate(LEVEL_COLUMNS, start=1):
            value = _clean(record.get(column))
            if not value:
                # A branch can legitimately bottom out above level 4.
                break
            path.append(value)
            node_id = (opco, *path)
            if node_id not in nodes:
                nodes[node_id] = CategoryNode(
                    opco_code=opco,
                    level=level,
                    code=value,
                    name=value,
                    path_codes=tuple(path),
                )

    ordered = sorted(nodes.values(), key=lambda n: (n.opco_code, n.level, n.path_codes))
    for index, node in enumerate(ordered):
        node.key = CATEGORY_KEY_BASE + index

    by_id = {n.path_id: n for n in ordered}
    for node in ordered:
        if node.level > 1:
            parent = by_id.get(node.path_id[:-1])
            if parent is not None:
                node.parent_key = parent.key
                parent.is_leaf = False
        # Ancestor chain including self, so l{level}_key is always populated.
        for depth in range(1, node.level + 1):
            ancestor = by_id.get(node.path_id[: depth + 1])
            if ancestor is not None:
                node.ancestors[depth] = ancestor.key
                node.ancestor_names[depth] = ancestor.name

    return ordered


# ---------------------------------------------------------------------------
# Lookup catalog
# ---------------------------------------------------------------------------

def _fingerprint(*parts: Any) -> str:
    import hashlib

    blob = "|".join(str(p) for p in parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:32]


# Columns whose values are identifiers or counts rather than words a user would
# type. `_code`/`_key`/`_id` are keys; the opco columns duplicate the `opco` entity
# class; the `*_months_*` and `*_seq`/`*_count`/`*_level` families enumerate
# integers.
_KEY_COLUMN_SUFFIXES = ("_code", "_key", "_id", "_seq", "_count", "_level")
_KEY_COLUMN_NAMES = frozenset({"opco_code", "opco_name", "opco_codes", "primary_opco_code"})


def _is_key_column(column: str) -> bool:
    name = (column or "").strip().lower()
    if name in _KEY_COLUMN_NAMES:
        return True
    if name.endswith(_KEY_COLUMN_SUFFIXES):
        return True
    # active_months_12m, purchase_months_6m, ...
    return "_months_" in name or name.endswith("_months")


def _answerable_tables() -> frozenset[str]:
    """Tables whose values a user can actually ask about.

    The glossary documents every table, including the copilot's own metadata --
    copilot_glossary, copilot_lookup_value, metric_definition.
    Those carry enum columns too, and loading them put their values into the user's
    entity vocabulary: `source_scope` contributed "product", "store" and
    "category"; `metric_class` contributed "key", "dimension", "metadata" and
    "audit"; `aggregation_rule` contributed "SUM", "RATIO" and "NONE".

    They are ordinary English words, they are indistinguishable from a real value
    once they are in the catalogue, and no SQL the planner may write can filter on
    them -- these tables are not queryable. "How many customers hold more than one
    product" was answered with `source_scope = 'product'` on that basis.

    Derived from the table registry, so a new serving view brings its vocabulary
    with it and a new internal table stays out.
    """
    from app.service.table_registry import JOINABLE_DIMENSIONS, TABLE_PROFILES

    return frozenset(
        {profile.base_table for profile in TABLE_PROFILES.values()}
        | set(JOINABLE_DIMENSIONS)
        | {"bridge_customer_category_monthly", "dim_product"}
    )


def read_enum_values() -> dict[str, list[str]]:
    """column -> allowed values, from the governed glossary.

    Read from ci_meta.copilot_glossary if it is populated, else from the seed CSV,
    matching how GlossaryService resolves the same data at runtime.
    """
    from app.utils.text_utils import parse_enum_values

    answerable = _answerable_tables()

    def collect(pairs) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for table, column, raw in pairs:
            if _clean(table) not in answerable:
                continue
            column = _clean(column)
            # The DB column is jsonb/array, so psycopg hands back a real list;
            # the CSV hands back a string. Stringifying a list and re-parsing it
            # produced fragments like "['Elite" and "'At Risk']".
            if isinstance(raw, (list, tuple)):
                values = [_clean(v) for v in raw if _clean(v)]
            else:
                values = [v for v in parse_enum_values(raw) if _clean(v)]
            if not column or not values:
                continue
            # Same column can appear on several tables with the same enum.
            out.setdefault(column, [])
            for value in values:
                if value not in out[column]:
                    out[column].append(value)
        return out

    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT table_name, column_name, enum_values "
                    f"FROM {META}.copilot_glossary WHERE enum_values IS NOT NULL"
                )
                pairs = cur.fetchall()
        if pairs:
            return collect(pairs)
    except Exception:  # noqa: BLE001
        pass

    df = pd.read_csv(settings.glossary_csv_path).rename(columns=lambda c: str(c).strip())
    if "enum_values" not in df.columns:
        return {}
    return collect(
        zip(df.get("table_name", []), df.get("column_name", []), df.get("enum_values", []))
    )


def build_product_rows(nodes: list[CategoryNode]) -> list[tuple[Any, ...]]:
    """SKUs under every leaf category.

    A dimension, so it belongs here rather than in the fact generator: it has a
    foreign key to dim_product_category, and this loader truncates that table
    CASCADE -- which silently deleted SKUs seeded elsewhere on every deploy.
    """
    import random

    rng = random.Random(20260101)
    rows: list[tuple[Any, ...]] = []
    key = SKU_KEY_BASE
    for node in nodes:
        if not node.is_leaf:
            continue
        for _ in range(SKUS_PER_LEAF):
            key += 1
            rows.append(
                (
                    key, node.opco_code, f"SKU{key}",
                    f"{rng.choice(SKU_BRANDS)} {node.name}"[:200],
                    node.key,
                    node.ancestors.get(1), node.ancestors.get(2),
                    node.ancestors.get(3), node.ancestors.get(4),
                    rng.choice(SKU_BRANDS), rng.choice(SKU_UOMS), True,
                )
            )
    return rows


def build_lookup_rows(
    nodes: list[CategoryNode],
    stores: list[dict[str, Any]],
    enum_values: dict[str, list[str]] | None = None,
) -> list[tuple[Any, ...]]:
    """One lookup row per resolvable entity name.

    entity_class is what makes the resolver typed: a category node is
    category_l1..l4 by its own level, a store is `store`. Because the class is
    stored, candidate generation can never return a category for a store span, so
    v2's three negative filter lists are unnecessary.
    """
    rows: list[tuple[Any, ...]] = []

    for node in nodes:
        raw = node.name
        normalized = normalize_lookup_text(raw)
        if not normalized:
            continue

        # Searchable text includes the full path, so "soft fashion" and "fashion"
        # both reach the same node.
        search_text = normalize_lookup_text(" ".join(node.path_codes))

        rows.append(
            (
                "category",                       # source_scope
                "dim_product_category",           # source_table
                LEVEL_COLUMNS[node.level - 1],    # source_column
                "category_name",                  # reference_column
                raw,
                normalized,
                node.path_text(),                 # display_value
                search_text,
                node.opco_code,
                OPCO_META.get(node.opco_code, (node.opco_code,))[0],
                node.key,
                node.ancestors.get(1),
                node.ancestors.get(2),
                node.ancestors.get(3),
                node.ancestors.get(4),
                f"category_l{node.level}",        # entity_class
                json.dumps(
                    {
                        "category_level": node.level,
                        "category_path": node.path_text(),
                        "is_leaf": node.is_leaf,
                    },
                    ensure_ascii=False,
                ),
                _fingerprint("category", node.opco_code, node.key),
            )
        )

    # OpCo entities. Scoped by opco_code like everything else, so a caller's
    # dictionary contains only their own -- the resolver uses the unscoped dim_opco
    # roster separately to detect an out-of-scope mention.
    for code, meta in OPCO_META.items():
        if meta[2]:  # skip the group entity
            continue
        # "NorthCo Mart", "NORTHCO_MART" and "NorthCo MART" all normalize to "northco mart", so
        # emitting one row per label produced three identical candidates and the
        # resolver reported a false ambiguity: "I found more than one match:
        # 1. NorthCo Mart 2. NorthCo Mart 3. NorthCo Mart". Dedupe on the normalized form.
        seen_labels: set[str] = set()
        for label in (meta[0], code, code.replace("_", " ")):
            normalized = normalize_lookup_text(label)
            if not normalized or normalized in seen_labels:
                continue
            seen_labels.add(normalized)
            rows.append(
                (
                    "opco", "dim_opco", "opco_code", "opco_name",
                    code, normalized, meta[0], normalized,
                    code, meta[0],
                    None, None, None, None, None,
                    "opco",
                    json.dumps({"opco_code": code}, ensure_ascii=False),
                    _fingerprint("opco", code, label),
                )
            )

    for store in stores:
        raw = _clean(store.get("store_name"))
        normalized = normalize_lookup_text(raw)
        if not normalized:
            continue
        opco = _clean(store.get("opco_code")).upper()
        rows.append(
            (
                "store",
                "dim_store",
                "store_name",
                "store_name",
                raw,
                normalized,
                raw,
                normalized,
                opco,
                OPCO_META.get(opco, (opco,))[0],
                None,
                None,
                None,
                None,
                None,
                "store",
                json.dumps(
                    {
                        "store_id": store.get("store_id"),
                        "store_type": _clean(store.get("store_type")),
                        "store_location": _clean(store.get("store_location")),
                    },
                    ensure_ascii=False,
                ),
                _fingerprint("store", opco, store.get("store_id")),
            )
        )

    # Store formats, with the operational prefix stripped.
    #
    # store_type reads 'SPECIALTY STORE - WELLNESS', 'SM - FRESHMART', 'SPECIALTY
    # STORE - MISTER DONUT'. Nobody asks about a "specialty store"; they ask about
    # NorthCo Wellness, FreshMart and Mister Donut. Loading the raw value alone left
    # "wellness outlets" resolving to nothing, so the planner had no filter for a
    # banner that is 102 of the 552 stores.
    #
    # The stripped form goes in as the search text, which the dictionary indexes as
    # a second exact alias, so both spellings resolve without any fuzzy guessing.
    # Typed as `enum` on purpose: it filters dim_store.store_type, which is exactly
    # what an enum slot does, and it needs no new entity class anywhere.
    seen_types: set[tuple[str, str]] = set()
    for store in stores:
        raw_type = _clean(store.get("store_type"))
        opco = _clean(store.get("opco_code")).upper()
        normalized = normalize_lookup_text(raw_type)
        if not normalized or (opco, normalized) in seen_types:
            continue
        seen_types.add((opco, normalized))

        banner = normalized
        for prefix in ("specialty store", "speciality store", "gms", "sm", "nsc", "shop"):
            if banner.startswith(f"{prefix} "):
                banner = banner[len(prefix) + 1 :].strip()
                break

        rows.append(
            (
                "store",                      # source_scope
                "dim_store",
                "store_type",                 # what it filters
                "store_type",
                raw_type,
                normalized,
                raw_type,
                banner or normalized,         # the short alias users actually type
                opco,
                OPCO_META.get(opco, (opco,))[0],
                None, None, None, None, None,
                "enum",                       # entity_class
                json.dumps({"column": "store_type"}, ensure_ascii=False),
                _fingerprint("store_type", opco, raw_type),
            )
        )

    # Enum values -- the vocabulary of every low-cardinality column, from the
    # glossary.
    #
    # This closes the gap that made the resolver look erratic. "How many customers
    # are in Elite, Premium, Growth, Mass and At Risk" names five real values of
    # value_segment, but the catalog held only stores, categories and OpCos, so none
    # of those words had an exact hit. They fell through to fuzzy matching and landed
    # on the nearest PRODUCT: "elite" on CORPORATE ELITE, "premium" on PREMIUM
    # DESSERT, "mass" on FRESHMART PLAZA S12. HOLLISBURN -- three filters for a question that
    # named none.
    #
    # The general rule this follows: the dictionary must hold the COMPLETE schema
    # vocabulary. Exact matching then wins for every word the schema knows, and the
    # fuzzy pass only ever sees words it was built for -- misspellings of real
    # names. Every false positive so far came from a word the dictionary should have
    # recognised exactly and did not.
    #
    # opco_code and category_key stay NULL: a value_segment of "Elite" means the
    # same thing in every OpCo. The RLS policy admits NULL for both, so this
    # vocabulary is visible to every caller without being tenant data.
    # Not every enum column is vocabulary a user names. Three rules, each with a
    # concrete failure behind it:
    #
    #   keys are not words -- opco_code/opco_name are already the `opco` entity
    #   class, and loading them again made "northco co" resolve as an enum instead of
    #   an OpCo, which broke the cross-OpCo overlap question outright.
    #
    #   counts are not words -- active_months_12m and daypart_seq enumerate 0..12,
    #   so "between 3 and 22 june" resolved "3" as a filter value.
    #
    #   an enum must not shadow a real entity name -- if a store or category is
    #   already called this, the entity wins; the enum would be a second reading of
    #   a name that already has one.
    # Built from every row added above -- categories, OpCos and stores -- which is
    # why this block runs last. Computed earlier it saw only categories, and
    # preferred_store_type = "NorthCo Mart" slipped through to shadow the OpCo.
    entity_names = {normalize_lookup_text(row[5]) for row in rows if row[5]}
    for column, values in sorted((enum_values or {}).items()):
        if _is_key_column(column):
            continue
        for raw in values:
            normalized = normalize_lookup_text(raw)
            if not normalized:
                continue
            if len(normalized) < 3 or normalized.replace(" ", "").isdigit():
                continue
            if normalized in entity_names:
                continue
            rows.append(
                (
                    "enum",                       # source_scope
                    "",                           # source_table (many)
                    column,                       # source_column -- what it filters
                    column,                       # reference_column
                    raw,
                    normalized,
                    raw,                          # display_value
                    normalized,
                    None,                         # opco_code -- global vocabulary
                    None,
                    None,                         # category_key
                    None, None, None, None,
                    "enum",                       # entity_class
                    json.dumps({"column": column}, ensure_ascii=False),
                    _fingerprint("enum", column, raw),
                )
            )

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    catalog = _read("PRODUCT_CATALOG_CSV_PATH", "/app/data/product_catalog.csv")
    store_df = _read("STORE_CSV_PATH", "/app/data/store_name.csv")

    nodes = build_category_nodes(catalog)
    # Dedupe on (opco_code, store_id): store_id alone is not unique across OpCos.
    stores = []
    _seen_stores: set[tuple[str, int]] = set()
    for record in store_df.to_dict("records"):
        raw_id = _clean(record.get("store_id"))
        if not raw_id:
            continue
        key = (_clean(record.get("opco_code")).upper(), int(float(raw_id)))
        if key in _seen_stores:
            continue
        _seen_stores.add(key)
        stores.append({k: v for k, v in record.items()})
    enum_values = read_enum_values()
    lookup_rows = build_lookup_rows(nodes, stores, enum_values)
    product_rows = build_product_rows(nodes)

    opcos = sorted(
        {n.opco_code for n in nodes} | {_clean(s.get("opco_code")).upper() for s in stores} | {"N360"}
    )

    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            # The login user holds no grants of its own -- writes go through the
            # loader role. This survived the removal of row-level security: the
            # policies are gone, the privilege split is not.
            cur.execute(f'SET LOCAL ROLE "{settings.ci_loader_role}"')

            # Children first: store references the category and opco tables.
            cur.execute(f"TRUNCATE TABLE {META}.copilot_lookup_value")
            cur.execute(f"TRUNCATE TABLE {CORE}.dim_store CASCADE")
            cur.execute(f"TRUNCATE TABLE {CORE}.dim_product_category CASCADE")
            cur.execute(f"TRUNCATE TABLE {CORE}.dim_opco CASCADE")

            cur.executemany(
                f"INSERT INTO {CORE}.dim_opco "
                "(opco_code, opco_name, opco_type, business_domain, is_active) "
                "VALUES (%s, %s, %s, %s, true)",
                [
                    (code, *OPCO_META.get(code, (code, "OPERATING", False, "RETAIL")))
                    for code in opcos
                ],
            )

            cur.executemany(
                f"""
                INSERT INTO {CORE}.dim_product_category (
                    category_key, opco_code, category_level, category_code, category_name,
                    parent_category_key, is_leaf, category_path, category_path_text,
                    l1_key, l1_name, l2_key, l2_name, l3_key, l3_name, l4_key, l4_name,
                    is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
                """,
                [
                    (
                        n.key, n.opco_code, n.level, n.code, n.name,
                        n.parent_key, n.is_leaf, n.ltree_path(), n.path_text(),
                        n.ancestors.get(1), n.ancestor_names.get(1),
                        n.ancestors.get(2), n.ancestor_names.get(2),
                        n.ancestors.get(3), n.ancestor_names.get(3),
                        n.ancestors.get(4), n.ancestor_names.get(4),
                    )
                    for n in nodes
                ],
            )

            cur.executemany(
                f"""
                INSERT INTO {CORE}.dim_store (
                    store_id, opco_code, store_name, store_type, store_location,
                    is_active
                ) VALUES (%s, %s, %s, %s, %s, true)
                """,
                [
                    (
                        int(float(_clean(s.get("store_id")))),
                        _clean(s.get("opco_code")).upper(),
                        _clean(s.get("store_name")),
                        _clean(s.get("store_type")) or None,
                        resolve_store_region(s),
                    )
                    for s in stores
                ],
            )

            cur.executemany(
                f"""INSERT INTO {CORE}.dim_product
                    (sku_key, opco_code, sku_code, sku_name, category_key,
                     category_l1_key, category_l2_key, category_l3_key,
                     category_l4_key, brand_name, uom, is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                product_rows,
            )

            cur.executemany(
                f"""
                INSERT INTO {META}.copilot_lookup_value (
                    source_scope, source_table, source_column, reference_column,
                    raw_value, normalized_value, display_value, normalized_search_text,
                    opco_code, opco_name, category_key,
                    category_l1_key, category_l2_key, category_l3_key, category_l4_key,
                    entity_class, row_context, row_fingerprint, is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,true)
                """,
                lookup_rows,
            )


    # Statistics are refreshed after the load commits, on a connection with no
    # role set. ANALYZE requires table ownership in PostgreSQL 16 (MAINTAIN only
    # exists from 17), and the loader role owns nothing -- so issuing it inside the
    # load transaction skipped every table with
    # "permission denied to analyze ..., skipping it", leaving the serving schema
    # with no statistics at all.
    with get_pg_conn() as analyze_conn:
        with analyze_conn.cursor() as cur:
            skipped: list[str] = []
            for table in (
                f"{CORE}.dim_opco",
                f"{CORE}.dim_product_category",
                f"{CORE}.dim_store",
                f"{META}.copilot_lookup_value",
            ):
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

    # Invalidate the in-process caches keyed on this data. Without this a running
    # app keeps a stale category-grant expansion and a stale entity dictionary
    # until it restarts, which would silently resolve names to keys that no longer
    # exist.
    try:
        from app.agents.sql_validator_agent import clear_opco_attribute_index
        from app.service.entity_resolution import clear_dictionary_cache
        from app.service.principal_service import clear_expansion_cache

        clear_opco_attribute_index()

        clear_dictionary_cache()
        clear_expansion_cache()
    except Exception:  # noqa: BLE001 - a loader run must not fail over a cache hint
        pass

    levels = {}
    for n in nodes:
        levels[n.level] = levels.get(n.level, 0) + 1
    print(f"dim_opco                     : {len(opcos)}")
    print(f"dim_product_category         : {len(nodes)}  by level {dict(sorted(levels.items()))}")
    print(f"dim_store                    : {len(stores)}")
    print(f"dim_product                  : {len(product_rows)}")
    enum_count = sum(len(v) for v in enum_values.values())
    print(
        f"copilot_lookup_value         : {len(lookup_rows)}"
        f"  (incl. {enum_count} enum values across {len(enum_values)} columns)"
    )


if __name__ == "__main__":
    main()
