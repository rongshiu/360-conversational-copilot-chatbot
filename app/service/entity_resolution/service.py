# app/service/entity_resolution/service.py
"""Scoped deterministic entity resolver.

Replaces the 2,633-line lookup_resolution package. What changed and why:

  - No LLM anywhere in resolution. The old resolver asked a model to extract
    entity phrases, cleaned up the output, then ran two
    `_patch_exact_known_*_if_missing` passes to recover what the model missed.
    Over a closed 6,000-string vocabulary that is the wrong tool; longest-match
    span tagging finds them deterministically, and a tie becomes a clarification
    question rather than a guess.

  - No negative filters. Candidates are typed at the source, so a value_segment
    enum can never surface as a store match. The three subtractive index builds
    (known opco values, business aliases, "normal" glossary enums) are gone.

  - Bounded lookup complexity. Span matching can find many entities cheaply, but
    the copilot only carries `lookup_max_slots_per_question` dimension-value
    filters -- stores and products -- so SQL planning stays predictable. An OpCo
    does not count against that budget: it is a permission scope, not a lookup
    value.

  - One reading per phrase. The matcher settles competing interpretations of the
    same words before returning, so a single misspelt name cannot arrive here as
    several findings and quietly exhaust the budget.

  - Scoped. The dictionary is built through RLS, so an out-of-scope entity is
    invisible rather than filtered late -- it can no longer leak into a
    clarification question.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings
from app.core.logging import Logger
from app.policies.clarification_policy import is_confirmation_text, is_option_number
from app.service.entity_resolution.dictionary import get_dictionary, normalize
from app.service.entity_resolution.matcher import (
    collapse_category_chain,
    max_options_per_span,
    collapse_enum_columns,
    dedupe_spans,
    drop_bracketed_glosses,
    match_exact,
    match_fuzzy,
    restrict_runs,
    span_status,
)
from app.service.entity_resolution.models import (
    EntitySpan,
    ResolutionPlan,
    ResolutionResult,
    ResolvedSlot,
)
from app.utils.text_utils import (
    FALLBACK_STOPWORDS,
    clean_text_value,
    internal_free_text,
    parenthesised_token_indices,
    strip_internal_lookup_meta,
)

logger = Logger.get_logger(__name__)



# First-person reference means "the caller's own OpCo". In "how many of OUR
# Fashion customers are also NorthCo Credit customers" the granted side is never
# named, so requiring an explicit granted OpCo refused the very question the
# overlap exception exists for. These are closed-class pronouns, matched by set
# membership on tokens -- not a phrasing heuristic about how overlap questions
# tend to be worded.
SELF_REFERENCE: frozenset[str] = frozenset(
    {"our", "ours", "my", "mine", "we", "us", "i", "own"}
)

# Order entities are resolved and asked about, regardless of where they appear in
# the sentence. Not cosmetic: an OpCo or a store PINS the OpCo, and that pin is
# what narrows every later option list. Asked "customers who visited NorthCo
# inglegatejuniperford purchased lamb", picking the NorthCo store first means the lamb
# options can drop the NorthCo Mart ones -- whereas asking about lamb first has
# nothing to narrow with. Word order must not decide this.
_SLOT_PRIORITY: dict[str, int] = {"opco": 0, "store": 1, "category": 2, "enum": 3}

# What to call each class when asking the user about it. Everything that is not a
# store, an OpCo or a customer attribute is a product category, which is why that
# is the default. Calling a `nearest_store_change_status` value a "product" -- "I
# found more than one product matching 'moved'" -- was not a wording slip so much
# as a question the user could not make sense of.
_CLARIFY_NOUN: dict[str, str] = {"store": "store", "opco": "OpCo", "enum": "value"}


def _entity_family(entity_class: str) -> str:
    return "category" if entity_class.startswith("category_") else entity_class


def _slot_priority(entity_class: str) -> int:
    return _SLOT_PRIORITY.get(_entity_family(entity_class), 9)


class EntityResolver:
    async def resolve(
        self,
        db: AsyncSession,
        query: str,
        principal: Any,
        previous_plan: Optional[dict[str, Any]] = None,
        selected_option: Optional[str] = None,
        entity_phrases: Optional[list[str]] = None,
    ) -> ResolutionResult:
        """Resolve every entity mention in the query against the caller's scope.

        Must be called inside copilot_scope() -- the dictionary and the fuzzy
        fallback both read through RLS.

        `entity_phrases` is what the routing agent read as naming something. It
        bounds the FUZZY pass only, and the distinction between None and [] is
        load-bearing: None means the router had no opinion (or its call failed), so
        resolution proceeds as it would have anyway; [] means it read the question
        and found nothing named, so nothing is guessed at.
        """
        plan = self._coerce_plan(previous_plan)

        clean = strip_internal_lookup_meta(query) or clean_text_value(query)
        if not clean:
            return ResolutionResult(plan=plan, spans=[])

        dictionary = await get_dictionary(db, principal.scope_signature())
        self._restore_internal_store_values(plan, dictionary)

        # A short reply answering a previous clarification resolves that slot
        # rather than starting a new extraction.
        if selected_option and plan.pending:
            resolved = self._apply_selection(plan, selected_option, dictionary)
            if resolved:
                return self._finalize(plan, [])
            return self._finalize(plan, [], invalid_selection=selected_option)

        # Two passes, both returning spans that cover disjoint tokens: exact
        # dictionary hits first, then the best reading of whatever tokens are
        # left. The matcher settles competing readings of the same words, so a
        # single misspelt name can no longer arrive here as three findings.
        spans, leftover = match_exact(clean, dictionary)

        # Exact matching is never gated. A model that omits a phrase must not be
        # able to make a filter silently disappear -- a missing filter is a wrong
        # number, where a missing guess is at worst a question not asked.
        if leftover and entity_phrases is not None:
            allowed = frozenset(
                token
                for phrase in entity_phrases
                for token in normalize(phrase).split()
                if token
            )
            leftover = restrict_runs(leftover, allowed) if allowed else []

        if leftover:
            spans.extend(match_fuzzy(leftover, dictionary, exact_spans=spans))
        spans = dedupe_spans(spans)

        # Words inside brackets gloss what the sentence already said. Positions come
        # from the same internal-metadata-free text that produced `clean`, so a
        # clarification merge cannot shift them; the equality check is a backstop
        # for the legacy phrase-removal path, which edits normalized text.
        user_text = internal_free_text(query)
        bracketed = parenthesised_token_indices(user_text)
        if bracketed and normalize(user_text).split() == clean.split():
            spans = drop_bracketed_glosses(spans, bracketed)

        # Several columns can hold one enum value. Settle which one this question
        # is about here, where the whole question is in hand, and keep the rest.
        #
        # Filler is excluded from that comparison. It is matched against column
        # NAMES, and `customer_status_in_opco` contains "in": "how many customers
        # are active in NorthCo Mart" scored a point for a preposition and picked that
        # column over `member_status` on the strength of it.
        question_tokens = frozenset(
            token for token in normalize(clean).split() if token not in FALLBACK_STOPWORDS
        )
        for span in spans:
            collapse_enum_columns(span, question_tokens)
            collapse_category_chain(span)

        # Stores and OpCos before products, whatever the word order: the first
        # resolution pins the OpCo, and the pin narrows what comes after. Also
        # makes the slot budget spend deterministic.
        spans.sort(
            key=lambda s: (_slot_priority(s.best.entity_class), s.start_token)
            if s.best
            else (9, s.start_token)
        )

        # _scope_classification() and _out_of_scope_categories() used to run here.
        # They split the OpCos and categories a question named into "granted",
        # "foreign but permitted for an overlap count", and "outside the grant" --
        # a distinction that only existed because an entity the grant excluded
        # never entered the RLS-scoped dictionary and so arrived indistinguishable
        # from a typo. Every entity is now in every caller's dictionary, so an
        # unresolved phrase is genuinely unrecognised and there is nothing to
        # classify.

        # Carry forward anything already resolved in an earlier turn so a
        # follow-up that adds one store does not lose the first.
        existing = {(s.entity_class, s.canonical_value) for s in plan.resolved}
        overflow: list[tuple[str, str]] = []

        for index, span in enumerate(spans):
            best = span.best
            if best is None:
                continue
            if (best.entity_class, best.canonical_value) in existing:
                continue

            slot_id = f"{best.entity_class}:{normalize(span.text) or index}"

            if span_status(span) == "resolved":
                slot = self._resolved_slot(
                    slot_id=slot_id, phrase=span.text, candidate=best
                )
            else:
                slot = ResolvedSlot(
                    slot_id=slot_id,
                    phrase=span.text,
                    entity_class=best.entity_class,
                    target_column=best.target_column,
                    status="ambiguous",
                    options=[
                        c.to_dict() for c in span.candidates[: max_options_per_span()]
                    ],
                )

            if not self._append_lookup_slot(plan, slot):
                overflow.append((self._span_label(span), best.entity_class))
                continue

            if slot.status == "resolved":
                existing.add((best.entity_class, best.canonical_value))

        if overflow:
            return self._too_many_lookup_values_result(plan, spans, overflow)

        return self._finalize(plan, spans)

    @staticmethod
    def _pinned_opcos(plan: ResolutionPlan, *, named_only: bool = False) -> set[str]:
        """OpCos already fixed by a resolved slot.

        `named_only` keeps just the OpCos the user actually NAMED, ignoring the one
        a chosen store happens to imply. See _apply_opco_pin for why the difference
        matters.
        """
        return {
            slot.opco_code
            for slot in plan.resolved
            if slot.opco_code and (not named_only or slot.entity_class == "opco")
        }

    @classmethod
    def _apply_opco_pin(cls, plan: ResolutionPlan) -> None:
        """Drop options that contradict an OpCo already resolved in this plan.

        A resolved store fixes the OpCo, so every later option list must respect
        it. It did not: picking WELLNESS INGLEGATEJUNIPERFORD (NorthCo) and then being asked
        about "lamb" still offered "Fresh > Fresh > Lamb (NorthCo Mart)". Choosing it
        would have produced a store in one OpCo and a category in another -- a
        combination no row can satisfy, so the answer would have been another
        zero that looks real.

        Applied on every turn rather than at option-build time, because the
        options carried in the plan were computed before the store was picked.

        A STORE is pinned only by an OpCo the user named, never by another store.
        The reasoning above is about a conjunction: a store AND a category have to
        sit on the same row, so they must share an OpCo. Two stores are alternatives
        -- `store_id IN (...)` -- and comparing branches across banners is an
        ordinary question. Pinning them alike made "member penetration for NorthCo Mart
        VELDRA SELBYCROSS compared with GLEDEHOLT VANTRYTON" unanswerable: choosing the NorthCo Mart
        store emptied every GLEDEHOLT VANTRYTON option, because all of them are NorthCo, and
        the turn came back asking again with nothing to offer.
        """
        pinned = cls._pinned_opcos(plan)
        named = cls._pinned_opcos(plan, named_only=True)
        if not pinned:
            return

        for slot in plan.slots:
            if slot.status != "ambiguous" or not slot.options:
                continue

            scope = named if slot.entity_class == "store" else pinned
            if not scope:
                continue

            kept = [
                option
                for option in slot.options
                if not option.get("opco_code") or option.get("opco_code") in scope
            ]
            if len(kept) == len(slot.options):
                continue

            slot.options = kept

            # One survivor is no longer a choice -- asking would be a question
            # with a single answer.
            if len(kept) == 1 and kept[0].get("match_kind") == "exact":
                cls._adopt_option(slot, kept[0])

    @staticmethod
    def _adopt_option(slot: ResolvedSlot, option: dict[str, Any]) -> None:
        """Resolve a slot to one of its options, taking the option's OWN identity.

        entity_class has to move with the rest of it. The slot is created from the
        BEST candidate of the span, and the user may well pick a different one --
        "fashion" offers HARD > HOME FASHION at level 2 and FASHION ACCESSORIES at
        level 4. Copying everything except the class left a slot reading
        entity_class=category_l2 while its target_column was category_l4_key.

        That is not cosmetic. LookupNodesMixin._category_depth_notes reads the level
        out of entity_class to decide whether the customer tables can answer at that
        depth, so a stale category_l2 silenced the warning for exactly the case it
        exists to catch: a pick deeper than the bridge stores, answerable only by
        rolling up to an ancestor.
        """
        slot.status = "resolved"
        slot.entity_class = option.get("entity_class") or slot.entity_class
        slot.target_column = option.get("target_column") or slot.target_column
        slot.canonical_value = option.get("canonical_value")
        slot.display_value = option.get("display_value")
        slot.category_key = option.get("category_key")
        slot.opco_code = option.get("opco_code")
        slot.options = []

    @classmethod
    def _ambiguous_in_ask_order(cls, plan: ResolutionPlan) -> list[ResolvedSlot]:
        """Unresolved slots, highest priority first.

        Both the question and the reply that answers it must use this order, or a
        numbered answer would be applied to a different slot than the one asked
        about.
        """
        return sorted(
            (s for s in plan.slots if s.status == "ambiguous"),
            key=lambda s: _slot_priority(s.entity_class),
        )

    @staticmethod
    def _named_opcos(query: str, dictionary, principal) -> tuple[list[str], list[str]]:
        """Split the OpCos a question names into (granted, foreign).

        Only OpCos are read out of the raw query this way. A store or category
        outside the grant is never named back to the user, because confirming that
        a specific store exists in another OpCo is itself the disclosure the scoped
        dictionary prevents. OpCo brands are public, so naming them costs nothing
        and the alternative -- answering "0 customers" for an OpCo the caller
        cannot see -- is actively misleading.

        Deterministic set membership over the unscoped OpCo roster: no pattern
        matching and no model call.
        """
        if getattr(principal, "is_group_user", False):
            return [], []

        granted_scope = set(getattr(principal, "opco_codes", ()) or ())
        normalized = normalize(query)
        if not normalized:
            return [], []

        padded = f" {normalized} "
        granted: list[str] = []
        foreign: list[str] = []

        # Longest label first, and consume the words it matched. "NorthCo Retail" and
        # "Retail" both occur in "how many NorthCo Retail customers"; the specific one
        # is what the user said, so it wins and the generic one does not also fire.
        for label in sorted(dictionary.all_opcos, key=len, reverse=True):
            codes = dictionary.all_opcos[label]
            if not label or f" {label} " not in padded:
                continue
            padded = padded.replace(f" {label} ", " " * (len(label) + 2))

            mine = [c for c in codes if c in granted_scope]
            theirs = [c for c in codes if c not in granted_scope]

            # A label covering several OpCos counts as GRANTED when any of them is:
            # "retail" means NorthCo and NorthCo Mart, and for an NorthCo caller that is
            # one concept partly inside the grant, which row-level security narrows
            # correctly. Reporting NorthCo Mart as out of scope would refuse a question
            # the caller may legitimately ask about their own half.
            if mine:
                granted.extend(c for c in mine if c not in granted)
                continue

            foreign.extend(c for c in theirs if c not in foreign)

        return granted, foreign

    # ------------------------------------------------------------------

    def _coerce_plan(self, previous: Optional[dict[str, Any]]) -> ResolutionPlan:
        if not previous:
            return ResolutionPlan()
        try:
            return ResolutionPlan.model_validate(previous)
        except Exception:
            logger.debug("previous resolution plan was unreadable; starting fresh")
            return ResolutionPlan()

    @staticmethod
    def _resolved_slot(
        *,
        slot_id: str,
        phrase: str,
        candidate,
    ) -> ResolvedSlot:
        return ResolvedSlot(
            slot_id=slot_id,
            phrase=phrase,
            entity_class=candidate.entity_class,
            target_column=candidate.target_column,
            status="resolved",
            canonical_value=candidate.canonical_value,
            display_value=candidate.display_value,
            category_key=candidate.category_key,
            opco_code=candidate.opco_code,
            alternate_columns=list(candidate.alternate_columns),
        )

    @staticmethod
    def _lookup_slot_limit() -> int:
        return max(1, int(settings.lookup_max_slots_per_question or 2))

    # Classes the per-question budget does not bound. An OpCo is a scope, not a
    # lookup value -- it comes from the permission block and, for a group user,
    # from naming the OpCo -- so it must not push a store out of the budget. It
    # used to: a spurious OpCo match plus one store filled the two slots, and a
    # one-store question was answered with "at most 2 OpCos or stores".
    #
    # An enum is not bounded either, for the same reason stated differently. The
    # budget exists to keep SQL predictable when a question combines DIMENSION
    # VALUES drawn from large catalogues -- 550 stores, 5,400 categories -- where
    # each filter is a resolution the user may have to be asked about. An enum is a
    # handful of known values on a column the view already has; `lifecycle_stage =
    # 'Active' AND membership_tier = 'Gold'` is two cheap equalities, not two
    # lookups. Counting them refused ordinary questions: "how many active NorthCo
    # Credit cardholders" was answered with "I can use at most 2 kinds of store or
    # product filters", having found no store and no product.
    _UNBUDGETED_CLASSES: frozenset[str] = frozenset({"opco", "enum"})

    @classmethod
    def _counts_against_budget(cls, entity_class: str) -> bool:
        """Whether a slot consumes the per-question lookup budget."""
        return entity_class not in cls._UNBUDGETED_CLASSES

    @classmethod
    def _budgeted_columns(cls, plan: ResolutionPlan) -> set[str]:
        """Distinct columns the plan filters, which is what the budget bounds.

        Counting VALUES was wrong. "How many customers are in Elite, Premium,
        Growth, Mass and At Risk" names five values of ONE column -- a single
        `value_segment IN (...)` predicate -- and was refused as five filters. What
        makes SQL unpredictable is the number of dimensions combined, not the length
        of an IN list, so the limit applies per column.
        """
        return {
            slot.target_column
            for slot in plan.slots
            if cls._counts_against_budget(slot.entity_class) and slot.target_column
        }

    @classmethod
    def _lookup_slot_count(cls, plan: ResolutionPlan) -> int:
        return len(cls._budgeted_columns(plan))

    def _append_lookup_slot(self, plan: ResolutionPlan, slot: ResolvedSlot) -> bool:
        if self._counts_against_budget(slot.entity_class):
            columns = self._budgeted_columns(plan)
            # Another value for a column already in the plan is free: it joins the
            # existing IN list rather than adding a dimension.
            if (
                slot.target_column not in columns
                and len(columns) >= self._lookup_slot_limit()
            ):
                return False
        plan.slots.append(slot)
        return True

    @staticmethod
    def _span_label(span: EntitySpan) -> str:
        best = span.best
        label = clean_text_value(getattr(best, "display_value", "")) if best else ""
        return label or f'"{span.text}"'

    def _too_many_lookup_values_result(
        self,
        plan: ResolutionPlan,
        spans: list[EntitySpan],
        overflow: list[tuple[str, str]],
    ) -> ResolutionResult:
        limit = self._lookup_slot_limit()
        counted = [
            (self._slot_label(slot), slot.entity_class)
            for slot in plan.slots
            if self._counts_against_budget(slot.entity_class)
        ] + overflow
        noun = self._budget_noun(entity_class for _, entity_class in counted)
        # Report the count that the limit is actually about.
        distinct = len(self._budgeted_columns(plan)) + len(
            {entity_class for _, entity_class in overflow}
        )
        plan.status = "pending"

        # Name what was found. The old message just restated the limit, which
        # reads as nonsense when the user believes they asked about one store.
        return ResolutionResult(
            plan=plan,
            spans=spans,
            needs_clarification=True,
            clarification_question=(
                f"I can use at most {limit} kinds of {noun} in one question, and "
                f"this one has {distinct}: "
                f"{', '.join(label for label, _ in counted)}. "
                f"Please ask again using no more than {limit}."
            ),
        )

    @classmethod
    def _budget_noun(cls, entity_classes: Iterable[str]) -> str:
        """"stores", "products", or "store or product filters" when both."""
        labels: list[str] = []
        for entity_class in entity_classes:
            label = cls._lookup_entity_plural_label(entity_class)
            if label not in labels:
                labels.append(label)

        if len(labels) == 1:
            return labels[0]
        return "store or product filters"

    @staticmethod
    def _slot_label(slot: ResolvedSlot) -> str:
        label = clean_text_value(slot.display_value) or clean_text_value(
            slot.canonical_value
        )
        if not label and slot.options:
            label = clean_text_value(slot.options[0].get("display_value"))
        return label or f'"{slot.phrase}"'

    @staticmethod
    def _lookup_entity_plural_label(entity_class: str) -> str:
        if entity_class == "store":
            return "stores"
        if entity_class.startswith("category_"):
            return "products"
        return "lookup values"

    @staticmethod
    def _option_with_internal_value(option: dict[str, Any], dictionary) -> dict[str, Any]:
        """Recover internal canonical keys from public clarification options."""
        if option.get("entity_class") != "store":
            return option

        display = clean_text_value(option.get("display_value"))
        if not display:
            return option

        candidates = dictionary.lookup_exact(normalize(display))
        for candidate in candidates:
            if candidate.entity_class != "store":
                continue
            if normalize(candidate.display_value) != normalize(display):
                continue
            out = dict(option)
            out["canonical_value"] = candidate.canonical_value
            out["target_column"] = candidate.target_column
            out["opco_code"] = candidate.opco_code
            out["opco_name"] = candidate.row_context.get("opco_name") or out.get("opco_name")
            out["category_key"] = candidate.category_key
            return out

        return option

    def _restore_internal_store_values(self, plan: ResolutionPlan, dictionary) -> None:
        for slot in plan.slots:
            if slot.entity_class != "store":
                continue
            fixed = self._option_with_internal_value(slot.model_dump(), dictionary)
            slot.canonical_value = fixed.get("canonical_value")
            slot.target_column = fixed.get("target_column") or slot.target_column
            slot.opco_code = fixed.get("opco_code") or slot.opco_code
            slot.category_key = fixed.get("category_key") or slot.category_key
            if slot.options:
                slot.options = [
                    self._option_with_internal_value(option, dictionary)
                    for option in slot.options
                ]

    def _apply_selection(self, plan: ResolutionPlan, selection: str, dictionary) -> bool:
        """Resolve the first ambiguous slot from a user's chosen option."""
        target = normalize(selection)
        if not target:
            return False

        for slot in self._ambiguous_in_ask_order(plan):
            for option in slot.options:
                display = normalize(option.get("display_value"))
                canonical = normalize(option.get("canonical_value"))
                if target in (display, canonical) or (display and target in display):
                    option = self._option_with_internal_value(option, dictionary)
                    self._adopt_option(slot, option)
                    return True

            # A bare ordinal ("2") picks from the presented list.
            if is_option_number(selection):
                number = target.removeprefix("option ").strip()
                idx = int(number) - 1
                if 0 <= idx < len(slot.options):
                    option = self._option_with_internal_value(slot.options[idx], dictionary)
                    self._adopt_option(slot, option)
                    return True
        return False

    @staticmethod
    def _display_opco_code(opco_code: str | None) -> str:
        code = clean_text_value(opco_code)
        if not code:
            return ""
        if code == "N360":
            return "N360"
        parts = code.split("_")
        return " ".join("NorthCo" if part == "NorthCo" else part.capitalize() for part in parts)

    def _format_clarification_option(self, option: dict[str, Any]) -> str:
        label = clean_text_value(option.get("display_value")) or clean_text_value(
            option.get("canonical_value")
        )
        opco = clean_text_value(option.get("opco_name")) or self._display_opco_code(
            option.get("opco_code")
        )
        if opco:
            return f"{label} (OpCo: {opco})"
        return label

    def _finalize(
        self,
        plan: ResolutionPlan,
        spans: list[EntitySpan],
        invalid_selection: str | None = None,
    ) -> ResolutionResult:
        # An out-of-scope OpCo or category used to short-circuit here, because
        # there was nothing to clarify and running the query would have returned a
        # misleading zero. No entity is out of scope now, so an unresolved phrase
        # falls through to ordinary clarification -- which is the right handling
        # for what it now always is: a name nobody recognises.
        self._apply_opco_pin(plan)

        impossible = [
            s for s in plan.slots if s.status == "ambiguous" and not s.options
        ]

        # A slot the pin emptied, of a class ALREADY resolved, was never a second
        # entity: it was another reading of the name that got resolved. Carrying it
        # produced "I could not find megamall within NORTHCO_MART" after the user picked
        # NORTHCO MART INGLEGATE JUNIPERFORD -- a refusal aimed at half of the name they had just
        # confirmed. Drop it and answer the question.
        resolved_classes = {_entity_family(s.entity_class) for s in plan.resolved}
        redundant = [
            s for s in impossible if _entity_family(s.entity_class) in resolved_classes
        ]
        if redundant:
            plan.slots = [s for s in plan.slots if s not in redundant]
            impossible = [s for s in impossible if s not in redundant]

        if impossible:
            # The OpCo pin removed every candidate: the entities the question names
            # cannot co-exist. Say so, rather than asking an empty question or
            # running a query that must return nothing.
            slot = impossible[0]
            pinned = ", ".join(sorted(self._pinned_opcos(plan))) or "the selected OpCo"
            noun = _CLARIFY_NOUN.get(slot.entity_class, "product")
            plan.status = "pending"
            return ResolutionResult(
                plan=plan,
                spans=spans,
                needs_clarification=True,
                clarification_question=(
                    f'I could not find the {noun} "{slot.phrase}" within {pinned}, '
                    "which is fixed by the store you selected. Entities from "
                    f"different OpCos cannot be combined. Please name a {noun} from "
                    f"{pinned}, or ask again with a different store."
                ),
            )

        ambiguous = self._ambiguous_in_ask_order(plan)

        if ambiguous:
            plan.status = "pending"
            slot = ambiguous[0]
            option_count = len(slot.options)
            options = "\n".join(
                f"{i}. {self._format_clarification_option(o)}"
                for i, o in enumerate(slot.options[: max_options_per_span()], start=1)
            )
            noun = _CLARIFY_NOUN.get(slot.entity_class, "product")
            if invalid_selection:
                if is_confirmation_text(invalid_selection):
                    question = (
                        "Please reply with the option number only, not yes/no. "
                        f"Which {noun} did you mean?\n{options}"
                    )
                else:
                    question = (
                        "I could not match your reply to one of the options. "
                        f"Please reply with the option number only.\n{options}"
                    )
            elif option_count == 1:
                question = (
                    f'I could not match "{slot.phrase}" exactly. '
                    f"Select the option number if this is the right {noun}.\n"
                    f"Reply with the option number only.\n{options}"
                )
            else:
                question = (
                    f'I found more than one {noun} matching "{slot.phrase}". '
                    f"Which one did you mean?\n"
                    f"Reply with the option number only.\n{options}"
                )
            return ResolutionResult(
                plan=plan,
                spans=spans,
                needs_clarification=True,
                clarification_question=question,
            )

        plan.status = "resolved"
        return ResolutionResult(plan=plan, spans=spans)


_resolver: Optional[EntityResolver] = None


def get_entity_resolver() -> EntityResolver:
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver()
    return _resolver
