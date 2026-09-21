# app/service/entity_resolution/matcher.py
"""Span matching over the scoped dictionary.

Pass 1  EXACT   slide 1..N token windows over the normalized query, longest match
                wins, non-overlapping. Pure dict lookups, no LLM, no database.
Pass 2  FUZZY   for each run of leftover, non-filler tokens, find the best
                reading of that run against the aliases the dictionary already
                holds. In memory, one scorer, one threshold ladder.

Both passes return spans that never overlap, which is the main structural change
from the previous version. That version generated every nested window of the
query ("visited northco veld", "northco veld", "veld nin", "veld", ...) and treated
each as an independent finding, so one misspelt branch name produced three
competing slots. Nested windows are not findings; they are alternative readings
of the same words, and only the best one is a finding. Choosing between them is
now internal to this module: `_best_reading_of_run` scores every sub-window of a
run and keeps the highest-scoring non-overlapping set, so nothing downstream ever
sees two spans covering the same token.

Scoring was the second cause of trouble. There used to be four scorers (token-set
Jaccard, character SequenceMatcher, a "substring bridge", and pg_trgm similarity)
combined with `max()`, each with its own cap and floor, so the numbers were not
comparable and a threshold that behaved on one path misbehaved on another. On
"how many customers visited northco veld nin july 2025" that produced:

  - five unrelated branches all scored exactly 72.0, because the substring bridge
    returned `min(72.0, ...)` -- among them NORTHCO MALL SOVELD, which shares only
    the tail "veld" -- presented to the user as a five-way choice;
  - "northcowang" scored 75 against "northcobank", above the then-floor of 72, and with
    one candidate nothing marked it ambiguous, so a wrong OpCo became a hard SQL
    filter silently.

So: one scorer, prefix-aware (shared word beginnings are what proper-noun typos
preserve, shared tails are coincidence), and a fuzzy match never resolves below
`lookup_confident_score` -- a mediocre lone match asks, it does not filter.

The pg_trgm round trip is gone too. It queried ci_meta.copilot_lookup_value,
which is the table the in-memory dictionary is built from under the same RLS, so
it could only ever return rows already in hand.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from app.core import settings
from app.core.logging import Logger
from app.service.entity_resolution.dictionary import (
    STORE_ALIAS_STOPWORDS,
    EntityDictionary,
    normalize,
    prefix_keys,
)
from app.service.entity_resolution.models import EntityCandidate, EntitySpan
from app.utils.text_utils import FALLBACK_STOPWORDS

logger = Logger.get_logger(__name__)

# Weight of a token that carries no distinguishing information for its class
# ("northco" in an OpCo name, "mall" in a store name). Not zero: matching it is mild
# corroboration. Not one: it must never carry a match on its own.
WEAK_TOKEN_WEIGHT = 0.2

# The prefix bonus needs enough characters, and enough of the whole word, to be
# evidence rather than coincidence. Without these floors "veld" would score 0.85
# against the compact alias "wangsamaju" purely for being its first four
# characters -- and then auto-resolve, picking one of three Veldra branches
# silently. "novelbur" -> "novelburn" (9 of 11 characters) still clears both.
MIN_PREFIX_BONUS_CHARS = 5
MIN_PREFIX_COVERAGE = 0.7

# A single-token fuzzy window shorter than this is noise. "nin" (a typo of "in")
# was reaching the scorer and pulling in branch names.
MIN_SINGLE_TOKEN_FUZZY_CHARS = 4

# What a ONE-WORD guess has to look like. A single token carries no context, so
# the only thing that makes it evidence is that it is nearly the spelling of a
# word in the name -- or that it accounts for a real share of a short name.
#
# Without this, "how many hold an active NorthCo Credit card" asked which BAG the
# user meant: "hold" scores 0.80 against "holder" (SequenceMatcher, 4 of 6
# characters, same first letter), and 0.7*0.80 + 0.3*0.33 = 66 clears the suggest
# floor of 62. It is not a misspelling of anything; it is the verb of the
# sentence. Every English word of four or more letters that shares a beginning
# with some word in a 5,400-name catalogue had the same power, which is why the
# resolver looked erratic rather than wrong in one specific way.
#
# 0.9 is what a typo of a proper noun actually scores: "grocry" -> "grocery" is
# 0.92, "novelbur" -> "novelburn" 0.95. A word that only shares a stem, like
# hold/holder or card/cards, lands at 0.80 and no longer guesses.
SINGLE_TOKEN_ANCHOR_SIMILARITY = 0.9

# ... unless the token accounts for at least half of a short name, which is how a
# partial name is meant to reach the user as a suggestion: "veldra" is one strong
# token of "YENMART S25 VELDRA SELBYCROSS", "fashion" is half of "HOME FASHION".
MIN_SINGLE_TOKEN_RECALL = 0.5

# Nouns that follow an OpCo brand name and name that OpCo's product line rather
# than a separate thing to filter on: "NorthCo Credit CARD", "NorthCo Bank ACCOUNT".
# Absorbed into the OpCo span so they cannot become a second filter.
#
# "How many hold an active NorthCo Credit Card" resolved `opco_code = 'NORTHCO_CREDIT'`
# from "northco credit" and then, from the leftover "card", `payment_type = 'Card'`
# -- silently, because "Card" is an exact enum value. The question named one
# thing and produced two filters, the second of which restricts the answer to
# card payers for no reason the user could see.
OPCO_BRAND_TAIL = frozenset(
    {"card", "cards", "account", "accounts", "service", "services", "group", "ltd"}
)

# Longest fuzzy window in tokens. Longer than this and it is a sentence, not a
# misspelt name.
MAX_FUZZY_WINDOW_TOKENS = 4

def max_options_per_span() -> int:
    """Most options one phrase may offer, from settings.

    Read at call time, like every other threshold in this module, so the value can
    be tuned by LOOKUP_MAX_OPTIONS without a code change. The resolver reads the
    same function to render its numbered list: these used to be separate literals,
    5 here and 6 there, so "fashion" showed five of its nine equally-scoring matches
    and nothing said the other four existed.

    It is a ceiling, not a target. lookup_option_window and the one-family rule
    still decide what is a plausible option at all.
    """
    return max(1, int(settings.lookup_max_options or 10))

# How the two directions of the match are combined. Precision (does the user's
# phrase appear in the name) matters more than recall (how much of the name the
# phrase accounts for), because typing part of a branch name is normal.
PRECISION_WEIGHT = 0.7
RECALL_WEIGHT = 1.0 - PRECISION_WEIGHT

# Recall counts a target token as accounted for only above this similarity.
# Averaging raw similarities instead let coincidence pay: "fashion" beat
# "HOME FASHION" with "FATE FASHION", because "fate" happens to look 0.55 like
# "fashion" while "home" does not. Neither is a match, and a threshold says so.
STRONG_TOKEN_MATCH = 0.8

# A minimum-recall floor was tried here and removed. The idea was that one token of
# four is weak evidence, which would have dropped "stage" against
# "SCARLETEEN BRA STAGE 1" (recall 0.25). It also drops "veldra" against
# "YENMART S25 VELDRA SELBYCROSS" -- recall 0.25, and a real Veldra branch the user wants
# offered. The two cases are identical to every string statistic; what separates
# them is that "stage" is being USED as a dimension word, which is context rather
# than spelling. Anything that tries to tell them apart lexically will keep getting
# one of them wrong, so the discrimination belongs upstream, in whatever reads the
# question. See dictionary.schema_terms for the part that is decidable from data.

# Classes a fuzzy match may propose. OpCo is deliberately absent: it is a
# permission scope rather than a lookup value, there are only a handful of them,
# and they are brand names users spell correctly. Guessing across four values is
# how "northco veld" became a filter on NORTHCO_BANK -- every OpCo name starts with
# "northco", so the shared prefix supplied most of the similarity. An exact
# dictionary hit on "northco mart" still resolves; only guessing is off.
FUZZY_MATCHABLE_CLASSES = frozenset(
    {"store", "category_l1", "category_l2", "category_l3", "category_l4", "enum"}
)


@dataclass(frozen=True)
class TokenRun:
    """A stretch of leftover query tokens that could hold a misspelt entity.

    Runs are maximal and separated by filler, so a run never starts or ends on a
    verb, connector, month or number -- "visited" and "july" cannot become part
    of a branch name.
    """

    start: int
    tokens: tuple[str, ...]

    @property
    def end(self) -> int:
        return self.start + len(self.tokens)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _token_similarity(a: str, b: str) -> float:
    """0..1 similarity between two single tokens, prefix-aware.

    Proper-noun typos and truncations preserve the beginning of the word
    ("novelbur" for "novelburn"), whereas a shared tail is weak evidence:
    "veld" and "soveld" share four of six characters but name different places.
    A plain edit-distance ratio scores that pair at 0.80, which is how NorthCo MALL
    SOVELD ended up in a clarification list for "northco veld".
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    short, long = (a, b) if len(a) <= len(b) else (b, a)
    coverage = len(short) / len(long)
    if (
        len(short) >= MIN_PREFIX_BONUS_CHARS
        and coverage >= MIN_PREFIX_COVERAGE
        and long.startswith(short)
    ):
        # A real prefix, discounted by how much of the name is missing.
        return 0.75 + 0.25 * coverage

    ratio = SequenceMatcher(None, a, b).ratio()
    if a[0] != b[0]:
        # Different first character: at best a coincidental middle overlap.
        ratio *= 0.5
    return ratio


def _weighted(tokens: tuple[str, ...], weak: set[str]) -> list[tuple[str, float]]:
    return [(t, WEAK_TOKEN_WEIGHT if t in weak else 1.0) for t in tokens]


def _precision(query: tuple[str, ...], alias: tuple[str, ...], weak: set[str]) -> float:
    """Weighted mean, over the query, of each token's best match in the alias."""
    total = earned = 0.0
    for token, weight in _weighted(query, weak):
        best = max((_token_similarity(token, a) for a in alias), default=0.0)
        total += weight
        earned += weight * best
    return earned / total if total else 0.0


def _recall(query: tuple[str, ...], alias: tuple[str, ...], weak: set[str]) -> float:
    """Weighted share of the alias the query actually accounts for."""
    total = earned = 0.0
    for token, weight in _weighted(alias, weak):
        best = max((_token_similarity(token, q) for q in query), default=0.0)
        total += weight
        if best >= STRONG_TOKEN_MATCH:
            earned += weight
    return earned / total if total else 0.0


def phrase_score(
    query_tokens: tuple[str, ...],
    alias_tokens: tuple[str, ...],
    weak: set[str],
) -> float:
    """0-100 similarity between a query window and a dictionary alias.

    The single scorer for every fuzzy comparison, so one threshold ladder means
    the same thing everywhere.
    """
    if not query_tokens or not alias_tokens:
        return 0.0

    # A window made only of weak tokens ("northco", "northco mall") identifies nothing.
    # Exact aliases still resolve those phrases; fuzzy matching must not guess.
    if all(token in weak for token in query_tokens):
        return 0.0

    return 100.0 * (
        PRECISION_WEIGHT * _precision(query_tokens, alias_tokens, weak)
        + RECALL_WEIGHT * _recall(query_tokens, alias_tokens, weak)
    )


# Tokens the loader ADDS to a division alias rather than reading from the data.
# dictionary.division_aliases turns a one-word level 1 name into "hardline",
# "hard line", "hardlines", "hard lines", because the trade says hardline and the
# catalogue says HARD.
#
# Measured document frequency cannot see them as weak -- they are synthesised, and
# there are only 16 divisions -- so "line" looked as distinguishing as "appliance".
# Any query word resembling it then matched EVERY division at the same score:
# "...have also LINKED an NorthCo Bank account" scored 71.0 against Appliance, Bazaar,
# FOOD, Fresh and Grocery alike, because "linked" is 0.8 similar to "line" and
# "line" is half of a two-token alias.
SYNTHETIC_CATEGORY_TOKENS: frozenset[str] = frozenset({"line", "lines"})


def _weak_tokens(dictionary: EntityDictionary, entity_class: str) -> set[str]:
    weak = set(dictionary.weak_tokens.get(entity_class) or ())
    if entity_class == "store":
        # Banner words are weak by construction, not by measurement: every store
        # name carries them and users type the location.
        weak |= STORE_ALIAS_STOPWORDS
    if entity_class == "category_l1":
        weak |= SYNTHETIC_CATEGORY_TOKENS
    return weak


# ---------------------------------------------------------------------------
# Tokenizing and run extraction
# ---------------------------------------------------------------------------

def _is_filler(token: str) -> bool:
    return token in FALLBACK_STOPWORDS or token.isdigit()


def query_tokens(query: str) -> list[str]:
    return [t for t in normalize(query).split() if t]


def leftover_runs(
    tokens: list[str],
    consumed: list[bool],
    blocked: frozenset[str] = frozenset(),
) -> list[TokenRun]:
    """Maximal runs of tokens that neither matched exactly nor look like filler.

    `blocked` holds schema vocabulary -- words naming a column or metric rather
    than a value in one. They break a run exactly as filler does, so they cannot
    appear ANYWHERE in a fuzzy window.

    That is stricter than the guard it replaces, which skipped only a window made
    ENTIRELY of schema words, and the difference was doing real damage. Asked "do
    we have customers who stopped buying fashion", the window ("fashion",) was
    skipped -- "fashion" is a metric word -- while ("buying", "fashion") was not,
    so the best reading was suppressed and a worse one won: the user was offered
    five BEDDING categories at score 64 instead of HOME FASHION at 85. Suppressing
    a word only in isolation lets the same word drag in a match as soon as any
    neighbour joins it.

    Exact matching is untouched: it reads the raw token stream, so a category
    genuinely NAMED after one of these words still resolves. Blocking here means
    "do not GUESS from this word".
    """
    runs: list[TokenRun] = []
    start: int | None = None

    for index, token in enumerate(tokens + [""]):
        is_break = (
            index >= len(tokens)
            or consumed[index]
            or _is_filler(token)
            or token in blocked
        )
        if is_break:
            if start is not None:
                runs.append(TokenRun(start=start, tokens=tuple(tokens[start:index])))
                start = None
            continue
        if start is None:
            start = index

    return runs


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------

def restrict_runs(runs: list[TokenRun], allowed: frozenset[str]) -> list[TokenRun]:
    """Keep only the parts of each run the router said were naming something.

    This is the one place a model gets a say in entity resolution, and it is
    deliberately the only place it could help. Exact matching is a lookup over a
    closed catalogue -- a model cannot improve on it and could not enforce the
    caller's scope, which comes for free from the dictionary being built through
    row-level security. Guessing is the opposite: deciding whether "linked",
    "hold", "top" or "brands" is naming a product at all is a judgement about the
    sentence, and string similarity has no way to make it.

    Measured over a 31-question corpus, 49 of 54 resolutions were exact. So this
    bounds the remaining 5 without touching the 90% that already cannot be wrong.

    A token not in `allowed` breaks a run exactly as filler does, so a phrase the
    router did name is still matched in full.
    """
    if not runs:
        return runs

    kept: list[TokenRun] = []
    for run in runs:
        start: int | None = None
        for offset, token in enumerate(run.tokens + ("",)):
            if offset >= len(run.tokens) or token not in allowed:
                if start is not None:
                    kept.append(
                        TokenRun(
                            start=run.start + start,
                            tokens=run.tokens[start:offset],
                        )
                    )
                    start = None
                continue
            if start is None:
                start = offset
    return kept


def match_exact(
    query: str, dictionary: EntityDictionary
) -> tuple[list[EntitySpan], list[TokenRun]]:
    """Longest-match-wins, non-overlapping span tagging.

    Returns the matched spans plus the leftover runs for the fuzzy pass. Exact
    matching runs over the raw token stream, filler included, because a whole
    entity name can be made of otherwise-generic words -- "northco mart" is an OpCo.
    """
    tokens = query_tokens(query)
    n = len(tokens)
    spans: list[EntitySpan] = []
    consumed = [False] * n

    max_span = min(dictionary.max_span_tokens(), n)

    # Longest first, so "northco mall inglegate juniperford" wins over "inglegate juniperford".
    for size in range(max_span, 0, -1):
        for start in range(0, n - size + 1):
            end = start + size
            if any(consumed[start:end]):
                continue

            normalized = " ".join(tokens[start:end])

            # A ONE-WORD exact hit on a word the question was always going to
            # contain is not a name. "Which TOP 5 brands..." resolved
            # `category_l4_key = SOFT > INNERWEAR > MEN INNERWEAR > TOP`, silently
            # and at full confidence, because a category really is called TOP.
            #
            # This does not weaken the reason exact matching reads raw tokens in the
            # first place -- a whole entity name made of generic words, like the
            # OpCo "northco mart" or the category "credit card holder", is more than one
            # token and still resolves. Only a lone superlative, verb or column word
            # is refused, and a user who means the garment says "tops" or names the
            # line it sits in.
            if size == 1 and (
                normalized in FALLBACK_STOPWORDS or normalized in dictionary.schema_terms
            ):
                continue

            candidates = sorted(dictionary.lookup_exact(normalized), key=_candidate_sort_key)
            if not candidates:
                continue

            # One family per span here too: an alias shared by a store and a
            # product is not a menu of both.
            winning = _family(candidates[0].entity_class)
            spans.append(
                EntitySpan(
                    text=normalized,
                    normalized=normalized,
                    start_token=start,
                    end_token=end,
                    candidates=[c for c in candidates if _family(c.entity_class) == winning],
                )
            )
            for i in range(start, end):
                consumed[i] = True

    spans = _absorb_opco_brand_tails(spans, tokens, consumed)
    spans.sort(key=lambda s: s.start_token)
    return spans, leftover_runs(tokens, consumed, dictionary.schema_terms)


def _absorb_opco_brand_tails(
    spans: list[EntitySpan], tokens: list[str], consumed: list[bool]
) -> list[EntitySpan]:
    """Pull a trailing product-line noun into the OpCo span that owns it.

    "NorthCo Credit Card" is one name. Longest-match tags "northco credit" as the OpCo,
    which is right, and then the size-1 pass tags the orphaned "card" as the enum
    value `payment_type = 'Card'`, which is not: nobody asking about NorthCo Credit
    cardholders is asking to exclude customers who paid cash.

    Absorbing the token rather than merely dropping its span also keeps it out of
    the fuzzy pass, where "card" would otherwise reach CREDIT CARD HOLDER.
    """
    if not spans:
        return spans

    opco_ends = {
        span.end_token for span in spans if span.best and span.best.entity_class == "opco"
    }
    if not opco_ends:
        return spans

    absorbed: set[int] = set()
    for end in opco_ends:
        index = end
        while index < len(tokens) and tokens[index] in OPCO_BRAND_TAIL:
            absorbed.add(index)
            consumed[index] = True
            index += 1

    if not absorbed:
        return spans

    # The OpCo span keeps its own text -- "northco credit" is what the user should see
    # it resolved -- so only the orphan spans are dropped. `consumed` already keeps
    # the absorbed tokens out of the fuzzy pass.
    return [
        span
        for span in spans
        if not set(range(span.start_token, span.end_token)) <= absorbed
    ]


def _rescored(candidate: EntityCandidate, score: float) -> EntityCandidate:
    return EntityCandidate(
        entity_class=candidate.entity_class,
        canonical_value=candidate.canonical_value,
        display_value=candidate.display_value,
        target_column=candidate.target_column,
        score=score,
        match_kind="fuzzy",
        opco_code=candidate.opco_code,
        category_key=candidate.category_key,
        category_level=candidate.category_level,
        row_context=candidate.row_context,
    )


def _family(entity_class: str) -> str:
    """Group the four category levels together.

    A span is a store or a product, and offering both in one list is not a choice
    a user can answer. Category LEVELS are a different matter: "did you mean the
    Home Fashion division or Fashion Accessories?" is a real question, so they
    stay in one list even though they filter different columns.
    """
    return "category" if entity_class.startswith("category_") else entity_class


def _candidate_sort_key(candidate: EntityCandidate) -> tuple:
    """Best score first; on a tie, the broader category and then a stable label.

    Scores are rounded so that near-identical matches genuinely tie and the
    tiebreak decides. The category tiebreak favours the shallower node because a
    broad ask ("fashion") usually means the division, and a user who wanted a leaf
    can narrow -- whereas guessing a leaf answers a question nobody asked.
    """
    return (
        -round(candidate.score, 1),
        candidate.category_level or 0,
        candidate.display_value,
    )


def _alias_shortlist(dictionary: EntityDictionary, window: tuple[str, ...]) -> set[str]:
    """Aliases sharing a word beginning with the window.

    The scorer gives near-zero to anything whose first characters differ, so this
    is a cheap way to avoid scoring 13,000 aliases per window. The index also
    keys on the sorted prefix, which catches transpositions ("bnak" -> "bank").
    """
    keys: set[str] = set()
    for token in window:
        if _is_filler(token):
            continue
        keys |= prefix_keys(token)

    shortlist: set[str] = set()
    for key in keys:
        shortlist |= dictionary.alias_prefix.get(key, frozenset())
    return shortlist


def _enum_floor() -> float:
    """An enum either matches confidently or not at all.

    The suggest floor exists so a PARTIAL name can be offered back: there are 550
    stores and 5,400 categories, a user types part of one, and "did you mean X or
    Y" is a question they can answer.

    None of that holds for an enum. The vocabulary is a few dozen short status
    labels, users take them from a dashboard and spell them correctly, and there is
    no list to recognise an answer from. A partial match is therefore not a typo,
    it is an ordinary word that happens to appear in a label -- "how many customers
    MOVED to a lower tier" was answered with "did you mean Moved Closer or Moved
    Further", offering two values of `nearest_store_change_status` to a question
    about `tier_change_status`, and "average age by GENERATION bucket" was asked
    whether it meant the Silent Generation.

    A real misspelling still resolves: "churn" scores 95 against "Churned", well
    above the confident line.
    """
    return float(settings.lookup_confident_score)


def _is_anchored(
    token: str, alias_tokens: tuple[str, ...], weak: set[str]
) -> bool:
    """Whether a one-word window is evidence for this alias at all.

    See SINGLE_TOKEN_ANCHOR_SIMILARITY. Either the word is nearly the spelling of
    a word in the name, or it is a strong match for at least half of a short name.
    """
    best = max((_token_similarity(token, a) for a in alias_tokens), default=0.0)
    if best >= SINGLE_TOKEN_ANCHOR_SIMILARITY:
        return True
    return _recall((token,), alias_tokens, weak) >= MIN_SINGLE_TOKEN_RECALL


def _score_window(
    dictionary: EntityDictionary,
    window: tuple[str, ...],
    floor: float,
    option_window: float,
    limit: int,
) -> list[EntityCandidate]:
    """Best candidates for one reading of a run, highest score first."""
    best_by_identity: dict[tuple[str, str], EntityCandidate] = {}

    for alias in _alias_shortlist(dictionary, window):
        alias_tokens = tuple(alias.split())
        by_class: dict[str, list[EntityCandidate]] = {}
        for candidate in dictionary.exact.get(alias, ()):
            by_class.setdefault(candidate.entity_class, []).append(candidate)

        for entity_class, candidates in by_class.items():
            if entity_class not in FUZZY_MATCHABLE_CLASSES:
                continue
            weak = _weak_tokens(dictionary, entity_class)
            if len(window) == 1 and not _is_anchored(window[0], alias_tokens, weak):
                continue
            score = phrase_score(window, alias_tokens, weak)
            if score < (_enum_floor() if entity_class == "enum" else floor):
                continue
            for candidate in candidates:
                key = (candidate.entity_class, candidate.canonical_value)
                current = best_by_identity.get(key)
                if current is None or score > current.score:
                    best_by_identity[key] = _rescored(candidate, score)

    if not best_by_identity:
        return []

    ranked = sorted(best_by_identity.values(), key=_candidate_sort_key)

    # One family per span. A phrase is a store or a product, not a menu of both:
    # "which store did you mean?" listing a product category as option 5 is not a
    # choice the user can answer.
    winning = _family(ranked[0].entity_class)
    ranked = [c for c in ranked if _family(c.entity_class) == winning]

    # Only candidates close to the best are real alternatives. Listing everything
    # above the floor is how "which of these five?" happened.
    cutoff = ranked[0].score - option_window
    within = [c for c in ranked if c.score >= cutoff]

    # Prune first, cap second, so the cap counts options the user can actually
    # choose between rather than ones a later rule removes.
    return without_redundant_descendants(within)[:limit]


def _best_reading_of_run(
    run: TokenRun,
    dictionary: EntityDictionary,
    limit_per_window: int,
) -> list[EntitySpan]:
    """Score every sub-window of a run and keep the best non-overlapping set.

    This is where nesting is settled. "veld nin" and "veld" are two readings of
    the same two tokens, not two entities; because one scorer produced both
    numbers, keeping the higher one is a meaningful choice rather than the
    arbitrary first-come ordering the old cascade applied.
    """
    floor = float(settings.lookup_suggest_score)
    option_window = float(settings.lookup_option_window)

    scored: list[EntitySpan] = []
    for size in range(min(len(run.tokens), MAX_FUZZY_WINDOW_TOKENS), 0, -1):
        for offset in range(0, len(run.tokens) - size + 1):
            window = run.tokens[offset : offset + size]
            if size == 1 and len(window[0]) < MIN_SINGLE_TOKEN_FUZZY_CHARS:
                continue

            # Schema vocabulary was excluded when the runs were built, so no window
            # here can contain a word that names a column or metric. See
            # leftover_runs().
            candidates = _score_window(
                dictionary, window, floor, option_window, limit_per_window
            )
            if not candidates:
                continue

            start = run.start + offset
            scored.append(
                EntitySpan(
                    text=" ".join(window),
                    normalized=" ".join(window),
                    start_token=start,
                    end_token=start + size,
                    candidates=candidates,
                )
            )

    # Best score wins; a longer reading breaks ties, since covering more of the
    # user's words at the same confidence is the better explanation.
    scored.sort(
        key=lambda s: (
            -round(s.best.score, 1),
            -(s.end_token - s.start_token),
            s.start_token,
        )
    )

    claimed: set[int] = set()
    kept: list[EntitySpan] = []
    for span in scored:
        positions = set(range(span.start_token, span.end_token))
        if positions & claimed:
            continue
        claimed |= positions
        kept.append(span)

    kept.sort(key=lambda s: s.start_token)
    return kept


def match_fuzzy(
    runs: list[TokenRun],
    dictionary: EntityDictionary,
    limit_per_window: int | None = None,
    exact_spans: list[EntitySpan] | None = None,
) -> list[EntitySpan]:
    """Best reading of each leftover run, as non-overlapping spans.

    Every candidate returned is a suggestion until `span_status` says otherwise;
    this function does not decide whether a match is good enough to filter SQL.
    """
    limit = max_options_per_span() if limit_per_window is None else limit_per_window
    spans: list[EntitySpan] = []
    for run in runs:
        spans.extend(_best_reading_of_run(run, dictionary, limit))
    return _drop_name_tails(spans, exact_spans or [])


def _drop_name_tails(
    fuzzy: list[EntitySpan], exact: list[EntitySpan]
) -> list[EntitySpan]:
    """Drop a guess that is really the tail of a name already matched.

    "NorthCo Inglegate Juniperford Megamall" is one store. Longest-match tags "inglegate juniperford"
    exactly, which is right, and leaves "megamall" over -- whereupon the fuzzy pass
    reads it as a SECOND store and offers WELLNESS JESSAMRIDGE MEGAMALL. The question
    then carried two store slots, and answering the first one emptied the second by
    OpCo pin, so the user was told "I could not find megamall within NORTHCO_MART".
    Two stores from one name, and a refusal from a question that named one place.

    Adjacency is what settles it. Two different entities of the same kind are never
    written touching -- "NorthCo Mart and NorthCo" has a connector between them, and a
    connector is filler, which ends a run. A guess that begins exactly where an
    exact match of the same kind ended is therefore a continuation of that name, not
    a new entity.

    Only same-family neighbours are dropped: "INGLEGATE JUNIPERFORD grocery" is a store and a
    product, and both are real.

    A banner touching a branch name counts as the same family even though one is
    typed `store` and the other `store_type`. "northco freshmarte rowanholt south" is one
    shop; read as two filters it became `store_type = 'SM - FRESHMART'` AND a branch
    in a different banner, which no row can satisfy. Adjacency is what makes it
    safe: "FreshMart stores in Metro Valley" has filler between the two, so the run
    breaks and both survive.
    """
    if not exact or not fuzzy:
        return fuzzy

    def family(candidate: EntityCandidate) -> str:
        # A store_type is an attribute OF a store, so a banner word beside a branch
        # name is naming the same shop, not a second thing.
        if (candidate.target_column or "").lower() == "store_type":
            return "store"
        return _family(candidate.entity_class)

    boundaries = {
        (span.start_token, family(span.best)) for span in exact if span.best
    } | {
        (span.end_token, family(span.best)) for span in exact if span.best
    }

    kept: list[EntitySpan] = []
    for span in fuzzy:
        best = span.best
        if best is not None and (
            (span.start_token, family(best)) in boundaries
            or (span.end_token, family(best)) in boundaries
        ):
            continue
        kept.append(span)
    return kept


# ---------------------------------------------------------------------------
# Span arbitration
# ---------------------------------------------------------------------------

def _column_tokens(column: str) -> tuple[str, ...]:
    return tuple(t for t in normalize(column).split() if t)


def _column_rank(column: str, question: frozenset[str]) -> tuple:
    """Preference among columns that share one enum value.

    Ordered by (1) how many words of the column name the question actually used,
    (2) how few words the column name has, (3) the name, so it is total and
    stable.

    (1) is what makes "how many members are Active" pick `member_status` and "how
    many customers were active" pick `customer_status_in_opco` -- the
    discriminating word is in the column name, and the user either said it or did
    not. (2) settles the rest towards the plain column: `payment_type` over
    `primary_payment_type`, because a qualified name is the special case and the
    question has to ask for it.

    Nothing here knows about any particular qualifier. The original example was
    `value_segment` over `previous_value_segment`, and when the prior-period columns
    were dropped from the schema this function needed no change -- it counts shared
    words, so it handles whatever qualified pairs the schema happens to have.
    """
    tokens = _column_tokens(column)
    return (-len(set(tokens) & question), len(tokens), column)


def collapse_enum_columns(span: EntitySpan, question_tokens: frozenset[str]) -> None:
    """Merge one enum value's several columns into a single candidate.

    Most enum values in this schema live in more than one column: "Active" is a
    `lifecycle_stage`, a `member_status` and a `customer_status_in_opco`; "ACS
    Credit" is both `payment_type` and `primary_payment_type`.

    They arrived here as separate candidates with identical entity_class and
    canonical_value, and dedupe_candidates -- which keys on exactly that pair --
    kept whichever happened to sort first. "How many customers are in the Elite
    segment" was answered with `previous_value_segment = 'Elite'`: last month's
    segment, presented as this month's, with nothing anywhere saying a choice had
    been made. Neither column is in the schema any more, but the failure was never
    about them -- any value carried by two columns reproduces it, and the ones
    above still are.

    So the choice is made explicitly and the alternatives are kept. The planner is
    given all of them (see ResolutionResult.filter_context) because it is the only
    stage that sees both the question and the glossary; the resolver's job is to
    establish that the VALUE is real and in scope, which it is either way.
    """
    enums = [c for c in span.candidates if c.entity_class == "enum"]
    if len(enums) < 2:
        return

    by_value: dict[str, list[EntityCandidate]] = {}
    for candidate in enums:
        by_value.setdefault(normalize(candidate.canonical_value), []).append(candidate)

    merged: dict[int, EntityCandidate] = {}
    dropped: set[int] = set()
    for group in by_value.values():
        if len(group) < 2:
            continue
        columns = sorted(
            {c.target_column for c in group if c.target_column},
            key=lambda column: _column_rank(column, question_tokens),
        )
        if len(columns) < 2:
            continue
        winner = next(c for c in group if c.target_column == columns[0])
        merged[id(winner)] = replace(winner, alternate_columns=tuple(columns[1:]))
        dropped.update(id(c) for c in group if c is not winner)

    if not merged and not dropped:
        return

    span.candidates = [
        merged.get(id(c), c) for c in span.candidates if id(c) not in dropped
    ]


def _path_parts(display: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in (display or "").split(">") if p.strip())


def without_redundant_descendants(
    candidates: list[EntityCandidate],
) -> list[EntityCandidate]:
    """Drop a category whose path merely extends another candidate's, same OpCo.

    `Appliance` and `Appliance > Appliance` are one concept at two depths, and
    offering both asks the user to choose between a thing and itself.

    Applied BEFORE the option cap, not after. Pruning afterwards spent the cap on
    options that were then thrown away: with LOOKUP_MAX_OPTIONS=10 the "fashion"
    list took ten, dropped `HARD > HOME FASHION > LETGO HOME FASHION` for being a
    child of `HARD > HOME FASHION`, and showed nine -- fewer than asked for, with
    nothing to say why, and a real tenth candidate left unseen behind it.
    """
    categories = [c for c in candidates if c.entity_class.startswith("category_")]
    if len(categories) < 2:
        return candidates

    dropped: set[int] = set()
    for candidate in categories:
        path = _path_parts(candidate.display_value)
        for other in categories:
            if other is candidate or id(other) in dropped:
                continue
            other_path = _path_parts(other.display_value)
            if (
                candidate.opco_code == other.opco_code
                and len(other_path) < len(path)
                and path[: len(other_path)] == other_path
            ):
                dropped.add(id(candidate))
                break

    if not dropped:
        return candidates
    return [c for c in candidates if id(c) not in dropped]


# Columns that all answer "where". A region and a store are the same dimension at
# two zoom levels, so naming both does not narrow anything -- it restates.
_LOCATION_COLUMNS: frozenset[str] = frozenset(
    {"store_id", "store", "store_location", "state_name", "city_name"}
)


def _dimension_family(candidate: EntityCandidate) -> str:
    column = (candidate.target_column or "").lower()
    if column in _LOCATION_COLUMNS:
        return "location"
    if candidate.entity_class.startswith("category_"):
        return "category"
    return column or candidate.entity_class


def drop_bracketed_glosses(
    spans: list[EntitySpan], bracketed: set[int]
) -> list[EntitySpan]:
    """Drop a bracketed span that restates a dimension already named outside.

    "combined revenue across NorthCo, NorthCo Mart and NorthCo Credit in the Southern
    Region (Fenwickholt/Tannermere)" resolves `store_location = 'Southern'` from the words
    outside the brackets, which is exactly right, and then matches TANNERMERE inside
    them -- a real store, exactly named, score 100. The user is asked which Tannermere
    store they meant, about a question that named a region.

    The test is not "is this word in brackets", which would lose a store somebody
    wrote as "NorthCo Mart (Veldra Selbycross)". It is whether the bracketed span answers a
    question the sentence outside has already answered. Southern is a location and
    so is Tannermere, so Tannermere is a gloss; in "NorthCo Mart (Veldra Selbycross)" the outside
    names an OpCo, which is a scope rather than a place, so the store survives.
    """
    if not bracketed or not spans:
        return spans

    outside_families = {
        _dimension_family(span.best)
        for span in spans
        if span.best
        and not set(range(span.start_token, span.end_token)) & bracketed
    }
    if not outside_families:
        return spans

    kept: list[EntitySpan] = []
    for span in spans:
        positions = set(range(span.start_token, span.end_token))
        if (
            span.best
            and positions <= bracketed
            and _dimension_family(span.best) in outside_families
        ):
            continue
        kept.append(span)
    return kept


def collapse_category_chain(span: EntitySpan) -> None:
    """Drop a category that only repeats its own ancestor.

    Asked about "appliance", NorthCo Mart offers `Appliance` and
    `Appliance > Appliance` -- a division and a group inside it with the same name.
    Both score 100, neither can win the ambiguity margin, and the user is asked
    "which one did you mean?" about two options they cannot tell apart, because
    there is nothing to tell apart: one contains the other and both are named for
    the same thing.

    The rule is structural, not lexical: a candidate is dropped only when its path
    is an extension of another candidate's path in the SAME OpCo. `Grocery`
    (NorthCo Mart) and `FOOD > GROCERY` (NorthCo) are two different divisions in two
    OpCos and stay a real question; `HARD > FLAT PRICE > FLAT PRICE > GROCERY` is
    not an extension of `Grocery` and stays too.

    The survivor is the ancestor, which is the same preference _candidate_sort_key
    already applies to ties: a broad ask usually means the division, and a user who
    wanted the narrower node can say so -- whereas guessing the leaf answers a
    question nobody asked.
    """
    span.candidates = without_redundant_descendants(span.candidates)


def dedupe_candidates(span: EntitySpan) -> None:
    """Collapse candidates that point at the same entity.

    Two rows describing one entity under different spellings are not a choice,
    and presenting them as one produces "1. NorthCo Mart 2. NorthCo Mart". Dedupe on the
    resolved identity, not the label.
    """
    seen: set[tuple[str, str]] = set()
    unique = []
    for candidate in span.candidates:
        key = (candidate.entity_class, candidate.canonical_value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    span.candidates = unique


def span_status(span: EntitySpan) -> str:
    """"resolved" if this span may become a SQL filter, else "ambiguous".

    An exact dictionary hit resolves; a fuzzy match must clear
    `lookup_confident_score` AND beat its runner-up by `lookup_ambiguity_margin`.
    Anything else is a question. This is the rule that stops a lone mediocre
    match -- "northco veld" scoring 75 against NorthCo Bank -- from silently filtering
    on the wrong entity.
    """
    dedupe_candidates(span)
    best = span.best
    if best is None:
        return "ambiguous"

    margin = (
        best.score - span.candidates[1].score if len(span.candidates) > 1 else 100.0
    )
    if margin <= float(settings.lookup_ambiguity_margin):
        return "ambiguous"
    if best.match_kind == "exact":
        return "resolved"
    return (
        "resolved"
        if best.score >= float(settings.lookup_confident_score)
        else "ambiguous"
    )


def dedupe_spans(spans: list[EntitySpan]) -> list[EntitySpan]:
    """Drop spans whose best candidate duplicates an entity already spoken for.

    Exact and fuzzy spans cover disjoint tokens by construction, so this is about
    identity, not position: two different phrases in one question resolving to the
    same store is one filter, not two.
    """
    seen: set[tuple[str, str]] = set()
    out: list[EntitySpan] = []
    for span in spans:
        best = span.best
        if best is None:
            continue
        key = (best.entity_class, best.canonical_value)
        if key in seen:
            continue
        seen.add(key)
        out.append(span)
    return out
