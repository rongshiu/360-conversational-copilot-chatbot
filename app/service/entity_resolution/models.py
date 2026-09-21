# app/service/entity_resolution/models.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# Typed at the source, which is the point. The v2 resolver searched one untyped
# blob and then subtracted wrong answers with three separate negative filters
# (known opco values, business aliases, "normal" glossary enums) to stop e.g.
# "Elite" matching a store name. When every candidate carries its class, those
# filters are unnecessary: a value_segment enum can never be returned for a store
# span because it was never in the store index.
EntityClass = Literal[
    "store",
    "category_l1",
    "category_l2",
    "category_l3",
    "category_l4",
    "opco",
    "enum",
]

CATEGORY_CLASSES: tuple[str, ...] = (
    "category_l1",
    "category_l2",
    "category_l3",
    "category_l4",
)

MatchKind = Literal["exact", "fuzzy"]
SlotStatus = Literal["pending", "resolved", "ambiguous"]


@dataclass(frozen=True)
class EntityCandidate:
    """One possible canonical value for a span of the user's query."""

    entity_class: str
    canonical_value: str          # the value to put in SQL
    display_value: str            # the value to show a human
    target_column: str            # which column it filters
    score: float                  # 0-100
    match_kind: MatchKind
    opco_code: Optional[str] = None
    category_key: Optional[int] = None
    category_level: Optional[int] = None
    row_context: dict[str, Any] = field(default_factory=dict)
    # Other columns holding the same value, best first. Only enums have these:
    # "Active" is a lifecycle_stage, a member_status and a customer_status_in_opco.
    # See matcher.collapse_enum_columns for why they are carried rather than
    # discarded.
    alternate_columns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_class": self.entity_class,
            "canonical_value": self.canonical_value,
            "display_value": self.display_value,
            "target_column": self.target_column,
            "score": round(float(self.score), 1),
            "match_kind": self.match_kind,
            "opco_code": self.opco_code,
            "opco_name": self.row_context.get("opco_name"),
            "category_key": self.category_key,
            "category_level": self.category_level,
            "alternate_columns": list(self.alternate_columns),
        }


@dataclass
class EntitySpan:
    """A stretch of the query that looks like a named entity."""

    text: str                     # the raw substring
    normalized: str               # normalized form used for matching
    start_token: int
    end_token: int                # exclusive
    candidates: list[EntityCandidate] = field(default_factory=list)

    @property
    def best(self) -> Optional[EntityCandidate]:
        return self.candidates[0] if self.candidates else None

    @property
    def token_count(self) -> int:
        return self.end_token - self.start_token


class ResolvedSlot(BaseModel):
    """A resolution carried across clarification turns.

    Kept as a pydantic model because it is checkpointed into LangGraph state and
    round-trips through JSON between turns.
    """

    slot_id: str
    phrase: str
    entity_class: str
    target_column: str
    status: SlotStatus = "pending"
    canonical_value: Optional[str] = None
    display_value: Optional[str] = None
    category_key: Optional[int] = None
    opco_code: Optional[str] = None
    options: list[dict[str, Any]] = Field(default_factory=list)
    # Columns other than target_column that hold this same value. Enums only.
    alternate_columns: list[str] = Field(default_factory=list)


class ResolutionPlan(BaseModel):
    """All entity slots for the current question."""

    status: SlotStatus = "pending"
    slots: list[ResolvedSlot] = Field(default_factory=list)

    @property
    def pending(self) -> list[ResolvedSlot]:
        return [s for s in self.slots if s.status != "resolved"]

    @property
    def resolved(self) -> list[ResolvedSlot]:
        return [s for s in self.slots if s.status == "resolved"]

    def to_public_dict(self) -> dict[str, Any]:
        return self.model_dump()


@dataclass
class ResolutionResult:
    plan: ResolutionPlan
    spans: list[EntitySpan]
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    # out_of_scope_phrases, out_of_scope_categories and overlap_opcos were here.
    # All three described entities the caller's grant excluded -- reported as
    # out-of-scope rather than "not found", because with an RLS-scoped dictionary
    # the two were indistinguishable from the resolver's side and are very
    # different answers. Every entity is now in every caller's dictionary, so an
    # unresolved phrase is simply unrecognised.

    def filter_context(self) -> str:
        """The block handed to the planner: phrase -> canonical column and value.

        An enum value that several columns share is reported with the alternatives
        named. The resolver establishes that the VALUE exists and is in scope; only
        the planner sees the question and the glossary together, so it is the stage
        that can tell `member_status = 'Active'` from `lifecycle_stage = 'Active'`.
        Pinning one silently is how "customers in the Elite segment" became a filter
        on last month's segment.
        """
        lines: list[str] = []
        for slot in self.plan.resolved:
            line = (
                f'- "{slot.phrase}" resolves to {slot.target_column} = '
                f"'{slot.canonical_value}'"
                + (f" (category_key {slot.category_key})" if slot.category_key else "")
            )
            if slot.alternate_columns:
                line += (
                    f" -- the same value also exists in "
                    f"{', '.join(slot.alternate_columns)}; use whichever column the "
                    f"question means, defaulting to {slot.target_column}"
                )
            lines.append(line)
        if not lines:
            return ""
        return "Resolved entity filters (use these exact values):\n" + "\n".join(lines)


def _public_slot_value(slot: dict[str, Any]) -> dict[str, Any]:
    public = dict(slot)

    if public.get("entity_class") == "store":
        display = public.get("display_value") or "Selected store"
        public["target_column"] = "store"
        public["canonical_value"] = display

    options: list[dict[str, Any]] = []
    for option in public.get("options") or []:
        public_option = dict(option)
        if public_option.get("entity_class") == "store":
            display = public_option.get("display_value") or "Selected store"
            public_option["target_column"] = "store"
            public_option["canonical_value"] = display
        options.append(public_option)
    public["options"] = options

    return public


def public_lookup_slots(slots: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return lookup slots safe to expose to clients.

    The planner needs internal keys such as store_id. Users should only see
    branch names, so store canonical values are rewritten to their display labels
    in response payloads.
    """
    return [_public_slot_value(slot) for slot in (slots or [])]


def public_lookup_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not plan:
        return None
    public = dict(plan)
    public["slots"] = public_lookup_slots(public.get("slots") or [])
    return public


def public_lookup_context(plan: dict[str, Any] | None, fallback: str = "") -> str:
    if not plan:
        return fallback

    lines: list[str] = []
    for slot in plan.get("slots") or []:
        if slot.get("status") != "resolved":
            continue

        phrase = slot.get("phrase")
        if not phrase:
            continue

        entity_class = slot.get("entity_class")
        if entity_class == "store":
            display = slot.get("display_value") or "selected store"
            lines.append(f'- "{phrase}" resolves to store "{display}"')
        elif entity_class == "opco":
            lines.append(
                f'- "{phrase}" resolves to opco_code = '
                f"'{slot.get('canonical_value')}'"
            )
        else:
            display = slot.get("display_value") or slot.get("canonical_value")
            target = slot.get("target_column") or entity_class or "value"
            lines.append(f'- "{phrase}" resolves to {target} = "{display}"')

    if not lines:
        return ""
    return "Resolved entity filters:\n" + "\n".join(lines)


def _store_display_by_internal_id(plan: dict[str, Any] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for slot in (plan or {}).get("slots") or []:
        items = [slot] + list(slot.get("options") or [])
        for item in items:
            if item.get("entity_class") != "store":
                continue
            internal = str(item.get("canonical_value") or "").strip()
            display = str(item.get("display_value") or "").strip()
            if internal and display and internal != display:
                mapping[internal] = display
    return mapping


def _redact_store_id_text(text: str, mapping: dict[str, str]) -> str:
    out = text
    for internal, display in mapping.items():
        escaped = re.escape(internal)
        quoted_display = f'"{display}"'
        replacements = [
            (rf"\bstore\s+ID\s+['\"]?{escaped}['\"]?", f"store {quoted_display}"),
            (rf"\bstore\s+id\s+['\"]?{escaped}['\"]?", f"store {quoted_display}"),
            (rf"\bstore_id\s*(?:=|is|:)?\s*['\"]?{escaped}['\"]?", f"store {quoted_display}"),
            (rf"\bstore\s+key\s+['\"]?{escaped}['\"]?", f"store {quoted_display}"),
        ]
        for pattern, replacement in replacements:
            out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out


def redact_internal_store_ids(value: Any, plan: dict[str, Any] | None) -> Any:
    """Hide internal store IDs from user-facing response payloads.

    SQL execution still uses `store_id`; this only rewrites the serialized
    response. If the `sql` field itself contains a store key, it is hidden rather
    than replaced with non-executable SQL.
    """
    mapping = _store_display_by_internal_id(plan)
    if not mapping:
        return value

    def redact(item: Any, key: str | None = None) -> Any:
        if isinstance(item, str):
            if key == "sql" and any(internal in item for internal in mapping):
                return None
            return _redact_store_id_text(item, mapping)

        if isinstance(item, list):
            return [redact(child) for child in item]

        if isinstance(item, dict):
            cleaned: dict[str, Any] = {}
            for child_key, child_value in item.items():
                if child_key == "sql" and isinstance(child_value, str):
                    cleaned[child_key] = redact(child_value, key="sql")
                    continue
                if child_key.lower().endswith("store_id"):
                    child_text = str(child_value)
                    cleaned[child_key] = mapping.get(child_text, child_value)
                    continue
                cleaned[child_key] = redact(child_value, key=child_key)
            return cleaned

        return item

    return redact(value)
