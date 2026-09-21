# app/service/glossary_service.py
from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, List

import pandas as pd

from app.core.logging import Logger
from app.utils.text_utils import clean_text_value, normalize_lookup_text, parse_enum_values, safe_schema_name

from app.service.business_alias_service import (
    expand_query_with_business_aliases,
    format_business_alias_context,
    get_alias_terms_for_field,
)
from app.core import settings
from app.service.table_registry import (
    FORBIDDEN_TABLES,
    JOINABLE_DIMENSIONS,
    TABLE_PROFILES,
    format_table_profile,
)
from app.db.v3_ddl import AGGREGATE_VIEWS, MEMBER_SPLITS, MONEY_COLUMNS, PERSONA_VIEWS

# The glossary CSV documents BASE tables (fact_sales_daily, agg_sales_daily...),
# because that is the schema of record. The planner may only reference the persona
# VIEWS over them, since the search_path resolves each view to the caller's role
# level. So every base-table row is surfaced under its view name.
#
# Getting this wrong is silent and severe: filtering ingest through
# is_forbidden_table() -- which correctly rejects base tables in generated SQL --
# discarded all seven fact tables from the glossary, leaving the planner with only
# the dimensions.
#
# ONE BASE TABLE CAN FEED MORE THAN ONE VIEW, which is why the values are tuples.
# This was a dict of single view names, built by inverting PERSONA_VIEWS -- and
# fact_sales_daily is the base of both v_sales_daily and v_sales_store_monthly, so
# the second entry overwrote the first. Every atomic sales column was filed under
# the monthly view, and v_sales_daily -- the only view with daypart and payment
# type -- ended up with no columns at all. An empty column set does not fail: the
# validator's column-ownership check treats "no columns known" as "not my business"
# and skips, so the whole thing was invisible.
_BASE_TO_VIEWS: dict[str, tuple[str, ...]] = {}
for _view, _base in PERSONA_VIEWS.items():
    _BASE_TO_VIEWS[_base] = _BASE_TO_VIEWS.get(_base, ()) + (_view,)


# An aggregating view does NOT project its base table's columns. v_sales_store_monthly
# groups fact_sales_daily to the month: calendar_date, daypart, payment_type,
# line_item_count and customer_count are not on it, month_start_date and the rank
# columns are, and telling the planner otherwise produces SQL the database rejects
# -- or, for customer_count, a number that would have been wrong if it existed.
#
# Projection views take their base table's columns unchanged, so they are absent
# here and mapped to None by _view_projection().
_AGGREGATE_VIEW_COLUMNS: dict[str, frozenset[str]] = {
    view: frozenset(
        alias
        for _, alias in (*spec.grain, *spec.measures, *spec.money, *spec.windows)
    )
    for view, spec in AGGREGATE_VIEWS.items()
}

logger = Logger.get_logger(__name__)

# Columns present in the glossary but never useful to a query author.
SYSTEM_HIDDEN_COLUMNS = {"updated_at", "created_at"}

# Canonical column names selected from the governed glossary table. Ordered to
# preserve column layout within each table for schema-context rendering.
_GLOSSARY_DB_COLUMNS = (
    "table_name",
    "column_name",
    "table_title",
    "data_type",
    "grain",
    "enum_values",
    "remarks",
    "description",
    "lookup_resolution_mode",
    "lookup_resolution_scope",
    "lookup_reference",
    # v3 governance columns. Without these the service cannot filter the planner's
    # schema context by role, and an executive would be shown money columns their
    # view does not have.
    "metric_class",
    "min_role_level",
    "is_identity",
    "aggregation_rule",
)



def _first(row: Any, *names: str) -> str:
    for name in names:
        value = clean_text_value(row.get(name))
        if value:
            return value
    return ""



# Columns a persona VIEW projects that its base table does not have, so the
# glossary has no row to file under them. Two kinds, both real:
#
#   member_*  -- MEMBER_SPLITS generates these on every sales view so the planner
#                never writes FILTER (WHERE customer_type = 'Member') by hand.
#   the rest  -- an aggregating view's own output: month_start_date where the base
#                has calendar_date, and the rank columns computed over the group.
#
# Without them the planner is told to use member_transaction_count (planner_agent
# says so explicitly) while the validator's column-ownership check has never heard
# of it, and rejects the query it asked for.
#
# `_source_column` names the base column whose type, class and role level are
# inherited; None means the entry stands alone and states its own.


def _derived_column_specs(view: str) -> list[dict[str, Any]]:
    """Raw glossary rows for the columns only `view` has. See above."""
    base = PERSONA_VIEWS.get(view)
    if base is None:
        return []

    specs: list[dict[str, Any]] = []

    # An aggregating view builds its own member splits inside the GROUP BY, so its
    # spec is the whole truth about what it projects. Only the projection views get
    # the generated MEMBER_SPLITS columns on top of their base table.
    member_splits = () if view in AGGREGATE_VIEWS else MEMBER_SPLITS.get(base, ())

    for source, alias, needs_money in member_splits:
        specs.append(
            {
                "table_name": view,
                "column_name": alias,
                "_source_column": source,
                "description": (
                    f"{source} for member transactions only; 0 otherwise. Generated by "
                    "the view, so membership penetration is SUM(member_x) / SUM(x) "
                    "with no FILTER clause."
                ),
                "min_role_level": "HOD" if needs_money else None,
            }
        )

    spec = AGGREGATE_VIEWS.get(view)
    if spec is None:
        return specs

    # An aggregating view's money columns are the ones in its OWN spec: those are
    # exactly what the executive view omits. MONEY_COLUMNS is the wider "what does
    # an EXEC not get" list and is used for the window columns below, which have no
    # spec entry of their own.
    money = {alias for _, alias in spec.money} | set(MONEY_COLUMNS.get(view, ()))
    for _expr, alias in (*spec.grain, *spec.measures, *spec.money):
        if alias == "month_start_date":
            specs.append(
                {
                    "table_name": view,
                    "column_name": alias,
                    "_source_column": "calendar_date",
                    "data_type": "date",
                    "metric_class": "dimension",
                    "aggregation_rule": "NONE",
                    "description": (
                        "Month this row aggregates, always the 1st. The view groups "
                        f"{spec.base_table} by month; there is no daily column on it."
                    ),
                }
            )
        elif alias.startswith("member_"):
            specs.append(
                {
                    "table_name": view,
                    "column_name": alias,
                    "_source_column": alias.removeprefix("member_"),
                    "description": (
                        f"{alias.removeprefix('member_')} for member transactions "
                        "only, summed over the month."
                    ),
                    "min_role_level": "HOD" if alias in money else None,
                }
            )

    for _expr, alias in spec.windows:
        specs.append(
            {
                "table_name": view,
                "column_name": alias,
                "_source_column": None,
                "data_type": "integer" if alias.endswith("rank") else "boolean",
                # Derived from revenue, so an executive does not get it -- the view
                # they read does not compute the window at all.
                "metric_class": "measure_money" if alias in money else "dimension",
                "min_role_level": "HOD" if alias in money else "EXEC",
                "aggregation_rule": "NEVER_AGGREGATE",
                "description": (
                    "Sales rank of this store within its month, OpCo and category. "
                    "Computed by the view over the whole group, so it is already "
                    "correct without a window function in the query."
                    if alias.endswith("rank")
                    else "True when this row's category_sales_rank is 100 or better."
                ),
            }
        )

    return specs


def _rows_by_view(rows: list[dict[str, Any]]) -> Any:
    """(view_name, row) for every glossary row, expanded across the views.

    Three things happen here, and all three used to be one lossy dict lookup:

      - a base table is surfaced under EVERY view built on it, not just the last
        one PERSONA_VIEWS happened to name;
      - a column is dropped for a view that does not project it, so the planner is
        never told v_sales_store_monthly has a daypart;
      - the columns a view generates itself are added, since no base table has a
        row for them.

    Rows for tables that are not base tables -- the joinable dimensions -- pass
    through under their own name.
    """
    for row in rows:
        base = _first(row, "table_name", "Table_Name")
        column = _first(row, "column_name", "Field_Name", "field_name")
        if not base or not column:
            continue

        for view in _BASE_TO_VIEWS.get(base, (base,)):
            projected = _AGGREGATE_VIEW_COLUMNS.get(view)
            if projected is not None and column not in projected:
                continue
            yield view, row

    # A derived column inherits its type, metric class and role level from the base
    # column it is computed from, so a money column stays HOD without restating it.
    by_key = {
        (
            _first(row, "table_name", "Table_Name"),
            _first(row, "column_name", "Field_Name", "field_name"),
        ): row
        for row in rows
    }

    for view in PERSONA_VIEWS:
        base = PERSONA_VIEWS[view]
        for spec in _derived_column_specs(view):
            source = spec.pop("_source_column", None)
            inherited = dict(by_key.get((base, source)) or {}) if source else {}
            inherited.pop("ordinal", None)
            inherited.update({k: v for k, v in spec.items() if v is not None})
            yield view, inherited


def _pretty_table_title(table_name: str) -> str:
    """Human-readable title for a v3 view or table name.

    v2 stripped a `gold_customer_360_` prefix and a `_v2` suffix. v3 names are
    already short (`v_sales_summary_daily`, `dim_product_category`), so this only
    drops the type prefix and title-cases the rest.
    """
    cleaned = table_name
    for prefix in ("v_", "fact_", "agg_", "bridge_", "dim_", "copilot_"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned.replace("_", " ").title()


def _add_alias(aliases: set[str], value: str | None) -> None:
    norm = normalize_lookup_text(value)
    if not norm:
        return

    aliases.add(norm)

    compact = norm.replace(" ", "")
    if len(compact) >= 4 and compact != norm:
        aliases.add(compact)


def derive_schema_alias_terms(
    field_name: str,
    *,
    description: str = "",
    data_type: str = "",
) -> set[str]:
    """Derive searchable schema aliases from glossary metadata.

    This keeps schema-wording aliases data-driven. For example, a boolean field
    named `has_<value>_line` with a description saying the customer purchased
    from a product line automatically becomes searchable by `<value> line`, the
    compact `<value>line` form, and similar purchase/cohort wording.
    """
    field_norm = normalize_lookup_text(field_name)
    if not field_norm:
        return set()

    aliases: set[str] = set()
    _add_alias(aliases, field_norm)

    tokens = field_norm.split()
    base_tokens = list(tokens)
    while base_tokens and base_tokens[0] in {"has", "is", "was", "were", "had"}:
        base_tokens = base_tokens[1:]

    base = " ".join(base_tokens).strip()
    if base and base != field_norm:
        _add_alias(aliases, base)

    phrase_subjects = {base} if base else set()

    if len(base_tokens) >= 2 and base_tokens[-1] == "line":
        line_subject = " ".join(base_tokens[:-1]).strip()
        if line_subject:
            _add_alias(aliases, line_subject)
            phrase_subjects.add(line_subject)

    # Generic field-name pattern: is_pure_<x>_only should also match
    # `<x> only` and `only <x>` without maintaining a per-field alias list.
    if len(base_tokens) >= 3 and base_tokens[0] == "pure" and base_tokens[-1] == "only":
        middle = " ".join(base_tokens[1:-1]).strip()
        if middle:
            for phrase in [f"{middle} only", f"only {middle}"]:
                _add_alias(aliases, phrase)
                phrase_subjects.add(phrase)

    desc_tokens = set(normalize_lookup_text(description).split())
    data_type_norm = normalize_lookup_text(data_type)
    purchase_words = {
        "bought",
        "buy",
        "buyer",
        "buyers",
        "purchase",
        "purchased",
        "purchases",
    }

    is_booleanish = "boolean" in data_type_norm or (tokens and tokens[0] in {"has", "is"})
    describes_purchase = bool(desc_tokens & purchase_words)

    if is_booleanish and describes_purchase:
        for subject in {s for s in phrase_subjects if s}:
            _add_alias(aliases, f"{subject} buyers")
            _add_alias(aliases, f"bought {subject}")
            _add_alias(aliases, f"bought from {subject}")
            _add_alias(aliases, f"purchased {subject}")
            _add_alias(aliases, f"purchased from {subject}")

            compact_subject = normalize_lookup_text(subject).replace(" ", "")
            if compact_subject and compact_subject != normalize_lookup_text(subject):
                _add_alias(aliases, f"{compact_subject} buyers")
                _add_alias(aliases, f"bought {compact_subject}")
                _add_alias(aliases, f"bought from {compact_subject}")
                _add_alias(aliases, f"purchased {compact_subject}")
                _add_alias(aliases, f"purchased from {compact_subject}")

    return aliases


class GlossaryService:
    def __init__(self) -> None:
        self.dictionary_rows: List[Dict[str, Any]] = []
        self.rows_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.columns_by_table: dict[str, set[str]] = defaultdict(set)
        self.all_tables: set[str] = set()

        self.source = "unknown"
        self._ingest(self._load_source_rows())

    # ------------------------------------------------------------------
    # Source loading
    # ------------------------------------------------------------------
    def _load_source_rows(self) -> list[dict[str, Any]]:
        """Load raw glossary rows from the configured source.

        Runtime source of truth is Postgres (`copilot_glossary`). The seed CSV is
        used only when explicitly requested, or as a safety fallback when the DB
        table is missing/empty (for example before the loader has run), so the
        service never comes up with an empty glossary.
        """
        if settings.glossary_source == "csv":
            self.source = "csv"
            return self._load_rows_from_csv()

        try:
            rows = self._load_rows_from_db()
        except Exception as exc:  # noqa: BLE001 - degrade to CSV on any DB error
            logger.warning(
                "Glossary DB read failed (%s); falling back to seed CSV %s",
                exc,
                settings.glossary_csv_path,
            )
            self.source = "csv-fallback"
            return self._load_rows_from_csv()

        if rows:
            self.source = "postgres"
            return rows

        logger.warning(
            "copilot_glossary is empty; falling back to seed CSV %s. "
            "Run `python -m app.scripts.load_glossary` to populate it.",
            settings.glossary_csv_path,
        )
        self.source = "csv-fallback"
        return self._load_rows_from_csv()

    def _load_rows_from_db(self) -> list[dict[str, Any]]:
        from app.db.postgres import get_pg_conn

        # ci_meta, not POSTGRES_SCHEMA: the governed glossary moved with the rest
        # of the v3 metadata.
        schema = safe_schema_name(settings.ci_meta_schema)
        columns = ", ".join(_GLOSSARY_DB_COLUMNS)
        sql = (
            f"SELECT {columns} FROM {schema}.copilot_glossary "
            "WHERE is_active = true ORDER BY table_name, ordinal, id"
        )

        rows: list[dict[str, Any]] = []
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                col_names = [desc[0] for desc in cur.description]
                for record in cur.fetchall():
                    row = dict(zip(col_names, record))
                    # JSONB comes back as a Python list; re-serialize so the
                    # shared ingest path parses it identically to the CSV form.
                    enum_value = row.get("enum_values")
                    if isinstance(enum_value, (list, dict)):
                        row["enum_values"] = json.dumps(enum_value, ensure_ascii=False)
                    rows.append(row)
        return rows

    def _load_rows_from_csv(self) -> list[dict[str, Any]]:
        df = pd.read_csv(settings.glossary_csv_path).rename(columns=lambda c: str(c).strip())
        return df.to_dict("records")

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def _ingest(self, rows: list[dict[str, Any]]) -> None:
        for view_name, row in _rows_by_view(rows):
            # Supports both the new canonical glossary and the old CSV column names.
            field_name = _first(row, "column_name", "Field_Name", "field_name")

            # Metadata tables (copilot_glossary, metric_definition, ...) describe
            # the copilot's own plumbing and are not queryable subject matter.
            if view_name in FORBIDDEN_TABLES or view_name.startswith("copilot_"):
                continue

            queryable = view_name in set(PERSONA_VIEWS) or view_name in set(JOINABLE_DIMENSIONS)
            if not queryable:
                continue

            table_name = view_name
            self.all_tables.add(table_name)
            self.columns_by_table[table_name].add(field_name)

            if field_name.lower() in SYSTEM_HIDDEN_COLUMNS:
                continue

            table_title = _first(row, "table_title", "Table_Title", "Table_Label") or _pretty_table_title(table_name)
            description = _first(row, "description", "Description")
            data_type = _first(row, "data_type", "Data_Type")
            grain = _first(row, "grain", "Grain")
            enum_values = parse_enum_values(_first(row, "enum_values", "Enum_Values", "sample_values", "Sample_Values"))

            field_name_norm = normalize_lookup_text(field_name)
            description_norm = normalize_lookup_text(description)
            table_norm = normalize_lookup_text(table_name)
            table_title_norm = normalize_lookup_text(table_title)
            enum_norm = normalize_lookup_text(" ".join(enum_values))

            aliases = {
                field_name_norm,
                field_name_norm.replace(" ", ""),
                f"{table_norm} {field_name_norm}".strip(),
                f"{table_title_norm} {field_name_norm}".strip(),
            }

            aliases.update(
                derive_schema_alias_terms(
                    field_name,
                    description=description,
                    data_type=data_type,
                )
            )

            aliases.update(
                normalize_lookup_text(term)
                for term in get_alias_terms_for_field(field_name)
                if normalize_lookup_text(term)
            )

            original_field_name = _first(row, "original_field_name", "Original_Field_Name")
            source_sheet = _first(row, "source_sheet", "Source_Sheet")
            remarks = _first(row, "remarks", "Remarks")
            lookup_resolution_mode = _first(row, "lookup_resolution_mode", "Lookup_Resolution_Mode")
            lookup_resolution_scope = _first(row, "lookup_resolution_scope", "Lookup_Resolution_Scope")
            lookup_reference = _first(row, "lookup_reference", "Lookup_Reference")

            # v3 governance columns. These are what let the planner's schema
            # context be filtered by role and category grant BEFORE the model ever
            # sees a column name -- a money column an executive cannot query is
            # simply absent from their prompt, rather than present with a caveat
            # the model might ignore.
            metric_class = _first(row, "metric_class", "Metric_Class")
            min_role_level = (_first(row, "min_role_level", "Min_Role_Level") or "EXEC").upper()
            aggregation_rule = _first(row, "aggregation_rule", "Aggregation_Rule")

            def _flag(*names: str) -> bool:
                return str(_first(row, *names)).strip().upper() in {"TRUE", "1", "YES", "Y"}

            is_identity = _flag("is_identity", "Is_Identity")

            item = {
                "table_name": table_name,
                "table_title": table_title,
                "field_name": field_name,
                "column_name": field_name,
                "field_name_normalized": field_name_norm,
                "description": description,
                "description_normalized": description_norm,
                "data_type": data_type,
                "grain": grain,
                "enum_values": enum_values,
                "sample_values": enum_values,
                "enum_values_normalized": enum_norm,
                "original_field_name": original_field_name,
                "source_sheet": source_sheet,
                "remarks": remarks,
                "lookup_resolution_mode": lookup_resolution_mode,
                "lookup_resolution_scope": lookup_resolution_scope,
                "lookup_reference": lookup_reference,
                "metric_class": metric_class,
                "min_role_level": min_role_level,
                "aggregation_rule": aggregation_rule,
                "is_identity": is_identity,
                "aliases": list(aliases),
            }

            self.dictionary_rows.append(item)
            self.rows_by_table[table_name].append(item)

    def get_allowed_full_tables(self) -> set[str]:
        """Table names the planner may reference.

        Bare names in v3: the search_path resolves each view to the caller's
        persona schema, so a qualified name would defeat the role split.
        """
        return set(self.all_tables)

    # ------------------------------------------------------------------
    # Role / grant scoping
    # ------------------------------------------------------------------

    def visible_rows(self, principal=None) -> list[dict[str, Any]]:
        """Glossary rows this caller may be shown.

        Filters two ways:
          - min_role_level: an EXEC caller never sees a money column, so the
            planner cannot reference one and get a hard SQL error from the
            executive view.
          - identity columns are annotated rather than hidden. The planner needs to
            know customer_key exists in order to write COUNT(DISTINCT customer_key);
            hiding it would make every customer count impossible.
        """
        if principal is None:
            return self.dictionary_rows

        can_see_money = bool(getattr(principal, "can_see_money", True))
        if can_see_money:
            return self.dictionary_rows

        return [
            row
            for row in self.dictionary_rows
            if str(row.get("min_role_level") or "EXEC").upper() != "HOD"
        ]

    def hidden_money_columns(self, principal=None) -> list[str]:
        """Money columns withheld from this caller, for the refusal message."""
        if principal is None or getattr(principal, "can_see_money", True):
            return []
        return sorted(
            {
                str(row.get("field_name"))
                for row in self.dictionary_rows
                if str(row.get("min_role_level") or "EXEC").upper() == "HOD"
            }
        )

    def identity_columns(self) -> set[str]:
        return {
            str(row.get("field_name"))
            for row in self.dictionary_rows
            if row.get("is_identity")
        }

    def search(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        q_raw = expand_query_with_business_aliases(query or "")
        q = normalize_lookup_text(q_raw)
        q_compact = q.replace(" ", "")
        q_tokens = set(q.split())
        scored: list[dict] = []

        for row in self.dictionary_rows:
            field_norm = row["field_name_normalized"]
            desc_norm = row["description_normalized"]
            enum_norm = row.get("enum_values_normalized", "")
            table_norm = normalize_lookup_text(row.get("table_name", ""))
            table_title_norm = normalize_lookup_text(row.get("table_title", ""))
            aliases = set(row.get("aliases", []))

            haystack = " ".join(
                [
                    field_norm,
                    desc_norm,
                    enum_norm,
                    table_norm,
                    table_title_norm,
                    normalize_lookup_text(row.get("data_type", "")),
                    normalize_lookup_text(row.get("grain", "")),
                    normalize_lookup_text(row.get("original_field_name", "")),
                    normalize_lookup_text(row.get("source_sheet", "")),
                    normalize_lookup_text(row.get("remarks", "")),
                    normalize_lookup_text(row.get("lookup_resolution_mode", "")),
                    normalize_lookup_text(row.get("lookup_resolution_scope", "")),
                    normalize_lookup_text(row.get("lookup_reference", "")),
                    normalize_lookup_text(" ".join(aliases)),
                ]
            ).strip()

            score = 0

            if q == field_norm:
                score += 220
            if q_compact == field_norm.replace(" ", ""):
                score += 200
            if q and q in haystack:
                score += 80
            if q and q in enum_norm:
                score += 70
            if q and q in table_norm:
                score += 60
            if q and q in table_title_norm:
                score += 60

            for alias in aliases:
                if q == alias:
                    score += 170
                if q_compact == alias.replace(" ", ""):
                    score += 150
                if alias and alias in q:
                    score += 80

            field_tokens = set(field_norm.split())
            desc_tokens = set(desc_norm.split())
            enum_tokens = set(enum_norm.split())
            table_tokens = set(table_norm.split())
            title_tokens = set(table_title_norm.split())
            alias_tokens = set(normalize_lookup_text(" ".join(aliases)).split())

            score += len(q_tokens & field_tokens) * 25
            score += len(q_tokens & desc_tokens) * 8
            score += len(q_tokens & enum_tokens) * 12
            score += len(q_tokens & table_tokens) * 12
            score += len(q_tokens & title_tokens) * 12
            score += len(q_tokens & alias_tokens) * 18

            if {"month", "monthly", "daily", "day", "trend", "period", "jan", "feb",
                "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                "dec"} & q_tokens and row["field_name"] in {
                "calendar_date",
                "month_start_date",
            }:
                score += 80

            if {"morning", "afternoon", "evening", "daypart", "time"} & q_tokens and row[
                "field_name"
            ] in {"daypart", "daypart_seq"}:
                score += 80

            if {"store", "branch", "outlet"} & q_tokens and row["field_name"] in {
                "store_id",
                "store_name",
                "primary_store_id",
                "primary_store_name",
                "store_location",
                "primary_store_location",
            }:
                score += 80

            if {"category", "product"} & q_tokens and row["field_name"] in {
                "product_category_id",
                "product_category",
                "product_line",
                "primary_product_line",
                "product_division",
                "product_group",
            }:
                score += 80

            if {"revenue", "sales", "amount", "gmv"} & q_tokens and row["field_name"] in {
                "member_revenue",
                "non_member_revenue",
                "total_revenue",
                "total_gmv",
                "nm_gmv_amount",
                "total_category_sales_amount",
            }:
                score += 80

            if {"tier"} & q_tokens and row["field_name"] == "membership_tier":
                score += 80

            if {"segment", "repeat", "repeated", "lapsed", "occasional"} & q_tokens and row["field_name"] in {
                "family_segment",
                "repeat_purchase_segment",
                "purchase_ratio_segment",
                "lifecycle_stage",
            }:
                score += 80

            if score > 0:
                scored.append({**row, "_score": score})

        scored.sort(key=lambda x: (-x["_score"], x["table_name"], x["field_name"]))

        clean_rows = []
        for item in scored[:limit]:
            clean_rows.append(
                {
                    k: v
                    for k, v in item.items()
                    if k not in {"_score", "field_name_normalized", "description_normalized", "enum_values_normalized", "aliases"}
                }
            )
        return clean_rows

    def get_relevant_schema_context(self, query: str, limit: int = 24) -> str:
        s = settings
        expanded_query = expand_query_with_business_aliases(query or "")
        rows = self.search(expanded_query, limit=limit)

        if not rows:
            rows = [
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"field_name_normalized", "description_normalized", "enum_values_normalized", "aliases"}
                }
                for row in self.dictionary_rows[:limit]
            ]

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["table_name"]].append(row)

        parts: list[str] = [
            "Important SQL rules:",
            "- Use only tables listed in this schema context.",
            "- Reference every table by its BARE name, never schema-qualified. The",
            "  database resolves each view to the variant this caller is allowed to",
            "  see; qualifying it would bypass that and is rejected.",
            "- Use PostgreSQL syntax only. No BigQuery syntax, no backticks, no",
            "  project.dataset.table references.",
            "- Do not use SAFE_CAST, SAFE.PARSE_DATE, FORMAT_DATE, FORMAT_TIMESTAMP,",
            "  QUALIFY, UNNEST, STRUCT, INT64, FLOAT64, BOOL, or STRING.",
            "- Sales views are DAILY: filter `calendar_date`, and use `daypart` /",
            "  `daypart_seq` for Morning / Afternoon / Evening. Order dayparts by",
            "  daypart_seq, never alphabetically.",
            "- Customer views are MONTHLY: filter `month_start_date`, always the 1st.",
            "- Always filter a period. Every serving table is period-grained.",
            "- Derive year, month, weekday and day-of-year with EXTRACT or date_trunc",
            "  on the date column. There is NO date dimension: public-holiday and",
            "  fiscal-period questions are not answerable -- say so rather than",
            "  approximating them with calendar dates.",
            "- `customer_key` is an identity column. It must never reach the",
            "  OUTERMOST SELECT list or GROUP BY, and never sit inside an aggregate",
            "  other than COUNT(DISTINCT). Inside a CTE or subquery it is free:",
            "  group by it for a per-customer figure, then aggregate it away.",
            "- Never SUM or AVG `customer_count`. It is distinct customers at one row",
            "  grain only, so summing double-counts. Use COUNT(DISTINCT customer_key)",
            "  on a customer view for a period total.",
            "- ONE CUSTOMER OCCUPIES MANY ROWS on the customer views, so COUNT(*) is",
            "  NOT a customer count. v_customer_opco_monthly has one row per customer",
            "  PER OPCO per month; v_customer_category_monthly has one per leaf",
            "  category per store. Always COUNT(DISTINCT customer_key). Any COUNT",
            "  without DISTINCT on those views is rejected.",
            "- `age` and `active_opco_count` are per-CUSTOMER values repeated on every",
            "  row that customer occupies, so AVG or SUM over rows weights them by how",
            "  many OpCos the customer shops with. De-duplicate to one row per",
            "  customer first.",
            "- Compute every ratio and average at query time. No stored average or",
            "  percentage columns exist.",
            "",
            "Business alias / synonym rules:",
            format_business_alias_context(),
            "",
            "String/category filter rules:",
            "- For text or categorical values, normalize both sides.",
            "- Use LOWER(TRIM(CAST(alias.column_name AS TEXT))) = LOWER(TRIM('user value')).",
            "- For configured business aliases with multiple canonical values, use normalized IN conditions.",
            "- Use LIKE/ILIKE only when the user clearly gives an incomplete or fuzzy text value.",
            "- Apply this to category_name, customer_status_in_opco,",
            "  membership_tier, family_segment, lifecycle_stage,",
            "  purchase_ratio_segment, tenure_bucket, payment_type, daypart,",
            "  opco_name, and other text filters.",
            "- Never filter store_name from raw user text. Stores/branches must be",
            "  resolved through lookup context and filtered by store_id plus opco_code.",
            "- Do not normalize numeric, boolean, key, or date filters.",
            "- Prefer joining a key (store_id, category_key, opco_code) over matching",
            "  a name when the entity resolver already supplied the key.",
            "- Never ask the user for store_id/category_key values; these are internal",
            "  keys. User-facing clarification should use business names only.",
            "",
            "Lookup-backed field rules:",
            "- A field is lookup-backed only when its table_glossary `remarks` column has a value, for example `lookup: store master` or `lookup: product catalog`.",
            "- Do not treat a column as lookup-backed only because its name sounds like store/product/location/category; the `remarks` column is the source of truth.",
            "- Do not invent values for lookup-backed fields.",
            "- If resolved lookup context is supplied by the graph, use those canonical values.",
            "- If the user explicitly mentions a field whose remarks is blank, treat it as a normal glossary enum/text field instead of using lookup resolution.",
            "- If no resolved lookup context exists and the user provides an unclear lookup-backed value, ask clarification instead of guessing.",
            "",
            "Current table registry:",
        ]

        for table_name in TABLE_PROFILES:
            if table_name not in self.all_tables:
                continue
            parts.append("")
            parts.append(f"Table `{table_name}`:")
            profile_text = format_table_profile(table_name)
            if profile_text:
                parts.append(profile_text)

        parts.extend(
            [
                "",
                "Table routing rules:",
                "- Prefer the SMALLEST table that can answer the question.",
                "- v_sales_summary_daily is the default for sales performance,",
                "  membership penetration, daily trend and best-daypart questions.",
                "- Use v_sales_daily only when the question needs store detail,",
                "  payment type, or category depth below level 2.",
                "- Use v_sales_store_monthly for monthly store/category league tables",
                "  and category ranks. It has NO customer_count column -- distinct",
                "  customers by store come from v_customer_category_monthly.",
                "- Use v_customer_opco_monthly for any distinct-customer count or",
                "  customer attribute breakdown.",
                "- Use v_customer_category_monthly for 'how many customers bought",
                "  <category>' questions.",
                "- Cross-OpCo overlap is `active_opco_codes` on",
                "  v_customer_opco_monthly, not a separate view: the set of OpCos a",
                "  customer was active in THAT MONTH. Test it with",
                "  active_opco_codes @> ARRAY['<CODE>'].",
                "- Never join two sales views together. Pick the one at the right grain.",
                "- To combine a customer count with a category filter, join",
                "  v_customer_category_monthly to v_customer_opco_monthly on",
                "  customer_key AND month_start_date AND opco_code.",
                "",
                "Relevant matched table columns:",
            ]
        )

        for table_name, table_rows in grouped.items():
            table_title = table_rows[0].get("table_title") or table_name

            parts.append("")
            parts.append(f"Table `{table_name}` ({table_title}):")

            profile_text = format_table_profile(table_name)
            if profile_text:
                parts.append(profile_text)

            all_table_rows = [
                row
                for row in self.rows_by_table.get(table_name, [])
                if str(row.get("field_name", "")).lower() not in SYSTEM_HIDDEN_COLUMNS
            ]

            for row in all_table_rows:
                enum_values = row.get("enum_values") or []
                enum_text = ""
                if enum_values:
                    shown = ", ".join(enum_values[:12])
                    suffix = " ..." if len(enum_values) > 12 else ""
                    enum_text = f" Allowed/example values: {shown}{suffix}."

                lookup_text = ""
                if row.get("remarks"):
                    lookup_bits = ["enabled_by=remarks"]
                    if row.get("lookup_resolution_mode"):
                        lookup_bits.append(f"mode={row.get('lookup_resolution_mode')}")
                    if row.get("lookup_resolution_scope"):
                        lookup_bits.append(f"scope={row.get('lookup_resolution_scope')}")
                    if row.get("lookup_reference"):
                        lookup_bits.append(f"reference={row.get('lookup_reference')}")
                    lookup_text = f" Lookup-backed field ({', '.join(lookup_bits)})."

                remarks_text = f" Remarks: {row.get('remarks')}." if row.get("remarks") else ""

                parts.append(
                    f"- `{row['field_name']}` ({row.get('data_type') or 'unknown'}): "
                    f"{row.get('description') or 'no description'}{enum_text}{lookup_text}{remarks_text}"
                )

        blocked = ", ".join(FORBIDDEN_TABLES)
        parts.extend(
            [
                "",
                "Blocked old tables:",
                f"- Do not use these old table names: {blocked}",
            ]
        )

        return "\n".join(parts)


    def get_lookup_backed_rows(self, scope: str | None = None) -> list[dict[str, Any]]:
        """
        Return lookup-backed glossary rows.

        Source of truth: `remarks` must have a value.
        `lookup_resolution_scope` is optional metadata only; it is not required
        for a row to be considered lookup-backed.
        """
        rows: list[dict[str, Any]] = []
        for row in self.dictionary_rows:
            remarks = str(row.get("remarks") or "").strip()
            if not remarks:
                continue

            lookup_scope = str(row.get("lookup_resolution_scope") or "").strip()
            if scope and lookup_scope and lookup_scope != scope:
                continue

            rows.append(
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"field_name_normalized", "description_normalized", "enum_values_normalized", "aliases"}
                }
            )
        return rows

    def get_columns_for_table(self, table_name: str) -> set[str]:
        return set(self.columns_by_table.get(table_name, set()))

    def get_columns_for_full_table(self, full_table_name: str) -> set[str]:
        short_name = (full_table_name or "").split(".")[-1].replace('"', "").replace("`", "")
        return self.get_columns_for_table(short_name)

    def has_column(self, table_name: str, column_name: str) -> bool:
        return column_name in self.columns_by_table.get(table_name, set())

    def find_similar_columns(self, table_name: str, column_name: str, limit: int = 8) -> list[str]:
        target = normalize_lookup_text(column_name)
        target_tokens = set(target.split())
        scored: list[tuple[int, str]] = []

        for candidate in self.columns_by_table.get(table_name, set()):
            if candidate.lower() in SYSTEM_HIDDEN_COLUMNS:
                continue

            cand_norm = normalize_lookup_text(candidate)
            cand_tokens = set(cand_norm.split())
            score = 0

            if cand_norm == target:
                score += 1000
            if target and target in cand_norm:
                score += 120
            if cand_norm and cand_norm in target:
                score += 80
            score += len(target_tokens & cand_tokens) * 25

            if score > 0:
                scored.append((score, candidate))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [candidate for _, candidate in scored[:limit]]
    
    def find_tables_with_column(self, column_name: str) -> list[str]:
        """
        Return all glossary tables that contain the given column.

        Used by SQL validator retry feedback so the planner can switch tables
        instead of repeating the same invalid table/column pair.
        """
        column_name = (column_name or "").strip()
        if not column_name:
            return []

        matches: list[str] = []
        for table_name, columns in self.columns_by_table.items():
            if column_name in columns:
                matches.append(table_name)

        return sorted(matches)


@lru_cache(maxsize=1)
@lru_cache(maxsize=1)
def question_vocabulary() -> frozenset[str]:
    """Words that describe the SCHEMA rather than name a value in it.

    Three sources, because a user's question draws on all three:

      column names        lifecycle_stage -> lifecycle, stage
      metric names        avg_basket_size_value -> basket, size
      metric synonyms     "basket size", "spend per basket", "ATV"

    A synonym must NAME the metric and never give an example of it. Every word in
    one lands here, so `category_customers` carrying the synonym "fashion
    customers" put "fashion" in this set and made a real category name unguessable:
    "customers who stopped buying fashion" then matched BEDDING instead. Enum values
    are subtracted below, which covers the same mistake for segment and daypart
    values, but a category or store name cannot be -- the lookup catalogue is
    row-level-security scoped and unreadable from here.

    Used to keep the entity resolver from guessing. Every one of these words caused
    a real false positive by being fuzzy-matched to a product that happens to
    contain it: "stage" -> SCARLETEEN BRA STAGE 1, "segment" -> a category, "size"
    -> DOG APPAREL-L SIZE. They are describing what to measure or group by, not
    which value to filter on.

    Deliberately does NOT include enum values. "Elite" and "Churned" are exactly
    the values a user filters on, and they are in the lookup catalog as `enum` so
    that they resolve exactly instead of falling through to a fuzzy product match.

    Derived from data, so a new column or metric protects its own vocabulary and
    nobody has to predict which word bites next.
    """
    words: set[str] = set()

    try:
        glossary = get_glossary_service()
        for table in glossary.all_tables:
            for column in glossary.get_columns_for_table(table):
                words.update(normalize_lookup_text(column).split())
    except Exception:  # noqa: BLE001
        logger.debug("glossary unavailable for question vocabulary", exc_info=True)

    try:
        from app.service.metric_service import get_metric_service

        metrics = get_metric_service()
        for metric in metrics.metrics.values():
            for phrase in (metric.metric_key, metric.metric_name, *metric.synonyms):
                words.update(normalize_lookup_text(phrase).split())
    except Exception:  # noqa: BLE001
        logger.debug("metric registry unavailable for question vocabulary", exc_info=True)

    # Subtract anything that is also an enum VALUE.
    #
    # A value must stay resolvable, and the two sets do overlap in practice: the
    # metric `customers_by_segment` carries the synonym "elite premium growth mass"
    # -- a list of its values rather than a name for the metric -- which would have
    # excluded exactly the words the enum fix just made resolvable. Rather than
    # trusting every synonym in the seed data to be well-formed, the values win.
    #
    # Read from copilot_glossary.enum_values, which has no row-level security, so
    # this needs no role switch. The lookup catalog would, and cannot be read as the
    # login user at all.
    # Only a value that IS one word gives that word up. A word taken from inside a
    # multi-word value is not the value, and treating it as one disarms the guard
    # for everything else that word protects: adding "Silent Generation" to
    # generation_bucket released "generation", and "average age by generation
    # bucket" was immediately asked whether it meant the Silent Generation.
    #
    # Multi-word values lose nothing by staying out of this set, because the guard
    # governs GUESSING only -- exact matching reads the raw token stream, so
    # "silent generation" and "at risk" still resolve exactly.
    values: set[str] = set()
    try:
        glossary = get_glossary_service()
        for rows in glossary.rows_by_table.values():
            for row in rows:
                for value in row.get("enum_values") or []:
                    tokens = normalize_lookup_text(value).split()
                    if len(tokens) == 1:
                        values.add(tokens[0])
    except Exception:  # noqa: BLE001
        logger.debug("could not read enum values while building vocabulary", exc_info=True)

    # Column names are singular; questions are not. "brand_name" protects "brand"
    # and left "brands" to be guessed at, so "which top 5 BRANDS did members spend
    # most on" was asked whether it meant BRANDY or 16 BRANDS -- the plural of a
    # column name matched two products that merely contain the word.
    #
    # Naive plurals only. These are schema words either way, so over-generating a
    # form that is not real English costs nothing: it can only fail to match.
    kept = {w for w in words - values if len(w) > 2}
    plurals = {f"{w}s" for w in kept} | {f"{w}es" for w in kept if w.endswith(("s", "x", "ch", "sh"))}

    # A plural that is itself a documented VALUE stays resolvable -- the same rule
    # the singular set follows, and for the same reason.
    return frozenset(kept | (plurals - values))


def get_glossary_service() -> GlossaryService:
    return GlossaryService()
