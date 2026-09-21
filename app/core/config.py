# app/core/config.py

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------------
    # PostgreSQL
    # ---------------------------------------------------------------------
    # Only db_user is read by the application (migration 20260727_01 prints the
    # bootstrap command with it). DB_PASSWORD / DB_NAME / DB_HOST / DB_PORT are
    # consumed by docker-compose to provision the postgres container -- the app
    # itself connects via POSTGRES_URI, so declaring them here would imply a
    # dependency that does not exist.
    db_user: str = Field(default="app", alias="DB_USER")

    postgres_schema: str = Field(default="public", alias="POSTGRES_SCHEMA")

    # ---------------------------------------------------------------------
    # v3 serving schemas
    # ---------------------------------------------------------------------
    # ci_core -> base tables and dimensions.
    #
    #            Neither read role holds SELECT on the FACT tables any more. That
    #            note used to say the opposite, and explain why: the persona views
    #            were security_invoker, so base-table permissions were checked as
    #            the calling role and revoking the grant would have broken every
    #            view. The consequence was that the HOD/EXEC money split was
    #            enforced by sql_validator_agent rejecting base-table names, not by
    #            PostgreSQL -- a validator bug was a revenue disclosure.
    #
    #            Revision 20260828_09 did what that note prescribed: the read role
    #            is split in two, the views run with the owner's privileges, and
    #            each persona role can reach exactly one persona schema. The four
    #            dimensions stay readable by both -- generated SQL joins them
    #            directly and they carry neither money nor a customer key.
    # ci_hod   -> persona views including money measures. Readable ONLY by
    #            ci_copilot_hod.
    # ci_exec  -> persona views with money measures physically absent, so a
    #            query that references one fails to resolve instead of
    #            silently returning zeros. Readable ONLY by ci_copilot_exec.
    # ci_meta  -> glossary, lookup catalog, metric registry, access log.
    #
    # The request's role_level decides both which role the session switches into
    # and which persona schema goes on the search_path, so the planner emits one
    # query and the database resolves it to the permitted view -- and refuses the
    # other one outright rather than relying on the query never naming it.
    ci_core_schema: str = Field(default="ci_core", alias="CI_CORE_SCHEMA")
    ci_hod_schema: str = Field(default="ci_hod", alias="CI_HOD_SCHEMA")
    ci_exec_schema: str = Field(default="ci_exec", alias="CI_EXEC_SCHEMA")
    ci_meta_schema: str = Field(default="ci_meta", alias="CI_META_SCHEMA")

    # The two non-login read roles. copilot_scope() switches into one of them per
    # request, chosen by role_level, and each can read exactly one persona schema.
    # This is the whole of access control, and it is the database's to enforce:
    # neither role can reach the other's views, and neither can reach the fact
    # tables the views are built from.
    #
    # The login user must not be superuser -- a superuser owns its way past object
    # privileges the same way it used to bypass RLS.
    ci_copilot_hod_role: str = Field(
        default="ci_copilot_hod", alias="CI_COPILOT_HOD_ROLE"
    )
    ci_copilot_exec_role: str = Field(
        default="ci_copilot_exec", alias="CI_COPILOT_EXEC_ROLE"
    )

    # The single read role that preceded them, and the only thing that still refers
    # to it: revisions 01, 06 and 07 grant to it, and revision 09 revokes every one
    # of those grants. Kept so those revisions still import on a database that has
    # not reached 09, and so the revoke names the same role the grants did. Nothing
    # in the request path reads it.
    ci_copilot_role: str = Field(default="ci_copilot", alias="CI_COPILOT_ROLE")

    # Write role for loaders and ETL. Has a permissive RLS policy, so bulk loads
    # are unaffected by scoping. Combined with FORCE ROW LEVEL SECURITY this
    # makes the design fail CLOSED: a session that forgets to SET ROLE gets zero
    # rows and cannot write, rather than silently getting everything.
    ci_loader_role: str = Field(default="ci_loader", alias="CI_LOADER_ROLE")

    # Read-only LOGIN role for humans: analysts, debugging, BI tools. Sees every
    # OpCo, holds SELECT and nothing else.
    #
    # It reads through an explicit `FOR SELECT ... USING (true)` policy rather than
    # the BYPASSRLS attribute. BYPASSRLS is a role property that silently applies
    # to every table in every database in the cluster, including tables created
    # later, and never appears in `\d+`. A policy is visible on the table it
    # affects and cannot leak past the tables the migration grants it on -- the
    # same reasoning that gave ci_loader a policy instead of the attribute.
    #
    # Deliberately NOT a member of ci_loader: that grant would work (an inheriting
    # member matches the loader policy) but would carry INSERT/UPDATE/DELETE/
    # TRUNCATE with it, so a read-only account would silently hold write access.
    ci_analyst_role: str = Field(default="ci_analyst", alias="CI_ANALYST_ROLE")

    # Monthly range partitions are pre-created for this window around the
    # anchor month so inserts never land in the DEFAULT partition.
    ci_partition_months_back: int = Field(default=27, alias="CI_PARTITION_MONTHS_BACK")
    ci_partition_months_forward: int = Field(default=3, alias="CI_PARTITION_MONTHS_FORWARD")

    # CI_MIN_CELL_SIZE was here. It drove small-cell suppression: any customer
    # count between 1 and the threshold was blanked before the rows reached the
    # answer agent, because a narrow enough filter ("Fashion buyers at store X,
    # Morning, on 3 Jun" -> 1) identifies a person as effectively as printing
    # their id. It is NOT implemented; those counts are simply answered.
    #
    # What still holds is structural: the sales facts carry no customer key, the
    # customer tables expose only an opaque surrogate the validator permits solely
    # inside COUNT(DISTINCT), and a request to identify or list individuals is
    # refused. None of that depended on the threshold.

    # Write one row per request to ci_meta.copilot_access_log: principal, role,
    # denial reason, trace id. Holds the one thing MLflow structurally cannot --
    # the REAL principal_id, since the trace stores a hash.
    #
    # Treat this as a compliance setting, not a performance one. Row-level security
    # used to be the structural proof that access held; with it withdrawn this log
    # is the only durable record of who saw what.
    ci_access_log_enabled: bool = Field(default=True, alias="CI_ACCESS_LOG_ENABLED")

    # Canonical Postgres connection string for this project.
    # Prefer POSTGRES_URI going forward. DATABASE_URL is accepted only for backward compatibility.
    postgres_uri: str = Field(
        default="postgresql://app:app@postgres:5432/customer_intelligence",
        validation_alias=AliasChoices("POSTGRES_URI", "DATABASE_URL"),
    )

    # ---------------------------------------------------------------------
    # LLM provider switch
    # ---------------------------------------------------------------------
    # Supported:
    #   LLM_PROVIDER=gemini
    #   LLM_PROVIDER=openai
    #
    # gemini = Google Gemini through Vertex AI
    # openai = OpenAI directly through OPENAI_API_KEY
    # ---------------------------------------------------------------------
    llm_provider: Literal["gemini", "vertex", "openai"] = Field(
        default="gemini",
        alias="LLM_PROVIDER",
    )

    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE")
    llm_timeout_seconds: int = Field(default=120, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")

    # ---------------------------------------------------------------------
    # Gemini / Vertex AI
    # ---------------------------------------------------------------------
    google_cloud_project: str = Field(default="", alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(
        default="asia-southeast1",
        alias="GOOGLE_CLOUD_LOCATION",
    )
    google_genai_use_vertexai: bool = Field(
        default=True,
        alias="GOOGLE_GENAI_USE_VERTEXAI",
    )
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    # ---------------------------------------------------------------------
    # OpenAI
    # ---------------------------------------------------------------------
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.4-mini", alias="OPENAI_MODEL")

    # ---------------------------------------------------------------------
    # Glossary source
    # ---------------------------------------------------------------------
    # postgres = read the governed glossary from the serving DB (copilot_glossary).
    # csv      = read directly from the seed CSV (legacy / offline / bootstrap).
    #
    # In `postgres` mode the CSV is still used as a safety fallback when the
    # copilot_glossary table is missing or empty (for example before the loader
    # has run), so a fresh deploy never comes up with an empty glossary.
    glossary_source: Literal["postgres", "csv"] = Field(
        default="postgres",
        alias="GLOSSARY_SOURCE",
    )

    # Glossary seed CSV. Only read at runtime as the bootstrap fallback when
    # ci_meta.copilot_glossary is missing or empty. product_catalog.csv and
    # store_name.csv are not read by the serving app at all -- they are inputs to
    # load_reference_data.py, configured on the seeding job.
    glossary_csv_path: str = Field(
        default="/app/data/v3_column_glossary.csv",
        alias="GLOSSARY_CSV_PATH",
    )

    # Metric registry seed. Same fallback rule as the glossary: read from
    # ci_meta.metric_definition at runtime, drop to the CSV if it is empty.
    metric_csv_path: str = Field(
        default="/app/data/v3_metric_definition_seed.csv",
        alias="METRIC_CSV_PATH",
    )

    # ---------------------------------------------------------------------
    # LangGraph checkpoint memory
    # ---------------------------------------------------------------------
    checkpoint_postgres_uri: str = Field(
        default="postgresql://app:app@postgres:5432/copilot_checkpoint",
        validation_alias=AliasChoices("CHECKPOINT_POSTGRES_URI", "LANGGRAPH_CHECKPOINT_POSTGRES_URI"),
    )
    checkpoint_setup_on_startup: bool = Field(
        default=True,
        alias="CHECKPOINT_SETUP_ON_STARTUP",
    )
    langgraph_checkpoint_enabled: bool = Field(
        default=True,
        alias="LANGGRAPH_CHECKPOINT_ENABLED",
    )
    checkpoint_pool_max_size: int = Field(
        default=10,
        alias="CHECKPOINT_POOL_MAX_SIZE",
    )


    # ---------------------------------------------------------------------
    # MLflow / OpenTelemetry observability
    # ---------------------------------------------------------------------
    mlflow_tracking_enabled: bool = Field(default=True, alias="MLFLOW_TRACKING_ENABLED")
    mlflow_tracking_uri: str = Field(default="", alias="MLFLOW_TRACKING_URI")
    mlflow_experiment_name: str = Field(
        default="customer-intelligence-copilot",
        alias="MLFLOW_EXPERIMENT_NAME",
    )
    # MLFLOW_TRACKING_USERNAME / MLFLOW_TRACKING_PASSWORD are read by the mlflow
    # client straight from the environment, so they are passed through in
    # docker-compose but deliberately not declared here.
    # Autolog the Gemini SDK, not LangChain. This app talks to Vertex through
    # google-genai directly and its graph nodes emit explicit mlflow_span()
    # wrappers, so there was never a LangChain Runnable to instrument --
    # `mlflow.langchain.autolog()` only ever raised ModuleNotFoundError at
    # startup ("No module named 'langchain'"), because `langchain` is not a
    # dependency; langgraph brings in langchain-core alone.
    mlflow_gemini_autolog_enabled: bool = Field(
        default=True,
        alias="MLFLOW_GEMINI_AUTOLOG_ENABLED",
    )

    # ---------------------------------------------------------------------
    # Currency
    # ---------------------------------------------------------------------
    # Every money column in ci_core is denominated in one currency -- there is no
    # per-row currency column and no FX table -- so the unit is a deployment
    # constant, not a property of a result row.
    #
    # Both values exist because they are read by different consumers: the symbol
    # goes into the narrative answer an LLM writes, the ISO code goes into
    # ChartSpec.currency so a frontend can format numbers with its own locale
    # rules instead of parsing prose. Neither is inferred from the data.
    currency_code: str = Field(default="MYR", alias="CURRENCY_CODE")
    currency_symbol: str = Field(default="RM", alias="CURRENCY_SYMBOL")

    # ---------------------------------------------------------------------
    # Query / response limits
    # ---------------------------------------------------------------------
    max_query_rows: int = Field(default=500, alias="MAX_QUERY_ROWS")
    chat_history_limit: int = Field(default=12, alias="CHAT_HISTORY_LIMIT")
    max_result_preview_rows: int = Field(default=120, alias="MAX_RESULT_PREVIEW_ROWS")
    stream_chunk_size: int = Field(default=120, alias="STREAM_CHUNK_SIZE")
    max_sql_retries: int = Field(default=2, alias="MAX_SQL_RETRIES")
    api_timeout_seconds: int = Field(default=180, alias="API_TIMEOUT_SECONDS")
    sql_timeout_seconds: int = Field(default=60, alias="SQL_TIMEOUT_SECONDS")

    # ---------------------------------------------------------------------
    # Entity resolution
    # ---------------------------------------------------------------------
    # Scoring thresholds for the span matcher. Exact dictionary hits always score
    # 100; these govern the fuzzy fallback only.
    #
    # lookup_resolution_enabled is gone because the new resolver does not make
    # the old per-turn LLM extraction call. The slot cap remains configurable as
    # a product guardrail for SQL/planner complexity.
    lookup_max_slots_per_question: int = Field(
        default=2,
        validation_alias=AliasChoices(
            "LOOKUP_MAX_SLOTS_PER_QUESTION",
            "MAX_LOOKUP_SLOT",
        ),
    )
    # Lowest score worth showing a user as "did you mean". Below this the phrase
    # is reported as unmatched instead of guessed at.
    lookup_suggest_score: float = Field(default=62.0, alias="LOOKUP_SUGGEST_SCORE")
    # Lowest score a fuzzy match may resolve at without asking. Anything between
    # the two becomes a clarification question, never a silent SQL filter.
    lookup_confident_score: float = Field(default=88.0, alias="LOOKUP_CONFIDENT_SCORE")
    # A best candidate this close to its runner-up is a tie, so ask.
    lookup_ambiguity_margin: float = Field(default=4.0, alias="LOOKUP_AMBIGUITY_MARGIN")
    # Only candidates within this many points of the best are offered as options.
    # Without it, everything above the floor was listed -- which is how a store
    # sharing one syllable ended up as option 2 of 5.
    lookup_option_window: float = Field(default=10.0, alias="LOOKUP_OPTION_WINDOW")
    # Most options one phrase may offer. Read in two places -- the matcher, which
    # decides how many candidates a span keeps, and the resolver, which renders the
    # numbered list -- and they must agree. They used to be separate literals, 5 and
    # 6, so "fashion" showed five of its nine equally-scoring matches and nothing
    # said the other four existed.
    #
    # Raising it does not widen what matches; lookup_option_window and the
    # one-family rule still decide that. It only stops the survivors being cut.
    lookup_max_options: int = Field(default=10, alias="LOOKUP_MAX_OPTIONS")

    # LOOKUP_MIN_SCORE and LOOKUP_TRGM_FLOOR are gone. The first conflated
    # "worth suggesting" with "safe to filter on"; those are now two settings.
    # The second configured a pg_trgm query that no longer exists -- it read the
    # same RLS-scoped table the in-memory dictionary is built from, so it could
    # only return rows already in hand.

    @staticmethod
    def _to_asyncpg_url(url: str) -> str:
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql+psycopg://"):
            return url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @staticmethod
    def _to_psycopg_url(url: str) -> str:
        if url.startswith("postgresql+asyncpg://"):
            return url.replace("postgresql+asyncpg://", "postgresql://", 1)
        if url.startswith("postgresql+psycopg://"):
            return url.replace("postgresql+psycopg://", "postgresql://", 1)
        return url

    @property
    def async_database_url(self) -> str:
        return self._to_asyncpg_url(self.postgres_uri)

    @property
    def psycopg_database_url(self) -> str:
        return self._to_psycopg_url(self.postgres_uri)

    @property
    def checkpoint_psycopg_database_url(self) -> str:
        return self._to_psycopg_url(self.checkpoint_postgres_uri)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
