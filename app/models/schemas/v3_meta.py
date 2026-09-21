# app/models/schemas/v3_meta.py
# GENERATED FILE -- do not edit by hand.
#
# Regenerate with:  python scripts/generate_orm_models.py
#
# Reflected from ci_meta. The alembic revisions are the source of truth for DDL;
# these models are for the application's own queries and as documentation.
# `alembic autogenerate` is deliberately NOT used -- see the generator docstring.
from __future__ import annotations

from datetime import date, datetime, time  # noqa: F401
from decimal import Decimal  # noqa: F401
from typing import Any, Optional  # noqa: F401

from sqlalchemy import (  # noqa: F401
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import JSONB  # noqa: F401
from sqlalchemy.orm import Mapped, mapped_column

from app.db.postgres import Base

SCHEMA = "ci_meta"


class CopilotAccessLog(Base):
    """Who ran what. principal_id is the REAL identity -- MLflow stores only safe_user_hash(user_id), so the trace alone cannot answer which person ran a query. Question/SQL/latency stay in the trace, via mlflow_trace_id."""

    __tablename__ = "copilot_access_log"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mlflow_trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    principal_id: Mapped[str] = mapped_column(Text, nullable=False)
    role_level: Mapped[str] = mapped_column(Text, nullable=False)
    denied_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CopilotGlossary(Base):
    """Reflected from ci_meta.copilot_glossary."""

    __tablename__ = "copilot_glossary"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    grain: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enum_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    remarks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lookup_resolution_mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lookup_resolution_scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lookup_reference: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    metric_class: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    min_role_level: Mapped[str] = mapped_column(Text, nullable=False)
    is_identity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    aggregation_rule: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CopilotLookupValue(Base):
    """Reflected from ci_meta.copilot_lookup_value."""

    __tablename__ = "copilot_lookup_value"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_scope: Mapped[str] = mapped_column(Text, nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_column: Mapped[str] = mapped_column(Text, nullable=False)
    reference_column: Mapped[str] = mapped_column(Text, nullable=False)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    display_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_search_text: Mapped[str] = mapped_column(Text, nullable=False)
    opco_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    opco_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l1_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l2_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l3_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l4_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    entity_class: Mapped[str] = mapped_column(Text, nullable=False)
    row_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    row_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MetricDefinition(Base):
    """Reflected from ci_meta.metric_definition."""

    __tablename__ = "metric_definition"
    __table_args__ = {"schema": SCHEMA}

    metric_key: Mapped[str] = mapped_column(Text, primary_key=True)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    metric_class: Mapped[str] = mapped_column(Text, nullable=False)
    min_role_level: Mapped[str] = mapped_column(Text, nullable=False)
    base_table: Mapped[str] = mapped_column(Text, nullable=False)
    numerator_sql: Mapped[str] = mapped_column(Text, nullable=False)
    denominator_sql: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_additive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    grain_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synonyms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["CopilotAccessLog", "CopilotGlossary", "CopilotLookupValue", "MetricDefinition"]
