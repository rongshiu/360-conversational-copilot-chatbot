# app/service/entity_resolution/dictionary.py
"""Scoped entity dictionary.

The whole design rests on one observation the v2 resolver ignored: the entity
vocabulary is CLOSED and small. Roughly 550 stores, 5,400 category nodes, a
handful of OpCos and a few dozen enum values -- under 6,000 strings. Matching a
query against a known finite set is a retrieval problem, not an extraction
problem, so no LLM is needed to find the spans.

Two properties matter:

1. It is built from ci_meta.copilot_lookup_value THROUGH RLS. An NorthCo Fashion
   caller's dictionary therefore contains only NorthCo Fashion entities. When
   they type "grocery" it does not resolve -- not because a filter rejected it,
   but because it was never in the dictionary. That closes the disclosure the v2
   resolver had, where an out-of-scope name could be echoed back inside a
   clarification question.

2. Weak tokens are computed, not hardcoded. v2 kept a literal set containing
   "northco", "freshmart", "yenmart", "mynorthco2go" and so on, which needed a code change
   for every new banner. A token appearing in more than WEAK_TOKEN_DF of store
   names is weak by measurement, and adapts on its own.

Cached per scope signature, since the dictionary is identical for every caller
with the same grant.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import Logger
from app.db.v3_ddl import CORE, META, qualified
from app.service.entity_resolution.models import EntityCandidate
from app.utils.text_utils import (
    GENERIC_LOOKUP_TOKENS,
    clean_text_value,
    normalize_lookup_text,
)

logger = Logger.get_logger(__name__)

# A token in more than this share of a class's values carries no distinguishing
# information ("northco", "mall", "store"). Measured per class, per scope.
WEAK_TOKEN_DF = 0.20

# Document frequency needs at least a few values to mean anything, but the old
# floor of 20 meant small classes got NO weak tokens at all: with four OpCos,
# "northco" -- which every one of them contains -- counted as a distinguishing
# token. That is what let "northco veld" score 75 against "northco bank".
MIN_DOCS_FOR_WEAK_TOKENS = 3

# Length of the alias prefix index key. Three characters is enough to shortlist
# candidates for a fuzzy window without scanning every alias in scope.
PREFIX_LEN = 3

# Longest entity name we will try to match, in tokens. "CENTRALIZED NorthCo STYLE
# ALDERRIDGE BRENBROOK" is 5.
MAX_SPAN_TOKENS = 6

# Store names carry a lot of banner noise ("NorthCo", "Mart", "Mall") while users
# usually type the location. These tokens are safe to drop when deriving branch
# aliases because they are still resolved through the caller's RLS-scoped
# dictionary.
STORE_ALIAS_STOPWORDS = GENERIC_LOOKUP_TOKENS | {
    "bank",
    "banks",
    "n360",
    "northco360",
    "mall",
    "malls",
    "shopping",
    "complex",
    "hypermarket",
    "supermarket",
    "wellness",
    "freshmart",
    "max",
    "valu",
    "mynorthco2go",
    "ecommerce",
    "commerce",
    "store",
}

# Which column each entity class filters on, and which table it belongs to.
TARGET_COLUMNS: dict[str, str] = {
    "store": "store_id",
    "category_l1": "category_l1_key",
    "category_l2": "category_l2_key",
    "category_l3": "category_l3_key",
    "category_l4": "category_l4_key",
    "opco": "opco_code",
}


@dataclass
class EntityDictionary:
    """Immutable once built. Safe to share across concurrent requests."""

    scope_signature: str
    # normalized n-gram -> candidates (may be several: same name, different OpCo)
    exact: dict[str, list[EntityCandidate]] = field(default_factory=dict)
    # word-beginning key -> aliases containing a token with that beginning.
    # Shortlists the fuzzy pass; see prefix_keys().
    alias_prefix: dict[str, set[str]] = field(default_factory=dict)
    weak_tokens: dict[str, set[str]] = field(default_factory=dict)
    value_count: int = 0

    # Tokens that appear inside a COLUMN name, taken from the glossary. Words like
    # "lifecycle", "stage", "segment" and "bucket" name dimensions to group by, not
    # values to filter on, so a window made only of them is never an entity. Sourced
    # from the schema rather than hand-listed: adding a column protects its own
    # vocabulary, and nobody has to predict which word bites next.
    schema_terms: frozenset[str] = frozenset()

    # Every OpCo in the business, regardless of the caller's grant:
    # normalized name/code -> opco_code.
    #
    # Deliberately UNSCOPED. dim_opco carries no tenant data and the OpCo brands
    # are public, so knowing NorthCo Mart exists discloses nothing. It is what lets the
    # resolver tell "you may not see this OpCo" apart from "there is no data" --
    # without it, an NorthCo user asking about NorthCo Mart gets a truthful-looking
    # `0 active customers`, which is a materially wrong answer.
    # A label may mean several OpCos: "Retail" is NorthCo AND NorthCo Mart, so the
    # value is a tuple. Without that, a multi-OpCo alias was invisible here and an
    # NorthCo Credit caller asking "how many retail customers" got a filter on
    # business_domain = 'RETAIL' -- an OpCo selection this roster never saw -- and a
    # confident zero.
    all_opcos: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def lookup_exact(self, normalized: str) -> list[EntityCandidate]:
        return self.exact.get(normalized, [])

    def max_span_tokens(self) -> int:
        return MAX_SPAN_TOKENS


def _has_meaningful_store_token(tokens: list[str]) -> bool:
    return any(t not in STORE_ALIAS_STOPWORDS and len(t) >= 3 for t in tokens)


def _add_store_phrase_alias(aliases: set[str], tokens: list[str]) -> None:
    if not tokens or not _has_meaningful_store_token(tokens):
        return

    phrase = " ".join(tokens).strip()
    if not phrase:
        return

    aliases.add(phrase)

    compact = "".join(tokens)
    if len(tokens) > 1 and len(compact) >= 6:
        aliases.add(compact)


def _store_location_tokens(tokens: list[str]) -> list[str]:
    location = list(tokens)
    while location and location[0] in STORE_ALIAS_STOPWORDS:
        location = location[1:]
    return location


def store_aliases(normalized: str, search_text: str | None = None) -> set[str]:
    """Derive high-confidence branch aliases from a normalized store name.

    Examples:
    - "northco mart inglegate juniperford" -> "inglegate juniperford", "inglegatejuniperford"
    - "northco mall au2 novelburn" -> "au2 novelburn", "novelburn"

    The aliases are only added to the in-memory scoped dictionary, not exposed as
    global metadata, so they cannot make an out-of-scope branch discoverable.
    """
    aliases: set[str] = set()
    for value in {normalized, search_text or ""}:
        value = normalize_lookup_text(value)
        if not value:
            continue

        tokens = value.split()
        _add_store_phrase_alias(aliases, tokens)

        location = _store_location_tokens(tokens)
        if location and location != tokens:
            _add_store_phrase_alias(aliases, location)

            # Some branch names include a unit/code before the location:
            # "S05 NOVELBURN", "3 LUMENRIDGE". The human may type only the
            # named place, which is still a high-confidence alias.
            if len(location) > 1 and any(ch.isdigit() for ch in location[0]):
                _add_store_phrase_alias(aliases, location[1:])

        # Every trailing run of two or more tokens, because a branch is named for
        # the place it sits in and the place is at the END of the name.
        #
        # Stripping only the LEADING banner words was not enough. "FRESHMART QUARRYMERE
        # ROWANHOLT SOUTH" keeps "quarrymere" -- a mall, not a banner -- so its aliases
        # were "quarrymere rowanholt south" and nothing shorter, while "NorthCo Mart ROWANHOLT
        # SOUTH" reduced cleanly to "rowanholt south". That left ONE store owning
        # "rowanholt south", so "northco freshmarte rowanholt south" resolved silently and
        # confidently to the NorthCo Mart branch: the wrong shop, with no question
        # asked, in a city where four branches carry the name.
        #
        # Two tokens minimum. A one-token suffix would make "south", "juniperford" and
        # "selbycross" into store names, which is how a question about the Southern region
        # would start matching branches again.
        for start in range(1, len(tokens) - 1):
            _add_store_phrase_alias(aliases, tokens[start:])

    return aliases


def division_aliases(normalized: str) -> set[str]:
    """Retail "-line" forms of a top-level division name.

    The NorthCo level-1 divisions are single words -- HARD, SOFT, FOOD, WELL -- while
    the trade calls them hardlines and softlines, which is what users type. "how
    many customers buys hardline in 2026" resolved to nothing, so the planner
    invented `category_name = 'Hardline'`: a literal matching no row, whose empty
    result was on its way to being reported as a real zero.

    Only single-token names get this treatment, so "NON MERCHANDISE" does not
    become "non merchandiseline".
    """
    tokens = normalized.split()
    if len(tokens) != 1:
        return set()

    name = tokens[0]
    if len(name) < 3 or not name.isalpha():
        return set()

    return {f"{name}line", f"{name} line", f"{name}lines", f"{name} lines"}


def prefix_keys(token: str) -> set[str]:
    """Index keys for one token: its word beginning, and that beginning sorted.

    The sorted key catches transpositions -- "bnak" and "bank" both key on "abn"
    -- so a typo in the first characters still shortlists the right alias without
    a full scan.
    """
    if len(token) < PREFIX_LEN:
        return set()
    head = token[:PREFIX_LEN]
    return {head, "".join(sorted(head))}


def _add_exact_alias(
    dictionary: EntityDictionary,
    alias: str,
    candidate: EntityCandidate,
) -> None:
    alias = normalize_lookup_text(alias)
    if not alias:
        return

    candidates = dictionary.exact.setdefault(alias, [])
    # The column is part of the identity. Without it, an enum value that several
    # columns share -- "Active" is a member_status AND a customer_status_in_opco,
    # "New" is a lifecycle_stage AND a tenure_bucket -- kept only whichever row the
    # catalogue query happened to return first, and that query has no ORDER BY. The
    # copilot then filtered a column nobody chose, and which one it was could change
    # between reloads of the same data.
    #
    # For every other class the column is fixed by the class, so this is a no-op
    # there. See matcher.collapse_enum_columns for how the choice is now made.
    identity = (candidate.entity_class, candidate.canonical_value, candidate.target_column)
    if all(
        (c.entity_class, c.canonical_value, c.target_column) != identity
        for c in candidates
    ):
        candidates.append(candidate)

    for token in alias.split():
        for key in prefix_keys(token):
            dictionary.alias_prefix.setdefault(key, set()).add(alias)


def _opco_aliases():
    """Business aliases whose field_hint names an OpCo."""
    try:
        from app.service.business_alias_service import BUSINESS_ALIASES

        return [a for a in BUSINESS_ALIASES if a.field_hint == "opco_name"]
    except Exception:  # noqa: BLE001
        logger.debug("business aliases unavailable", exc_info=True)
        return []


def _schema_terms() -> frozenset[str]:
    """Words describing the schema, from glossary columns and the metric registry.

    See glossary_service.question_vocabulary for why each source is there.
    """
    try:
        from app.service.glossary_service import question_vocabulary

        return question_vocabulary()
    except Exception:  # noqa: BLE001
        # The dictionary must still build without it; this is a precision aid, not
        # a correctness guarantee.
        logger.debug("question vocabulary unavailable", exc_info=True)
        return frozenset()


async def build_dictionary(
    db: AsyncSession, scope_signature: str
) -> EntityDictionary:
    """Load every lookup value the caller may see, and index it.

    Must be called inside copilot_scope() so RLS narrows the rows.
    """
    rows = (
        await db.execute(
            sa.text(
                f"""
                SELECT entity_class,
                       normalized_value,
                       normalized_search_text,
                       display_value,
                       raw_value,
                       source_column,
                       opco_code,
                       opco_name,
                       category_key,
                       row_context
                FROM {qualified(META, 'copilot_lookup_value')}
                WHERE is_active
                """
            )
        )
    ).mappings().all()

    dictionary = EntityDictionary(
        scope_signature=scope_signature, schema_terms=_schema_terms()
    )
    token_docs: dict[str, list[list[str]]] = defaultdict(list)

    # The full OpCo roster, identical for every caller.
    #
    # Filtered on opco_type rather than the dropped is_group_entity flag. The
    # intent is unchanged: N360 is the holding entity and carries no fact rows, so
    # letting a question resolve to it would produce a filter that matches nothing.
    # What changed is the reason -- it used to be excluded because it meant
    # "see everything", and is now excluded because it has no data.
    by_name: dict[str, str] = {}
    for row in (
        await db.execute(
            sa.text(
                f"SELECT opco_code, opco_name FROM {qualified(CORE, 'dim_opco')} "
                "WHERE is_active AND opco_type = 'OPERATING'"
            )
        )
    ).mappings().all():
        code = row["opco_code"]
        for label in (row["opco_name"], code, code.replace("_", " ")):
            norm = normalize_lookup_text(label)
            if norm:
                dictionary.all_opcos[norm] = (code,)
        by_name[normalize_lookup_text(row["opco_name"])] = code

    # Business aliases for OpCos, so a real trading name is recognised as the OpCo
    # it means. "NorthCo Retail" IS NorthCo -- it is in the alias registry and the
    # planner resolves it correctly -- but this roster only knew dim_opco names and
    # codes, so out-of-scope detection saw no OpCo at all. An NorthCo Credit caller
    # asking about NorthCo Retail therefore reached the planner, which filtered
    # opco_code = 'NORTHCO', and the empty result was narrated as "no customer
    # activity was captured for this OpCo".
    #
    # Multi-OpCo aliases included: "Retail" resolves to both retail OpCos, and a
    # caller granted neither must be told so rather than shown a zero.
    for alias in _opco_aliases():
        codes = tuple(
            sorted(
                {
                    by_name[normalize_lookup_text(v)]
                    for v in alias.canonical_values
                    if normalize_lookup_text(v) in by_name
                }
            )
        )
        if not codes:
            continue
        for term in (*alias.terms, alias.alias_name):
            norm = normalize_lookup_text(term)
            if norm and norm not in dictionary.all_opcos:
                dictionary.all_opcos[norm] = codes

    for row in rows:
        entity_class = row["entity_class"] or "enum"
        normalized = row["normalized_value"]
        if not normalized:
            continue

        ctx = dict(row["row_context"] or {})
        if row["opco_name"]:
            ctx["opco_name"] = row["opco_name"]
        target = TARGET_COLUMNS.get(entity_class) or row["source_column"]

        # For a store the canonical SQL value is the id, not the name: filtering
        # by name is fragile and the name is only for display.
        canonical = row["raw_value"]
        if entity_class == "store":
            canonical = str(ctx.get("store_id") or row["raw_value"])
        elif entity_class in TARGET_COLUMNS and entity_class.startswith("category_"):
            canonical = str(row["category_key"] or row["raw_value"])

        candidate = EntityCandidate(
            entity_class=entity_class,
            canonical_value=canonical,
            display_value=row["display_value"] or row["raw_value"],
            target_column=target,
            score=100.0,
            match_kind="exact",
            opco_code=row["opco_code"],
            category_key=row["category_key"],
            category_level=ctx.get("category_level"),
            row_context=ctx,
        )

        search_text = row["normalized_search_text"]
        if entity_class == "store":
            # Branch aliases only. There used to be a second, deliberately
            # low-confidence "confirmation alias" index holding every sub-phrase
            # of a store name, which existed to let a partial name suggest rather
            # than filter. Scoring now expresses that directly -- a partial match
            # simply scores below lookup_confident_score -- so the parallel index
            # and its _requires_clarification flag are gone.
            aliases = store_aliases(normalized, search_text)
        else:
            aliases = {normalized}
            if search_text and search_text != normalized:
                aliases.add(search_text)
            if entity_class == "category_l1":
                aliases |= division_aliases(normalized)

        for alias in aliases:
            _add_exact_alias(dictionary, alias, candidate)

        token_docs[entity_class].append(normalized.split())
        dictionary.value_count += 1

    # Document frequency per class -> weak tokens, replacing v2's hardcoded set.
    for entity_class, docs in token_docs.items():
        if len(docs) < MIN_DOCS_FOR_WEAK_TOKENS:
            dictionary.weak_tokens[entity_class] = set()
            continue
        df: Counter[str] = Counter()
        for tokens in docs:
            df.update(set(tokens))
        # Share-based, not count-based: with four OpCos a token in all four is
        # weak, even though a count cutoff of 20 would never fire.
        cutoff = max(2, len(docs) * WEAK_TOKEN_DF)
        dictionary.weak_tokens[entity_class] = {
            token for token, count in df.items() if count >= cutoff
        }

    logger.info(
        "entity dictionary built for scope %s: %d values, %d n-grams, "
        "%d prefix keys, weak tokens %s",
        scope_signature,
        dictionary.value_count,
        len(dictionary.exact),
        len(dictionary.alias_prefix),
        {k: len(v) for k, v in dictionary.weak_tokens.items()},
    )
    return dictionary


# ---------------------------------------------------------------------------
# Per-scope cache
# ---------------------------------------------------------------------------

_cache: dict[str, EntityDictionary] = {}
_MAX_CACHED_SCOPES = 64


async def get_dictionary(db: AsyncSession, scope_signature: str) -> EntityDictionary:
    cached = _cache.get(scope_signature)
    if cached is not None:
        return cached

    dictionary = await build_dictionary(db, scope_signature)

    if len(_cache) >= _MAX_CACHED_SCOPES:
        # Simple eviction: distinct scopes are few in practice (one per
        # opco x category-grant x role combination actually in use).
        _cache.clear()
    _cache[scope_signature] = dictionary
    return dictionary


def clear_dictionary_cache() -> None:
    """Call after reloading copilot_lookup_value."""
    _cache.clear()


def normalize(value: Optional[str]) -> str:
    return normalize_lookup_text(clean_text_value(value))
