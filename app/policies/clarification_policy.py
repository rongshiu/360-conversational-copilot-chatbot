from __future__ import annotations

import json
import re

from app.utils.text_utils import INTERNAL_LINE_MARKER as M


_CONFIRMATION_REPLIES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "ok",
    "okay",
    "sure",
    "correct",
    "confirm",
    "confirmed",
    "proceed",
    "continue",
    "go ahead",
    "run it",
    "do it",
}

_MONTH_WORDS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
}

_ANALYTICS_INTENT_WORDS = {
    "how", "many", "count", "number", "total", "sum", "average", "avg",
    "show", "list", "give", "compare", "trend", "breakdown", "distribution",
}

_ANALYTICS_METRIC_WORDS = {
    "customer", "customers", "cust", "people", "person", "member", "members",
    "transaction", "transactions", "sale", "sales", "revenue", "spend", "spent",
    "amount", "basket", "visit", "visits", "store", "stores", "product", "products",
}

_ANALYTICS_ACTION_WORDS = {
    "buy", "buys", "bought", "purchase", "purchases", "purchased", "visit",
    "visited", "spend", "spent", "has", "have", "having", "with", "in", "by", "for",
    "are", "is", "there", "across", "over", "near", "live", "lives", "living",
}


def normalize_short_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def is_option_number(text: str | None) -> bool:
    q = normalize_short_text(text)
    return bool(re.fullmatch(r"(?:option\s*)?\d{1,2}", q))


def is_confirmation_text(text: str | None) -> bool:
    q = normalize_short_text(text)
    return q in _CONFIRMATION_REPLIES


def looks_like_time_slot_answer(text: str | None) -> bool:
    q = normalize_short_text(text)
    if not q:
        return False

    # year 2026 / 2026 / in 2026
    if re.fullmatch(r"(?:in\s+)?(?:year\s+)?20\d{2}", q):
        return True

    # oct 2026 / october 2026 / 2026 october
    tokens = q.replace(",", " ").split()
    if any(tok in _MONTH_WORDS for tok in tokens) and any(re.fullmatch(r"20\d{2}", tok) for tok in tokens):
        return True

    # this year / last year / this month / last month / ytd / mtd
    if q in {"this year", "last year", "this month", "last month", "ytd", "mtd"}:
        return True

    return False


def has_time_signal(text: str | None) -> bool:
    q = normalize_short_text(text)
    if not q:
        return False
    if re.search(r"\b(?:19|20)\d{2}\b", q):
        return True
    tokens = set(q.replace(",", " ").split())
    if tokens & _MONTH_WORDS:
        return True
    return q in {"this year", "last year", "this month", "last month", "ytd", "mtd"}


def looks_like_new_analytics_question(text: str | None) -> bool:
    """Return True when the user is asking a fresh analytics question.

    This is deliberately checked before treating a message as an answer to a
    pending clarification. It prevents stale lookup clarifications such as
    Veldra/store options from being merged into a new question like
    "how many customers buy grocery in dec 2026".
    """
    q = normalize_short_text(text)
    if not q or is_option_number(q) or is_confirmation_text(q):
        return False

    tokens = set(re.findall(r"[a-z0-9&]+", q))
    if not tokens:
        return False

    has_intent = bool(tokens & _ANALYTICS_INTENT_WORDS)
    has_metric = bool(tokens & _ANALYTICS_METRIC_WORDS)
    has_action = bool(tokens & _ANALYTICS_ACTION_WORDS)
    has_time = has_time_signal(q)

    # Full natural language metric query, e.g.
    # "how many customers buy grocery in dec 2026" or
    # "how many customers are there in northco ecosystem".
    if has_intent and has_metric and (has_action or has_time):
        return True

    # Population-count wording often has no verb besides "are/is there".
    # It must still cancel stale lookup clarification state.
    if has_intent and has_metric and ({"are", "is", "there", "across", "ecosystem"} & tokens):
        return True

    # Compact dashboard-style query, e.g. "customers grocery dec 2026".
    if has_metric and has_time and len(tokens) >= 3:
        return True

    return False


def looks_like_short_clarification_answer(text: str | None) -> bool:
    q = normalize_short_text(text)
    if not q:
        return False

    # A fresh analytics question must win over pending clarification state.
    if looks_like_new_analytics_question(q):
        return False

    if is_option_number(q) or is_confirmation_text(q) or looks_like_time_slot_answer(q):
        return True

    # Short answers to missing filter/grouping questions, e.g. "NorthCo Mart", "Retail", "Grocery".
    if "?" not in q and len(q.split()) <= 4:
        return True
    return False


def parse_last_assistant_payload(history: list[dict] | None) -> dict | None:
    if not history:
        return None

    for item in reversed(history):
        if item.get("role") != "assistant":
            continue

        content = item.get("content", "")
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue

    return None


def last_assistant_payload_index(history: list[dict] | None) -> tuple[int, dict] | tuple[None, None]:
    if not history:
        return None, None
    for idx in range(len(history) - 1, -1, -1):
        item = history[idx]
        if item.get("role") != "assistant":
            continue
        try:
            payload = json.loads(item.get("content") or "")
            if isinstance(payload, dict):
                return idx, payload
        except Exception:
            continue
    return None, None


def _parse_assistant_payload(item: dict | None) -> dict | None:
    if not isinstance(item, dict):
        return None
    if item.get("role") != "assistant":
        return None

    try:
        payload = json.loads(item.get("content") or "")
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None

def is_terminal_lookup_failure_clarify(payload: dict | None) -> bool:
    """
    Detect clarify responses that should not remain pending.

    This handles terminal lookup failures such as:
    - one lookup value was selected or resolved
    - another lookup value cannot be found under that narrowed context
    - assistant says the value could not be found in lookup master data

    In this state:
    - the next user message should start fresh
    - previous selected lookup context should not be inherited
    - the user should not continue by answering the failed product/store value
    """
    if not isinstance(payload, dict):
        return False

    if payload.get("type") != "clarify":
        return False

    answer = str(payload.get("answer") or "").lower()
    intent_reason = str(payload.get("intent_reason") or "").lower()
    text = f"{answer}\n{intent_reason}"

    return (
        "could not find" in text
        and "lookup master data" in text
    )


def _is_assistant_clarification_payload(payload: dict | None) -> bool:
    """
    Local helper for clarification-window logic.

    Do not call is_prior_assistant_clarification here because that function may
    be defined later in this file. This avoids function-order issues during
    patching.
    """
    if not isinstance(payload, dict):
        return False

    if payload.get("type") != "clarify":
        return False

    answer = str(payload.get("answer") or "").strip()
    return bool(answer)


def active_clarification_window(history: list[dict] | None) -> list[dict]:
    """
    Return only the currently active clarification chain.

    Terminal lookup failure is a hard boundary.

    Example:
    1. User asks full question.
    2. Assistant asks store clarification.
    3. User selects NorthCo Mart.
    4. Assistant fails product lookup under NorthCo Mart.
    5. User asks full question again.
    6. Assistant asks store clarification again.
    7. User selects NorthCo.

    The old NorthCo Mart selection before the terminal failure must not be recovered.
    """
    items = list(history or [])
    last_idx, last_payload = last_assistant_payload_index(items)

    if last_idx is None:
        return []

    if not _is_assistant_clarification_payload(last_payload):
        return []

    start_idx = 0

    for idx in range(last_idx - 1, -1, -1):
        item = items[idx]
        if item.get("role") != "assistant":
            continue

        payload = _parse_assistant_payload(item)

        if is_terminal_lookup_failure_clarify(payload):
            start_idx = idx + 1
            break

        if not _is_assistant_clarification_payload(payload):
            start_idx = idx + 1
            break

    return items[start_idx:]


def active_clarification_window_start_index(history: list[dict] | None) -> int | None:
    """
    Return the start index of the currently active clarification chain.

    Returns None when there is no active pending clarification.

    Terminal lookup failure is a hard boundary. If a later clarification starts
    after a terminal lookup failure, selected lookup values before that failure
    must not be reused.
    """
    items = list(history or [])
    last_idx, last_payload = last_assistant_payload_index(items)

    if last_idx is None:
        return None

    if not _is_assistant_clarification_payload(last_payload):
        return None

    start_idx = 0

    for idx in range(last_idx - 1, -1, -1):
        item = items[idx]
        if item.get("role") != "assistant":
            continue

        payload = _parse_assistant_payload(item)

        if is_terminal_lookup_failure_clarify(payload):
            start_idx = idx + 1
            break

        if not _is_assistant_clarification_payload(payload):
            start_idx = idx + 1
            break

    return start_idx


def history_without_active_clarification_window(history: list[dict] | None) -> list[dict]:
    """
    Remove the active clarification chain from history.

    Use this only when the current user message abandons the pending clarification.
    This prevents stale lookup selections from leaking into intent, slot checking,
    SQL planning, or answer-from-previous routing.
    """
    items = list(history or [])
    start_idx = active_clarification_window_start_index(items)

    if start_idx is None:
        return items

    return items[:start_idx]


def extract_lookup_phrase_from_clarification_answer(answer: str | None) -> str | None:
    """Extract the raw user phrase being clarified from a lookup prompt.

    Examples supported:
      - I found multiple possible matches for `veld northco`. Which one do you mean?
      - Which exact lookup value do you mean by `baby products`?
      - Do you mean `X` for `baby products`?

    The raw phrase is critical state. Once the user selects an option for this
    phrase, later lookup extraction must ignore this phrase in the original query.
    Otherwise fallback extraction can combine leftovers, e.g. `veld and baby`.
    """
    text = str(answer or "")
    patterns = [
        r"matches\s+for\s+`([^`]+)`",
        r"mean\s+by\s+`([^`]+)`",
        r"for\s+`([^`]+)`",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            phrase = match.group(1).strip()
            if phrase:
                return phrase
    return None


def extract_selected_lookup_option(answer: str | None, reply: str | None) -> dict[str, str] | None:
    """Extract a numbered lookup clarification selection from the prior assistant answer.

    Returns the canonical option value, optional display-only context, and the raw
    phrase from the clarification prompt. The raw phrase prevents an already
    resolved phrase from being sent back through fuzzy lookup in a later turn.

    This parser supports both old prompts:
        context only: field: store_name, opco: NorthCo Mart, store_id: 1013

    And newer clean prompts:
        - Matched field: store_name
        - OpCo: NorthCo Mart
        - Store ID: 1013

    The extra parsed metadata is used by lookup resolution to resolve store_name
    first, then constrain product lookup by selected store OpCo.
    """
    q = normalize_short_text(reply)
    match = re.fullmatch(r"(?:option\s*)?(\d{1,2})", q)
    if not match:
        return None

    wanted = match.group(1)
    raw_phrase = extract_lookup_phrase_from_clarification_answer(answer)
    lines = (answer or "").splitlines()

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        option_match = re.match(rf"^{re.escape(wanted)}\.\s*(.+?)\s*$", line)
        if not option_match:
            continue

        value = option_match.group(1).strip()
        # Remove common display-only suffixes such as "(REGION 2)" from older
        # clarification text. New prompts keep context on separate lines.
        value = re.sub(
            r"\s*\((?:opco|op\s*co|region|area|state|location|context)\s*[^)]*\)\s*$",
            "",
            value,
            flags=re.I,
        ).strip()
        if not value:
            return None

        context = ""
        context_fields: dict[str, str] = {}

        # Read all context lines under the selected option until the next option.
        j = idx + 1
        compact_context_parts: list[str] = []
        while j < len(lines):
            next_line = lines[j].strip()

            if not next_line:
                if compact_context_parts or context:
                    break
                j += 1
                continue

            if re.match(r"^\d{1,2}\.\s+", next_line):
                break

            # Old format.
            context_match = re.match(r"^context only:\s*(.+?)\s*$", next_line, flags=re.I)
            if context_match:
                context = context_match.group(1).strip()
                for part in context.split(","):
                    if ":" not in part:
                        continue
                    key, val = part.split(":", 1)
                    normalized_key = normalize_short_text(key).replace(" ", "_")
                    val = val.strip()
                    if normalized_key and val:
                        context_fields[normalized_key] = val
                break

            # New clean bullet format.
            bullet_match = re.match(r"^-\s*([^:]+):\s*(.+?)\s*$", next_line)
            if bullet_match:
                label = bullet_match.group(1).strip()
                val = bullet_match.group(2).strip()
                normalized_key = normalize_short_text(label).replace(" ", "_")
                if normalized_key and val:
                    context_fields[normalized_key] = val
                    compact_context_parts.append(f"{label}: {val}")

            j += 1

        if compact_context_parts:
            context = ", ".join(compact_context_parts)

        selected: dict[str, str] = {"value": value}
        if raw_phrase:
            selected["phrase"] = raw_phrase
        if context:
            selected["context"] = context

        # Normalize field metadata.
        field = ""
        if context_fields.get("matched_field"):
            field = context_fields["matched_field"].strip()
        elif context_fields.get("field"):
            field = context_fields["field"].strip()
        elif context:
            field_match = re.search(r"(?:^|,\s*)field:\s*([^,]+)", context, flags=re.I)
            if field_match:
                field = field_match.group(1).strip()

        if field:
            selected["field"] = field

        # Keep useful machine-readable metadata for downstream contextual lookup.
        # Store selections can provide OpCo, which should constrain product lookup.
        key_map = {
            "opco": "opco",
            "op_co": "opco",
            "opco_code": "opco_code",
            "op_co_code": "opco_code",
            "opco_name": "opco_name",
            "op_co_name": "opco_name",
            "store_id": "store_id",
            "store_name": "store_name",
            "store_location": "store_location",
            "store_type": "store_type",
            "product_line": "category_name",
            "product_division": "category_name",
            "product_group": "category_name",
            "category": "category_name",
            "product_category": "product_category",
        }
        for parsed_key, selected_key in key_map.items():
            parsed_value = context_fields.get(parsed_key)
            if parsed_value:
                selected[selected_key] = parsed_value.strip()

        return selected

    return None

def dedupe_selected_lookup_options(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Keep selected lookups unique.

    For the same clarified raw phrase and lookup field, keep only the latest
    selected value.

    Why:
    - User may first select NORTHCO MART INGLEGATE JUNIPERFORD.
    - Product lookup fails under NorthCo Mart.
    - User asks the full question again.
    - User then selects INGLEGATE JUNIPERFORD or INGLEGATE JUNIPERFORD E-COMMERCE.
    - The older NorthCo Mart selection must not remain active.

    We keep latest by:
      (field, phrase)

    This still allows different lookup phrases in the same query, for example:
      - northco inglegatejuniperford
      - northco veldra

    It also allows separate store and product selections because their fields
    differ, for example:
      - store_name / northco inglegatejuniperford
      - category / fashion
    """
    latest_by_key: dict[tuple[str, str], dict[str, str]] = {}
    key_order: list[tuple[str, str]] = []

    for item in items:
        value = str(item.get("value") or "").strip()
        if not value:
            continue

        field = str(item.get("field") or "").strip().lower()
        phrase = str(item.get("phrase") or "").strip().lower()

        # Fallback for older selections that may not have field or phrase.
        # In normal lookup clarification, both should exist.
        key = (field or "__unknown_field__", phrase or value.lower())

        if key not in latest_by_key:
            key_order.append(key)

        latest_by_key[key] = item

    return [latest_by_key[key] for key in key_order]


def find_original_user_query_for_clarification(history: list[dict] | None) -> str | None:
    """
    Find the original user question for the currently active clarification chain only.

    Do not search the whole thread, because that can revive old abandoned
    clarification questions.
    """
    items = active_clarification_window(history)

    if not items:
        return None

    last_idx, _payload = last_assistant_payload_index(items)
    if last_idx is None:
        return None

    for item in reversed(items[:last_idx]):
        if item.get("role") != "user":
            continue

        content = str(item.get("content") or "").strip()
        if not content:
            continue

        if "User provided missing clarification detail:" in content:
            continue

        if "User selected clarification option:" in content:
            continue

        if looks_like_short_clarification_answer(content):
            continue

        return content

    for item in reversed(items[:last_idx]):
        if item.get("role") != "user":
            continue

        content = str(item.get("content") or "").strip()
        if not content:
            continue

        if "User provided missing clarification detail:" in content:
            continue

        if "User selected clarification option:" in content:
            continue

        return content

    return None


def mentions_lookup_backed_field(text: str | None) -> bool:
    """True when the text names a lookup-backed column.

    Reads the glossary directly. The v2 version asked the lookup resolver for its
    lookup_fields list, which coupled a pure text policy to a service that had to
    build five indexes at import time just to answer this question.
    """
    from app.service.glossary_service import get_glossary_service
    from app.utils.text_utils import normalize_lookup_text

    q = normalize_lookup_text(text)
    if not q:
        return False

    for row in get_glossary_service().get_lookup_backed_rows():
        column = row.get("column_name") or row.get("field_name") or ""
        field = normalize_lookup_text(column)
        if not field:
            continue
        if field in q or field.replace("_", " ") in q:
            return True

    return False


def is_prior_assistant_clarification(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("type") != "clarify":
        return False
    answer = str(payload.get("answer") or "").strip().lower()
    return bool(answer)


def has_numbered_lookup_options(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False

    plan = payload.get("lookup_plan")
    if not isinstance(plan, dict):
        return False

    for slot in plan.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        if slot.get("status") == "ambiguous" and slot.get("options"):
            return True
    return False


def build_clarification_followup_query(
    *,
    current_query: str,
    history: list[dict] | None,
    previous_payload: dict | None,
) -> tuple[str, bool, str | None]:
    """
    Convert answers to a pending clarification into a concrete analytics query.

    This prevents loops such as:
      Assistant: For which time period?
      User: year 2026
      Assistant: I found fields calendar_date...

    It also prevents:
      Assistant: Ready to proceed?
      User: yes
      Assistant: Ready to proceed?
    """
    if not is_prior_assistant_clarification(previous_payload):
        return current_query, False, None

    q = str(current_query or "").strip()

    # Important: users often abandon a lookup clarification loop and ask a new
    # question in the same thread. Do not merge the new question with the old
    # unresolved lookup phrase.
    if looks_like_new_analytics_question(q):
        return current_query, False, None

    if not looks_like_short_clarification_answer(q):
        return current_query, False, None

    original_query = find_original_user_query_for_clarification(history)
    if not original_query:
        return current_query, False, None

    previous_answer = str((previous_payload or {}).get("answer") or "")
    selected_lookup = extract_selected_lookup_option(previous_answer, q)
    selected_option = selected_lookup.get("value") if selected_lookup else None

    if selected_option:
        selected_field = selected_lookup.get("field") if selected_lookup else None
        selected_context = selected_lookup.get("context") if selected_lookup else None
        selected_phrase = selected_lookup.get("phrase") if selected_lookup else None
        merged_parts = [
            original_query,
            f"{M} User selected clarification option: {selected_option}",
            f"{M} Use exactly this canonical selected value when it is a filter "
            f"value: {selected_option}",
        ]
        if selected_phrase:
            merged_parts.append(f"{M} Resolved raw lookup phrase: {selected_phrase}")
        if selected_field:
            merged_parts.append(f"{M} Selected lookup field: {selected_field}")
        if selected_context:
            merged_parts.append(f"{M} Selected option context only: {selected_context}")
        merged = "\n".join(merged_parts)
        reason = f"Resolved pending clarification with selected option `{selected_option}`."
        return merged, True, reason

    if is_confirmation_text(q) and has_numbered_lookup_options(previous_payload):
        merged = (
            f"{original_query}\n"
            f"{M} Reply with the option number only: {q}"
        )
        return (
            merged,
            True,
            "User replied with yes/no to numbered lookup options; ask for an option number.",
        )

    if is_confirmation_text(q):
        merged = (
            f"{original_query}\n"
            f"{M} User confirmed the pending clarification. Proceed with the "
            f"already clarified request."
        )
        return merged, True, "User confirmed pending clarification; proceed to SQL planning."

    if mentions_lookup_backed_field(q):
        return q, False, None

    merged = (
        f"{original_query}\n"
        f"{M} User provided missing clarification detail: {q}"
    )
    return merged, True, "User supplied missing clarification detail; proceed to SQL planning."
