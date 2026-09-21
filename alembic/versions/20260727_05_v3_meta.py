"""v3 metadata: glossary, lookup catalog, metric registry, access log

Creates fresh copies in ci_meta rather than altering the v2 tables in place.
The v2 glossary/lookup rows describe tables that no longer exist after the hard
replace, so migrating the data would carry nothing but stale rows -- the loaders
repopulate from the v3 seed CSVs.

Revision ID: 20260727_05
Revises: 20260727_04
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.v3_ddl import META, qualified

revision: str = "20260727_05"
down_revision: Union[str, Sequence[str], None] = "20260727_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "copilot_access_log",
    "metric_definition",
    "copilot_lookup_value",
    "copilot_glossary",
)


def upgrade() -> None:
    # =================================================================
    # copilot_glossary -- v2 columns plus six governance columns
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(META, 'copilot_glossary')} (
            id                      bigserial PRIMARY KEY,
            table_name              text    NOT NULL,
            column_name             text    NOT NULL,
            table_title             text,
            data_type               text,
            grain                   text,
            enum_values             jsonb   NOT NULL DEFAULT '[]'::jsonb,
            remarks                 text,
            description             text,
            lookup_resolution_mode  text,
            lookup_resolution_scope text,
            lookup_reference        text,
            ordinal                 integer NOT NULL DEFAULT 0,
            is_active               boolean NOT NULL DEFAULT true,

            -- v3 governance. These are what let the planner's context be filtered
            -- by role and category grant BEFORE it ever sees a column name.
            metric_class            text
                                    CHECK (metric_class IS NULL OR metric_class IN
                                          ('key','dimension','measure_volume','measure_money',
                                           'metadata','audit')),
            min_role_level          text    NOT NULL DEFAULT 'EXEC'
                                    CHECK (min_role_level IN ('EXEC','HOD')),
            is_identity             boolean NOT NULL DEFAULT false,
            aggregation_rule        text
                                    CHECK (aggregation_rule IS NULL OR aggregation_rule IN
                                          ('NONE','SUM','COUNT_DISTINCT_ONLY',
                                           'NEVER_AGGREGATE','RATIO')),

            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT uq_copilot_glossary_table_column UNIQUE (table_name, column_name),
            -- Money columns are HOD by definition; catch a bad seed row at load time.
            CONSTRAINT ck_glossary_money_is_hod CHECK (
                metric_class <> 'measure_money' OR min_role_level = 'HOD'
            ),
            -- An identity column may only ever be counted.
            CONSTRAINT ck_glossary_identity_agg CHECK (
                NOT is_identity OR aggregation_rule = 'COUNT_DISTINCT_ONLY'
            )
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_glossary_table ON {qualified(META, 'copilot_glossary')} "
        "(table_name, ordinal) WHERE is_active"
    )
    # The per-request filter: role level, then identity/scope flags.
    op.execute(
        f"CREATE INDEX idx_glossary_role ON {qualified(META, 'copilot_glossary')} "
        "(min_role_level, table_name) WHERE is_active"
    )
    op.execute(
        f"CREATE INDEX idx_glossary_identity ON {qualified(META, 'copilot_glossary')} "
        "(table_name, column_name) WHERE is_identity"
    )

    # =================================================================
    # copilot_lookup_value -- now scope-bearing
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(META, 'copilot_lookup_value')} (
            id                     bigserial PRIMARY KEY,
            source_scope           text  NOT NULL,
            source_table           text  NOT NULL,
            source_column          text  NOT NULL,
            reference_column       text  NOT NULL,
            raw_value              text  NOT NULL,
            normalized_value       text  NOT NULL,
            display_value          text  NOT NULL,
            normalized_search_text text  NOT NULL,

            -- v2 had opco_code as advisory metadata, which let an NorthCo user
            -- resolve an NorthCo Mart store name. It is now an RLS scope key.
            opco_code              text,
            opco_name              text,
            category_key           bigint,
            category_l1_key        bigint,
            category_l2_key        bigint,
            category_l3_key        bigint,
            category_l4_key        bigint,

            -- Typed entity class, so candidate generation is typed at the source
            -- and the resolver never needs negative filters to exclude, say, a
            -- value_segment enum from store matches.
            entity_class           text  NOT NULL DEFAULT 'unknown',

            row_context            jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            row_fingerprint        text  NOT NULL,
            is_active              boolean NOT NULL DEFAULT true,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_copilot_lookup_value ON {qualified(META, 'copilot_lookup_value')} (
            source_scope, source_table, source_column, reference_column,
            raw_value, COALESCE(opco_code, ''), row_fingerprint
        )
        """
    )
    # Exact-match path for the deterministic span matcher.
    op.execute(
        f"CREATE INDEX idx_lookup_exact ON {qualified(META, 'copilot_lookup_value')} "
        "(entity_class, normalized_value) WHERE is_active"
    )
    # Scoped dictionary build: fetch everything the caller may see, in one pass.
    op.execute(
        f"CREATE INDEX idx_lookup_scope ON {qualified(META, 'copilot_lookup_value')} "
        "(opco_code, entity_class, category_key) WHERE is_active"
    )
    # Fuzzy fallback for spans that did not match exactly.
    op.execute(
        f"CREATE INDEX idx_lookup_search_trgm ON {qualified(META, 'copilot_lookup_value')} "
        "USING gin (normalized_search_text gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX idx_lookup_norm_trgm ON {qualified(META, 'copilot_lookup_value')} "
        "USING gin (normalized_value gin_trgm_ops)"
    )
    for col in ("category_l1_key", "category_l2_key", "category_l3_key", "category_l4_key"):
        op.execute(
            f"CREATE INDEX idx_lookup_{col} ON {qualified(META, 'copilot_lookup_value')} "
            f"(opco_code, {col}) WHERE is_active"
        )

    # =================================================================
    # metric_definition -- semantic layer
    # =================================================================
    op.execute(
        f"""
        CREATE TABLE {qualified(META, 'metric_definition')} (
            metric_key      text    PRIMARY KEY,
            metric_name     text    NOT NULL,
            metric_class    text    NOT NULL
                            CHECK (metric_class IN ('volume','money','ratio_volume','ratio_money')),
            min_role_level  text    NOT NULL
                            CHECK (min_role_level IN ('EXEC','HOD')),
            base_table      text    NOT NULL,
            numerator_sql   text    NOT NULL,
            denominator_sql text,
            is_additive     boolean NOT NULL DEFAULT false,
            grain_note      text,
            synonyms        jsonb   NOT NULL DEFAULT '[]'::jsonb,
            description     text,
            is_active       boolean NOT NULL DEFAULT true,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),

            -- Money-bearing metrics are HOD by definition.
            CONSTRAINT ck_metric_money_is_hod CHECK (
                metric_class NOT IN ('money','ratio_money') OR min_role_level = 'HOD'
            ),
            -- A ratio needs a denominator and is never additive.
            CONSTRAINT ck_metric_ratio CHECK (
                (metric_class NOT IN ('ratio_volume','ratio_money'))
                OR (denominator_sql IS NOT NULL AND is_additive = false)
            )
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_metric_role ON {qualified(META, 'metric_definition')} "
        "(min_role_level) WHERE is_active"
    )
    # Synonym lookup for the intent agent.
    op.execute(
        f"CREATE INDEX idx_metric_synonyms_gin ON {qualified(META, 'metric_definition')} "
        "USING gin (synonyms jsonb_path_ops)"
    )

    # =================================================================
    # copilot_access_log -- who ran what, which MLflow structurally cannot answer
    # =================================================================
    # DELIBERATELY ABSENT: opco_codes, is_group_user and category_key_count would
    # record a grant scope that does not exist -- there is no row-level security and
    # every caller reaches every OpCo -- and logging them as constants would suggest
    # access is scoped when it is not. masked_cell_count is absent for the same kind
    # of reason: small-cell suppression is not implemented, so a column recording
    # how many cells were blanked would read, to whoever queries this log later, as
    # "none were" rather than "there is no such thing".
    #
    # The TABLE was very nearly removed with them, on the reading that its own
    # comment deferred to RLS as "the structural proof" and left this as a
    # misconfiguration-window record. That reading is backwards. FORCE ROW LEVEL
    # SECURITY was the evidence that scoping held; with it gone there is no
    # structural proof of anything, and this log is the only durable record of who
    # saw what. Every caller can now query every OpCo's customer data, which makes
    # that record more valuable than it was, not less.
    #
    # What survives untouched is the reason it was built: MLflow stores
    # safe_user_hash(user_id), so the trace alone cannot answer which PERSON ran a
    # query. No change to the access model affects that.
    op.execute(
        f"""
        CREATE TABLE {qualified(META, 'copilot_access_log')} (
            id                  bigserial PRIMARY KEY,
            mlflow_trace_id     text,
            principal_id        text    NOT NULL,
            role_level          text    NOT NULL CHECK (role_level IN ('EXEC','HOD')),
            denied_reason       text,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"COMMENT ON TABLE {qualified(META, 'copilot_access_log')} IS "
        "'Who ran what. principal_id is the REAL identity -- MLflow stores only "
        "safe_user_hash(user_id), so the trace alone cannot answer which PERSON ran a "
        "query. Question/SQL/latency stay in the trace, reached via mlflow_trace_id. "
        "There is no row-level security, so this is the only durable record of "
        "access. Records denials only -- there is no cell suppression to count.'"
    )
    op.execute(
        f"CREATE INDEX idx_access_log_principal ON {qualified(META, 'copilot_access_log')} "
        "(principal_id, created_at DESC)"
    )
    op.execute(
        f"CREATE INDEX idx_access_log_denied ON {qualified(META, 'copilot_access_log')} "
        "(created_at DESC) WHERE denied_reason IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX idx_access_log_trace ON {qualified(META, 'copilot_access_log')} "
        "(mlflow_trace_id) WHERE mlflow_trace_id IS NOT NULL"
    )

    for table in TABLES:
        op.execute(f"ANALYZE {qualified(META, table)}")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {qualified(META, table)} CASCADE")
