FASTAPI_CONFIGS = {
    "title": "Customer Intelligence Copilot",
    "description": "Customer-intelligence-only copilot using Vertex AI Gemini, PostgreSQL analytics, and Postgres checkpoint memory.",
    "version": "0.1.0",
}

# Everything else that used to live here was lexicon for the v2 lookup resolver:
# PRODUCT_FIELDS, STORE_FIELDS, PRODUCT_LEVEL_WORD_TO_FIELD,
# PRODUCT_LEVEL_COMPACT_SUFFIXES, PRODUCT_ANCHORS, TIME_WORDS, BUSINESS_PREFIXES,
# GLOSSARY_ENUM_DOMAIN_WORDS and LOOKUP_ROW_KEYS -- all verified unused after the
# resolver rewrite.
#
# They are gone because the v3 entity resolver derives all of it from data. Level
# names come from dim_product_category.category_level, weak tokens from document
# frequency measured over the store-name corpus, and candidates are typed at the
# source so no anchor or domain-word list is needed to disambiguate them. Adding a
# banner or a hierarchy level no longer requires a code change here.
