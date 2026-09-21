# app/models/schemas/v3_core.py
# GENERATED FILE -- do not edit by hand.
#
# Regenerate with:  python scripts/generate_orm_models.py
#
# Reflected from ci_core. The alembic revisions are the source of truth for DDL;
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

SCHEMA = "ci_core"


class AggSalesDaily(Base):
    """Reflected from ci_core.agg_sales_daily."""

    __tablename__ = "agg_sales_daily"
    __table_args__ = {"schema": SCHEMA}

    calendar_date: Mapped[date] = mapped_column(Date, primary_key=True)
    daypart: Mapped[str] = mapped_column(Text, primary_key=True)
    daypart_seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    category_l1_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_l2_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    customer_type: Mapped[str] = mapped_column(Text, primary_key=True)
    transaction_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    customer_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_sales_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    net_sales_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    gmv_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)


class AggSalesSkuMonthly(Base):
    """Optional SKU and BRAND sales. Deliberately NO CUSTOMER KEY -- SKU x customer is the combination that must never exist, and the customer bridge stops at category, so nothing pairs a customer with a SKU or a brand. Brand here answers what sold, never who bought it."""

    __tablename__ = "agg_sales_sku_monthly"
    __table_args__ = {"schema": SCHEMA}

    month_start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    brand_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_l1_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_l2_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l3_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l4_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    customer_type: Mapped[str] = mapped_column(Text, primary_key=True)
    transaction_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    gross_sales_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)


class BridgeCustomerCategoryMonthly(Base):
    """Category-scoped customer activity: who bought what, where, and for how much. LEAF GRAIN with every ancestor denormalised, so a question filters category_l1_key..category_l4_key at whatever depth it names and a customer is counted once. Carries store_id; deliberately carries no brand and no SKU."""

    __tablename__ = "bridge_customer_category_monthly"
    __table_args__ = {"schema": SCHEMA}

    customer_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    month_start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    category_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    category_l1_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_l2_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l3_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l4_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    store_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    gross_sales_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)


class DimOpco(Base):
    """OpCo master. Joinable by the planner for opco_name and business_domain."""

    __tablename__ = "dim_opco"
    __table_args__ = {"schema": SCHEMA}

    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    opco_name: Mapped[str] = mapped_column(Text, nullable=False)
    opco_type: Mapped[str] = mapped_column(Text, nullable=False)
    business_domain: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DimProduct(Base):
    """Reflected from ci_core.dim_product."""

    __tablename__ = "dim_product"
    __table_args__ = {"schema": SCHEMA}

    sku_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opco_code: Mapped[str] = mapped_column(Text, nullable=False)
    sku_code: Mapped[str] = mapped_column(Text, nullable=False)
    sku_name: Mapped[str] = mapped_column(Text, nullable=False)
    brand_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_l1_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_l2_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l3_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l4_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    uom: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DimProductCategory(Base):
    """4-level category hierarchy. The denormalised l1..l4 ancestor keys are what make a permission grant at any level enforceable as one indexable equality."""

    __tablename__ = "dim_product_category"
    __table_args__ = {"schema": SCHEMA}

    category_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opco_code: Mapped[str] = mapped_column(Text, nullable=False)
    category_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    category_code: Mapped[str] = mapped_column(Text, nullable=False)
    category_name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_category_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False)
    category_path: Mapped[Any] = mapped_column(Text, nullable=False)
    category_path_text: Mapped[str] = mapped_column(Text, nullable=False)
    l1_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    l1_name: Mapped[str] = mapped_column(Text, nullable=False)
    l2_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    l2_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    l3_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    l3_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    l4_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    l4_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DimStore(Base):
    """Store master, keyed by (opco_code, store_id). store_id repeats across OpCos, so every join to a fact MUST match on both columns."""

    __tablename__ = "dim_store"
    __table_args__ = {"schema": SCHEMA}

    store_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    store_name: Mapped[str] = mapped_column(Text, nullable=False)
    store_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    store_location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    city_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    open_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    close_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    opened_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    closed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class FactCustomerOpcoMonthly(Base):
    """The customer's relationship with ONE OpCo in one month. A customer active in two OpCos occupies TWO rows, so COUNT(*) is not a customer count -- COUNT(DISTINCT customer_key) is. Traits live on FactCustomerTraitsMonthly; join on customer_key AND month_start_date."""

    __tablename__ = "fact_customer_opco_monthly"
    __table_args__ = {"schema": SCHEMA}

    customer_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    month_start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    is_member: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Declared in the DDL through the CUSTOMER_TYPE_CHECK macro, not as a plain
    # column line -- which is why a naive read of the migration misses it.
    customer_type: Mapped[str] = mapped_column(Text, nullable=False)
    is_active_in_opco: Mapped[bool] = mapped_column(Boolean, nullable=False)
    customer_status_in_opco: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    member_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_primary_opco: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    days_since_last_purchase: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active_months_6m: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    active_months_12m: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    purchase_months_12m: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    longest_consecutive_months_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    active_days_in_month: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    repeat_purchase_segment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    purchase_ratio_segment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_store_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    store_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    preferred_store_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    nearest_store_change_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cash_txn_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    card_txn_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ewallet_txn_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    primary_daypart: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_category_l1_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l1_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    transaction_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    total_revenue: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_gmv: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    northvalu_transaction_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class FactCustomerTraitsMonthly(Base):
    """Who the customer IS, one row per customer per month -- no OpCo. COUNT(*) here IS a customer count, and AVG(age) is correct, neither of which is true on the OpCo table. Holds no measures: counts and amounts stay on FactCustomerOpcoMonthly so the two cannot disagree."""

    __tablename__ = "fact_customer_traits_monthly"
    __table_args__ = {"schema": SCHEMA}

    customer_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    month_start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    age: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    gender_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generation_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    residency_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    membership_tier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    member_join_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    member_tenure_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tenure_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lifecycle_stage: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_at_risk_customer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_transaction_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    lifetime_transaction_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    visit_frequency_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    basket_size_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    northvalu_adoption_segment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    family_segment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    financial_product_holding_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    preferred_payment_segment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    primary_payment_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    secondary_affinity_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    active_credit_card_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    active_loan_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    active_insurance_count: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    has_northco_bank: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    cross_holder_combo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    no_product_eligibility_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active_opco_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    active_opco_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_multi_opco_customer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    primary_opco_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

class FactSalesDaily(Base):
    """Atomic sales grain. NO customer key by design -- this is what makes customer-level answers structurally impossible."""

    __tablename__ = "fact_sales_daily"
    __table_args__ = {"schema": SCHEMA}

    calendar_date: Mapped[date] = mapped_column(Date, primary_key=True)
    daypart: Mapped[str] = mapped_column(Text, primary_key=True)
    daypart_seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    opco_code: Mapped[str] = mapped_column(Text, primary_key=True)
    store_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_l1_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_l2_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l3_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    category_l4_key: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    customer_type: Mapped[str] = mapped_column(Text, primary_key=True)
    payment_type: Mapped[str] = mapped_column(Text, primary_key=True)
    transaction_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_item_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    customer_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_sales_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    net_sales_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    gmv_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["AggSalesDaily", "AggSalesSkuMonthly", "BridgeCustomerCategoryMonthly", "DimOpco", "DimProduct", "DimProductCategory", "DimStore", "FactCustomerOpcoMonthly", "FactSalesDaily"]
