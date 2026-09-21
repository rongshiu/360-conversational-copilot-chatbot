from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd


def clean_text_value(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def normalize_lookup_text(value: str | None) -> str:
    text = (value or "").lower().strip()
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parenthesised_token_indices(value: str | None) -> set[int]:
    """Indices of the tokens that sat inside brackets, over normalize_lookup_text().

    Brackets are how people gloss a term they have already named: "the Southern
    Region (Fenwickholt/Tannermere)", "Millennial (Gen Y)", "NorthCo Mart (hypermarket)". The
    words inside restate the thing outside; they are not a second thing to filter
    on. Normalisation throws the brackets away, so the gloss arrives looking exactly
    like a name the user typed -- and "Tannermere" is a real store, so a question about
    a whole region was answered with "which TANNERMERE store did you mean?".

    Tokenising here mirrors normalize_lookup_text exactly: it lowercases, and every
    character that is not a letter or digit separates tokens (underscores included).
    The index sequence therefore lines up 1:1 with `normalize_lookup_text(v).split()`,
    which the caller checks before trusting it.
    """
    text = (value or "").lower()
    inside: set[int] = set()
    depth = 0
    index = -1
    in_token = False

    for char in text:
        if char in "([{":
            depth += 1
            in_token = False
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            in_token = False
            continue

        if char.isalnum() and char.isascii():
            if not in_token:
                index += 1
                in_token = True
                if depth > 0:
                    inside.add(index)
        else:
            in_token = False

    return inside


def parse_enum_values(raw: Any) -> list[str]:
    text = clean_text_value(raw)
    if not text:
        return []
    normalized_quotes = (
        text.replace("“", "\"")
        .replace("”", "\"")
        .replace("‘", "'")
        .replace("’", "'")
    )
    try:
        parsed = json.loads(normalized_quotes)
        if isinstance(parsed, list):
            return [clean_text_value(x) for x in parsed if clean_text_value(x)]
    except Exception:
        pass
    return [clean_text_value(x) for x in re.split(r"[,|]", text) if clean_text_value(x)]


GENERIC_LOOKUP_TOKENS = {
    "northco", "co", "mart", "credit", "retail", "store", "stores", "branch", "branches",
    "outlet", "outlets", "mall", "malls", "primary", "preferred", "location", "locations",
    "customer", "customers", "cust", "people", "person", "user", "users", "has", "have", "having", "do", "does", "did", "doing",
    "tell", "me", "please", "pls", "kindly", "want", "need",
    "buy", "buys", "bought", "purchased", "purchase",
    "how", "many", "count", "number", "of", "the", "a", "an",
    "in", "at", "by", "for", "with", "jan", "january", "feb", "february", "mar", "march",
    "apr", "april", "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
}

# Closed-class English words: pronouns, determiners, prepositions, conjunctions,
# auxiliaries, quantifiers, degree adverbs. Listed as a class rather than
# discovered one at a time, because the failures they cause all look the same and
# arrive one question at a time: "which daypart performed best" matched a product
# called JIMROSA BEST BUY, and "the same period last year" matched BEE SAME. Both
# are four-letter function words that happen to appear in one category name out of
# 5,240.
#
# Only function words belong here. Content words are excluded even when they are
# common English -- "home", "beauty", "kids", "ladies" and "food" are all real
# category names, and blocking them would break the lookups this exists to serve.
FUNCTION_WORDS = {
    # Pronouns and determiners
    "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "yours",
    "he", "him", "his", "she", "hers", "it", "its", "they", "them", "theirs",
    "each", "every", "either", "neither", "other", "others", "another", "such",
    "own", "same", "any", "some", "all", "none", "no", "nothing", "something",
    "anything", "everything", "someone", "anyone", "everyone",

    # Prepositions and particles
    "to", "of", "in", "on", "at", "by", "for", "with", "without", "within",
    "into", "onto", "from", "up", "down", "out", "off", "over", "under",
    "above", "below", "across", "through", "between", "among", "against",
    "about", "around", "near", "toward", "towards", "per", "via", "plus",

    # Conjunctions and subordinators
    "and", "or", "but", "nor", "so", "yet", "if", "unless", "because", "since",
    "while", "whereas", "though", "although", "whether", "as", "than", "that",

    # Auxiliaries and copulas
    "be", "am", "is", "are", "was", "were", "been", "being", "have", "has",
    "had", "having", "do", "does", "did", "doing", "done", "will", "would",
    "shall", "should", "can", "could", "may", "might", "must", "ought",

    # Quantifiers and degree adverbs
    "more", "less", "fewer", "much", "many", "lot", "lots", "several", "few",
    "little", "enough", "very", "quite", "rather", "really", "too", "also",
    "just", "only", "even", "still", "already", "almost", "nearly", "about",
    "approximately", "roughly", "exactly", "not", "never", "always", "ever",
    "often", "sometimes", "usually", "rarely", "again", "once", "twice",

    # Interrogatives and discourse glue
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "here", "there", "then", "now", "thus", "hence", "therefore", "however",
    "please", "thanks", "thank", "ok", "okay", "yes", "yeah", "yep", "nope",
}

# These are generic natural-language intent/stop words, not business values.
# Business values still come only from table_glossary + copilot_lookup_value.
#
# Only the fuzzy pass of entity resolution consults this list, to decide which
# stretches of a question could hold a misspelt entity name. Exact matching reads
# the raw tokens, so a store or category genuinely named after one of these words
# still resolves -- listing a word here means "do not GUESS from it", not "this
# word can never be an entity".
FALLBACK_STOPWORDS = GENERIC_LOOKUP_TOKENS | {
    # Connectors / clause glue. These must never become lookup slots.
    "and", "or", "both", "either", "neither", "versus", "vs",
    "between", "among", "also", "then", "than",

    # Generic analytics/action words.
    "visit", "visited", "visits", "visiting", "visted",
    "transact", "transacted", "transacts", "transaction", "transactions",
    "spend", "spent", "sales", "sale", "revenue", "amount", "value",

    # Verbs of holding, using and acquiring. These are how a question says what a
    # customer DID with a product, never what the product is called, and each one
    # is a four-plus-letter word that some category name contains a stem of:
    # "how many hold an active NorthCo Credit card" offered CREDIT CARD HOLDER and
    # NAME CARD HOLDER, because "hold" looks 0.8 like "holder".
    "hold", "holds", "held", "holding", "holder", "holders",
    "own", "owns", "owned", "owning", "owner", "owners",
    "use", "uses", "used", "using", "usage",
    "carry", "carries", "carried", "carrying",
    "keep", "keeps", "kept", "keeping",
    "buying", "purchasing", "purchases",
    "shop", "shops", "shopped", "shopping", "shopper", "shoppers",
    "stop", "stops", "stopped", "stopping",
    "sign", "signed", "signup", "subscribe", "subscribed", "subscription",
    "apply", "applies", "applied", "applying",

    # Generic product/lookup column words.
    "item", "items", "product", "products", "category", "categories",
    "group", "groups", "division", "divisions", "line", "lines",

    # Generic grammar / metric words.
    "related", "relation", "about", "into", "from", "on", "per", "during",
    "there", "are", "is", "was", "were", "recorded", "total", "distinct",

    # Generic follow-up/pronoun words. These are especially important after an
    # analytics answer, e.g. `what are their name` must be treated as a SQL
    # follow-up/listing request, not as product_category ~= NAME CARD HOLDER.
    "name", "names", "their", "them", "they", "these", "those", "this", "that",
    "what", "which", "who", "whom", "whose", "show", "list", "give", "get",

    # Comparison and superlative words. "which daypart performed best" asked
    # about a product called JIMROSA BEST BUY, because "best" is a four-letter
    # word that happens to appear in one category name.
    "best", "worst", "better", "worse", "top", "bottom", "highest", "lowest",
    "high", "low", "most", "least", "biggest", "largest", "smallest",
    "perform", "performs", "performed", "performing", "performance",
    "compare", "compared", "comparing", "comparison", "against",
    "rank", "ranked", "ranking", "breakdown", "broken", "split", "share",

    # Metric vocabulary. These belong to the glossary and metric registry, which
    # map them to columns; the entity resolver must never read them as names.
    "daypart", "dayparts", "morning", "afternoon", "evening", "night",
    "weekday", "weekdays", "weekend", "weekends",
    "penetration", "member", "members", "membership", "nonmember", "nonmembers",
    "basket", "baskets", "average", "avg", "mean", "median",
    "growth", "decline", "increase", "decrease", "trend", "trends",
    "ratio", "rate", "percent", "percentage", "pct", "overlap", "overlapping",
    "lifecycle", "lifecycles", "stage", "stages", "segment", "segments",
    "tier", "tiers", "status", "statuses", "attribute", "attributes",

    # Time vocabulary. Months are already covered above; these are the rest.
    "day", "days", "daily", "week", "weeks", "weekly", "month", "months",
    "monthly", "year", "years", "yearly", "annual", "quarter", "quarterly",
    "today", "yesterday", "tomorrow", "ytd", "mtd", "period", "periods",
    "last", "previous", "prior", "current", "recent", "ago", "since", "until",
    "before", "after", "date", "dates", "time", "times",
} | FUNCTION_WORDS


# Every line the copilot synthesises into a merged clarification query carries
# this marker, and strip_internal_lookup_meta drops the whole line on sight.
#
# The phrase list below came first and failed in the way literal lists do. The
# builder wrote "User confirmed the pending clarification. Proceed with the
# already clarified request." while the list held "resolved pending clarification"
# and "proceed to sql planning" -- close enough to look maintained, different
# enough to match nothing. So a "yes" reply reached the entity resolver carrying
# the word "proceed", which fuzzy-matched FOOD > PERISHABLE > SEAFOOD > PROCESSED
# and became a hard filter on seafood in a question about a date range.
#
# A marker cannot drift: the line is internal because it says so, not because
# somebody kept two files in step.
INTERNAL_LINE_MARKER = "[internal]"

INTERNAL_LOOKUP_META_PHRASES = (
    "user provided missing clarification detail",
    "user selected clarification option",
    "use exactly this canonical selected value",
    "selected lookup field",
    "selected option context only",
    "resolved pending clarification",
    "resolved raw lookup phrase",
    "lookup entity resolution needs clarification",
    "proceed to sql planning",
    "reply with the option number only",
)


def internal_free_text(value: str | None) -> str:
    """The user's own words, with punctuation intact.

    Split out of strip_internal_lookup_meta so that anything needing to reason
    about the ORIGINAL characters -- brackets, in particular, which normalisation
    destroys -- works from the same text the resolver ends up matching on. Without
    it, token positions computed from the raw query drift out of step the moment a
    clarification merge adds a line, and any rule keyed on position silently stops
    firing on exactly the turns that replay the original question.
    """
    cleaned = clean_text_value(value)
    if not cleaned:
        return ""

    # If an internally merged graph query arrives here, keep only the original
    # user-facing line. The metadata lines are instructions, not lookup values.
    user_lines: list[str] = []
    for line in cleaned.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        if INTERNAL_LINE_MARKER in line_clean.lower():
            continue
        # Phrase matching stays for threads checkpointed before the marker existed.
        line_norm = normalize_lookup_text(line_clean)
        if any(meta in line_norm for meta in INTERNAL_LOOKUP_META_PHRASES):
            continue
        user_lines.append(line_clean)

    return " ".join(user_lines) if user_lines else cleaned


def strip_internal_lookup_meta(value: str | None) -> str:
    cleaned = internal_free_text(value)
    if not cleaned:
        return ""
    cleaned_norm = normalize_lookup_text(cleaned)
    for meta in INTERNAL_LOOKUP_META_PHRASES:
        cleaned_norm = cleaned_norm.replace(meta, " ")
    return re.sub(r"\s+", " ", cleaned_norm).strip()


def safe_schema_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""):
        return "public"
    return value
