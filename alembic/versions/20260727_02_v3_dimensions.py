"""v3 dimensions: opco, store, product category, product

The category dimension is the important one. It carries a denormalised ancestor
chain (l1_key..l4_key), so a question naming a category at ANY level resolves to
one indexable equality on the facts instead of a recursive walk of the tree.

Two objects that were here are gone, and the reasons differ.

dim_product_category_closure held the ancestor->descendant expansion. Its only
consumer was permission resolution: a grant arrived as a node key and had to
become the full set of keys it covered. Access is no longer scoped by category,
and question-level filtering always ran off the denormalised l1..l4 chain above,
so nothing reads it.

dim_date held Northland public holidays and the fiscal calendar -- the only date
facts PostgreSQL cannot compute. Dropped on a scope decision rather than a
technical one: holiday and fiscal-period questions are out of scope. Year, month,
weekday and day-of-year were always EXTRACT / date_trunc on the calendar_date
already carried by every fact, so nothing else moved.

dim_opco keeps opco_type, business_domain and is_active -- the entity resolver
reads its roster and the planner may join it -- but loses is_group_entity, which
existed solely to derive the see-everything exception.

Revision ID: 20260727_02
Revises: 20260727_01
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.v3_ddl import CORE, ltree_type, qualified

revision: str = "20260727_02"
down_revision: Union[str, Sequence[str], None] = "20260727_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "dim_product",
    "dim_product_category",
    "dim_store",
    "dim_opco",
)


def upgrade() -> None:
    bind = op.get_bind()
    path_type = ltree_type(bind)

    # ------------------------------------------------------------------ opco
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'dim_opco')} (
            opco_code       text     PRIMARY KEY,
            opco_name       text     NOT NULL,
            opco_type       text     NOT NULL
                            CHECK (opco_type IN ('GROUP','OPERATING')),
            business_domain text     NOT NULL,
            is_active       boolean  NOT NULL DEFAULT true
        )
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'dim_opco')} IS "
        "'OpCo master. Joinable for opco_name and business_domain; the entity resolver "
        "reads its roster. opco_type=GROUP marks N360, which carries no fact rows.'"
    )

    # ----------------------------------------------------------------- store
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'dim_store')} (
            -- store_id is unique only WITHIN an OpCo, not globally: the seed data has
            -- NORTHCO store 1011 (QUILLREACH RAVENSHOLLOW) and NORTHCO_MART store 1011 (PIPERBROOK KELVINHOLLOW)
            -- as different stores. A global primary key on store_id would have
            -- silently collapsed 31 store pairs into one, attributing one OpCo's
            -- sales to the other's store name.
            store_id       bigint  NOT NULL,
            opco_code      text    NOT NULL REFERENCES {qualified(CORE, 'dim_opco')} (opco_code),
            store_name     text    NOT NULL,
            store_type     text,
            -- The region the store trades in, standardized. The feed used to spell
            -- one area several ways -- 'CENTRAL', 'CENTER REGION' and 'RETAIL -
            -- CENTRAL METRO VALLEY' are all the middle of the country -- so "how did
            -- Metro Valley stores do" either found a third of them or none, and the
            -- partial answer looked exactly like a complete one.
            --
            -- Standardized in the source file (data/store_name.csv) rather than in a
            -- second column beside the raw label. Two columns naming one concept is
            -- how a filter ends up on the wrong one: the copilot resolves a value,
            -- not a column, and 'Northern' sitting in both would have made the
            -- choice arbitrary.
            store_location text,
            -- Still NULL: the store feed carries no state or city. They are
            -- documented in the glossary, so a question naming Fenwickholt or Tannermere will
            -- be planned against them and answered with an empty result. Populate
            -- them before promising state-level answers.
            state_name     text,
            city_name      text,
            open_time      time,
            close_time     time,
            opened_date    date,
            closed_date    date,
            is_active      boolean NOT NULL DEFAULT true,
            PRIMARY KEY (opco_code, store_id)
        )
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'dim_store')} IS "
        "'Store master, keyed by (opco_code, store_id). store_id repeats across OpCos, "
        "so every join to a fact MUST match on both columns.'"
    )
    # Lookups by store_id alone still need an index for the fact-side join.
    op.execute(
        f"CREATE INDEX idx_dim_store_id ON {qualified(CORE, 'dim_store')} (store_id)"
    )
    op.execute(
        f"CREATE INDEX idx_dim_store_name_trgm ON {qualified(CORE, 'dim_store')} "
        "USING gin (store_name gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX idx_dim_store_location ON {qualified(CORE, 'dim_store')} "
        "(opco_code, store_location)"
    )

    # -------------------------------------------------------------- category
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'dim_product_category')} (
            category_key        bigint   PRIMARY KEY,
            opco_code           text     NOT NULL REFERENCES {qualified(CORE, 'dim_opco')} (opco_code),
            category_level      smallint NOT NULL CHECK (category_level BETWEEN 1 AND 4),
            category_code       text     NOT NULL,
            category_name       text     NOT NULL,
            parent_category_key bigint   REFERENCES {qualified(CORE, 'dim_product_category')} (category_key),
            is_leaf             boolean  NOT NULL,
            category_path       {path_type} NOT NULL,
            category_path_text  text     NOT NULL,
            l1_key              bigint   NOT NULL,
            l1_name             text     NOT NULL,
            l2_key              bigint,
            l2_name             text,
            l3_key              bigint,
            l3_name             text,
            l4_key              bigint,
            l4_name             text,
            is_active           boolean  NOT NULL DEFAULT true,
            -- Uniqueness is per PARENT, not per level. A code like OTHERS or
            -- ACCESSORIES legitimately repeats at the same level under different
            -- parents -- the seed catalog has 78 level-4 nodes named OTHERS. What
            -- must be unique is a code among its siblings.
            -- COALESCE because parent_category_key is NULL at level 1 and NULLs do
            -- not conflict with each other in a unique constraint.
            -- A level-N node must have its own level populated and nothing deeper.
            CONSTRAINT ck_dim_product_category_levels CHECK (
                (category_level >= 2) = (l2_key IS NOT NULL)
                AND (category_level >= 3) = (l3_key IS NOT NULL)
                AND (category_level >= 4) = (l4_key IS NOT NULL)
            ),
            -- Level 1 is the only level allowed to have no parent.
            CONSTRAINT ck_dim_product_category_parent CHECK (
                (category_level = 1) = (parent_category_key IS NULL)
            )
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX uq_dim_prodcat_sibling_code ON "
        f"{qualified(CORE, 'dim_product_category')} "
        "(opco_code, category_level, COALESCE(parent_category_key, 0), category_code)"
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(CORE, 'dim_product_category')} IS "
        "'4-level category hierarchy. The denormalised l1..l4 ancestor keys are what "
        "make a permission grant at any level enforceable as one indexable equality.'"
    )
    for col in ("l1_key", "l2_key", "l3_key", "l4_key"):
        op.execute(
            f"CREATE INDEX idx_dim_prodcat_{col} ON {qualified(CORE, 'dim_product_category')} "
            f"(opco_code, {col})"
        )
    op.execute(
        f"CREATE INDEX idx_dim_prodcat_name_trgm ON {qualified(CORE, 'dim_product_category')} "
        "USING gin (category_name gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX idx_dim_prodcat_parent ON {qualified(CORE, 'dim_product_category')} "
        "(parent_category_key)"
    )
    op.execute(
        f"CREATE INDEX idx_dim_prodcat_leaf ON {qualified(CORE, 'dim_product_category')} "
        "(opco_code, category_key) WHERE is_leaf"
    )
    if path_type == "ltree":
        op.execute(
            f"CREATE INDEX idx_dim_prodcat_path_gist ON "
            f"{qualified(CORE, 'dim_product_category')} USING gist (category_path)"
        )

    # --------------------------------------------------------------- product
    # Optional: only populated when SKU-level questions are in scope.
    op.execute(
        f"""
        CREATE TABLE {qualified(CORE, 'dim_product')} (
            sku_key         bigint  PRIMARY KEY,
            opco_code       text    NOT NULL REFERENCES {qualified(CORE, 'dim_opco')} (opco_code),
            sku_code        text    NOT NULL,
            sku_name        text    NOT NULL,
            brand_name      text,
            category_key    bigint  NOT NULL
                            REFERENCES {qualified(CORE, 'dim_product_category')} (category_key),
            category_l1_key bigint  NOT NULL,
            category_l2_key bigint,
            category_l3_key bigint,
            category_l4_key bigint,
            uom             text,
            is_active       boolean NOT NULL DEFAULT true,
            CONSTRAINT uq_dim_product_code UNIQUE (opco_code, sku_code)
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_dim_product_category ON {qualified(CORE, 'dim_product')} "
        "(opco_code, category_key)"
    )
    op.execute(
        f"CREATE INDEX idx_dim_product_name_trgm ON {qualified(CORE, 'dim_product')} "
        "USING gin (sku_name gin_trgm_ops)"
    )

    for table in reversed(TABLES):
        op.execute(f"ANALYZE {qualified(CORE, table)}")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {qualified(CORE, table)} CASCADE")
