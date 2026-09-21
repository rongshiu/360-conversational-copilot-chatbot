# app/service/business_alias_service.py
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


def normalize_text(value: str | None) -> str:
    text = (value or "").lower().strip()
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass(frozen=True)
class BusinessAlias:
    alias_name: str
    terms: tuple[str, ...]
    canonical_values: tuple[str, ...]
    field_hint: str
    description: str

    def __post_init__(self) -> None:
        """Coerce a bare string into a one-element tuple.

        `canonical_values=("NorthCo")` is a string, not a tuple -- the trailing
        comma is easy to omit and nothing complained. Iterating it yields
        characters, so the planner was instructed to filter
        `opco_name IN ('A','E','O','N',' ','C','o')`. Normalising here means the
        mistake cannot survive construction, wherever an alias is added.
        """
        for field in ("terms", "canonical_values"):
            value = getattr(self, field)
            if isinstance(value, str):
                object.__setattr__(self, field, (value,))
            else:
                object.__setattr__(self, field, tuple(value))


BUSINESS_ALIASES: tuple[BusinessAlias, ...] = (
    BusinessAlias(
        alias_name="Retail",
        terms=("retail",),
        canonical_values=("NorthCo", "NorthCo Mart"),
        field_hint="opco_name",
        description="NorthCo Retail refers to both NorthCo and NorthCo Mart.",
    ),
        BusinessAlias(
        alias_name="NorthCo Retail",
        terms=("northco retail",),
        canonical_values=("NorthCo",),
        field_hint="opco_name",
        description="NorthCo Retail refers to NorthCo.",
    ),
    BusinessAlias(
        alias_name="ACSM",
        terms=("acsm",),
        canonical_values=("NorthCo Credit",),
        field_hint="opco_name",
        description="ACSM refers to NorthCo Credit.",
    ),
    BusinessAlias(
        alias_name="NorthCo Insurance",
        terms=("northco insurance",),
        canonical_values=("NorthCo Credit",),
        field_hint="opco_name",
        description="NorthCo Insurance is part of NorthCo Credit.",
    ),
)


def _term_exists(query_norm: str, term: str) -> bool:
    term_norm = normalize_text(term)
    if not term_norm:
        return False

    return re.search(rf"(^|\s){re.escape(term_norm)}($|\s)", query_norm) is not None


@lru_cache(maxsize=2048)
def find_business_aliases(query: str | None) -> tuple[BusinessAlias, ...]:
    query_norm = normalize_text(query)

    if not query_norm:
        return tuple()

    matches: list[BusinessAlias] = []

    for alias in BUSINESS_ALIASES:
        if any(_term_exists(query_norm, term) for term in alias.terms):
            matches.append(alias)

    return tuple(matches)


def expand_query_with_business_aliases(query: str | None) -> str:
    """
    Expand the user query only for glossary/schema retrieval.

    Example:
    - "ACSM stores" becomes searchable with "NorthCo Credit opco_name"
    - "NorthCo Retail customers" becomes searchable with "NorthCo NorthCo Mart opco_name"

    This does not directly generate SQL. SQL generation still happens in planner_agent.
    """
    text = query or ""
    matches = find_business_aliases(text)

    if not matches:
        return text

    expansions: list[str] = []

    for alias in matches:
        expansions.append(alias.alias_name)
        expansions.extend(alias.terms)
        expansions.extend(alias.canonical_values)
        expansions.append(alias.field_hint)

    return f"{text} {' '.join(expansions)}"


def get_alias_terms_for_field(field_name: str | None) -> set[str]:
    """
    Return alias terms that should help a glossary field match.

    Example:
    field_name="opco_name" returns:
    NorthCo Retail, northco retail, NorthCo, NorthCo Mart, ACSM, NorthCo Credit, etc.
    """
    field = normalize_text(field_name)
    terms: set[str] = set()

    for alias in BUSINESS_ALIASES:
        if normalize_text(alias.field_hint) == field:
            terms.add(alias.alias_name)
            terms.update(alias.terms)
            terms.update(alias.canonical_values)

    return terms


def format_business_alias_context() -> str:
    """
    Dynamic prompt context used by planner/glossary.

    Keep business alias maintenance here instead of duplicating rules across prompts.
    """
    lines: list[str] = []

    for alias in BUSINESS_ALIASES:
        canonical = ", ".join(f"`{value}`" for value in alias.canonical_values)
        terms = ", ".join(f"`{term}`" for term in alias.terms)

        if len(alias.canonical_values) > 1:
            filter_instruction = (
                f"When the user says {terms}, filter `{alias.field_hint}` using normalized IN ({canonical})."
            )
        else:
            filter_instruction = (
                f"When the user says {terms}, filter `{alias.field_hint}` as normalized `{alias.canonical_values[0]}`."
            )

        lines.append(f"- {alias.description} {filter_instruction}")

    if not lines:
        return "- No business aliases configured."

    return "\n".join(lines)
