#!/usr/bin/env python
"""Generate SQLAlchemy ORM models by reflecting the live v3 schema.

    python scripts/generate_orm_models.py

Writes app/models/schemas/v3_core.py and v3_meta.py.

Generated rather than hand-written on purpose: the schema is 262 columns across 17
tables, and hand-typing it guarantees typos that only surface as a runtime error on
a rarely-used column. Reflection makes the models correct by construction.

The alembic revisions remain the ONLY source of truth for DDL. These models exist
for the application's own queries (principal resolution, the entity dictionary, the
loaders, the access log) and as in-code documentation. `alembic autogenerate` stays
off, because partitioned parents, RLS policies, security_invoker views and per-role
grants have no declarative representation -- autogenerate would happily emit
migrations that drop them.

tests/test_schema_drift.py guards the one risk this arrangement creates: models
silently diverging from the database. Regenerate after any schema migration.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import settings  # noqa: E402

# Reflected type -> (python annotation, SQLAlchemy type expression)
TYPE_MAP: dict[str, tuple[str, str]] = {
    "BIGINT": ("int", "BigInteger"),
    "INTEGER": ("int", "Integer"),
    "SMALLINT": ("int", "SmallInteger"),
    "TEXT": ("str", "Text"),
    "VARCHAR": ("str", "Text"),
    "BOOLEAN": ("bool", "Boolean"),
    "DATE": ("date", "Date"),
    "TIME": ("time", "Time"),
    "TIMESTAMP": ("datetime", "DateTime(timezone=True)"),
    "JSONB": ("dict[str, Any]", "JSONB"),
    "NUMERIC": ("Decimal", "Numeric"),
    "ARRAY": ("list[str]", "ARRAY(Text)"),
    "LTREE": ("str", "Text"),  # ltree has no SQLAlchemy type; text is wire-compatible
}

HEADER = '''# {module}
# GENERATED FILE -- do not edit by hand.
#
# Regenerate with:  python scripts/generate_orm_models.py
#
# Reflected from {schema}. The alembic revisions are the source of truth for DDL;
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

SCHEMA = "{schema}"

'''


def class_name(table: str) -> str:
    return "".join(part.title() for part in table.split("_"))


def python_type(col: dict) -> tuple[str, str]:
    raw = str(col["type"]).upper()
    base = re.split(r"[(\[ ]", raw)[0]
    annotation, sa_type = TYPE_MAP.get(base, ("Any", "Text"))

    # Preserve numeric precision so a round-trip keeps the same scale.
    if base == "NUMERIC":
        m = re.search(r"NUMERIC\((\d+),\s*(\d+)\)", raw)
        if m:
            sa_type = f"Numeric({m.group(1)}, {m.group(2)})"
    return annotation, sa_type


def emit_table(insp, schema: str, table: str) -> str:
    cols = insp.get_columns(table, schema=schema)
    pk = set((insp.get_pk_constraint(table, schema=schema) or {}).get("constrained_columns") or [])
    comment = (insp.get_table_comment(table, schema=schema) or {}).get("text")

    lines = [f"class {class_name(table)}(Base):"]
    doc = comment or f"Reflected from {schema}.{table}."
    # Keep the table COMMENT as the docstring -- it carries the design rationale.
    lines.append(f'    """{doc}"""')
    lines.append("")
    lines.append(f'    __tablename__ = "{table}"')
    lines.append('    __table_args__ = {"schema": SCHEMA}')
    lines.append("")

    for col in cols:
        name = col["name"]
        annotation, sa_type = python_type(col)
        nullable = bool(col["nullable"])
        is_pk = name in pk

        opts = [sa_type]
        if is_pk:
            opts.append("primary_key=True")
        # A PK column is implicitly NOT NULL; stating it again is noise.
        if not is_pk:
            opts.append(f"nullable={nullable}")

        ann = f"Mapped[Optional[{annotation}]]" if nullable and not is_pk else f"Mapped[{annotation}]"
        lines.append(f"    {name}: {ann} = mapped_column({', '.join(opts)})")

    return "\n".join(lines) + "\n"


def generate(schema: str, module_path: Path) -> int:
    url = settings.postgres_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = sa.create_engine(url)
    insp = sa.inspect(engine)

    # Skip partition children: they share the parent's shape and mapping them would
    # create 160 redundant classes.
    tables = sorted(
        t
        for t in insp.get_table_names(schema=schema)
        if not re.search(r"_p(?:\d{6}|default)$", t)
    )

    body = [HEADER.format(module=module_path.as_posix(), schema=schema)]
    for table in tables:
        body.append(emit_table(insp, schema, table))
        body.append("")

    exported = ", ".join(f'"{class_name(t)}"' for t in tables)
    body.append(f"__all__ = [{exported}]\n")

    module_path.write_text("\n".join(body))
    return len(tables)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="app/models/schemas")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    for schema, filename in (
        (settings.ci_core_schema, "v3_core.py"),
        (settings.ci_meta_schema, "v3_meta.py"),
    ):
        n = generate(schema, out / filename)
        total += n
        print(f"{filename:<12} {n:>3} models from {schema}")
    print(f"{total} models generated")


if __name__ == "__main__":
    main()
