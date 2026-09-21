# app/graph/copilot_state.py
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict


class CopilotState(TypedDict, total=False):
    # Resolved caller scope (app.service.principal_service.PrincipalContext).
    # Present for every request that runs SQL. Nodes read it to scope the
    # glossary, the entity dictionary, and table routing, and to explain an
    # out-of-scope refusal in the caller's own terms.
    principal: Any

    query: str
    original_query: str
    clarification_followup: bool
    clarification_followup_reason: Optional[str]

    inherits_previous_filters: bool
    # The resolution plan carries resolved entities forward, so entity
    # inheritance is one boolean rather than the v2 per-scope list.
    inherits_entity_filters: bool
    inherits_time_period: bool
    contextual_rewrite: Optional[str]
    contextual_followup_reason: Optional[str]

    # A short reply answering a pending clarification, matched by the resolver
    # against the options it presented last turn.
    clarification_selected_option: Optional[str]
    thread_id: str
    request_id: str

    history: List[Dict[str, Any]]
    glossary_hits: List[Dict[str, Any]]
    glossary_context: str
    schema_match_confidence: Literal["high", "medium", "low", "none"]

    intent: Literal["analytics", "glossary", "smalltalk", "clarify"]
    route: Literal["answer_from_previous", "plan_sql", "glossary", "smalltalk", "clarify"]
    intent_reason: str
    is_followup: bool
    resolved_term: Optional[str]
    # Phrases the router read as naming a store, product or attribute value. None
    # when it had no opinion; [] when it found none. See EntityResolver.resolve.
    entity_phrases: Optional[list[str]]

    previous_assistant_payload: Optional[Dict[str, Any]]


    # Entity resolution output. lookup_plan is a
    # app.service.entity_resolution.ResolutionPlan dict; it is the single carrier
    # of cross-turn entity state.
    lookup_matches: List[Dict[str, Any]]
    lookup_context: str
    lookup_plan: Optional[Dict[str, Any]]
    lookup_needs_clarification: bool
    lookup_clarification_question: Optional[str]

    # OpCos outside the caller's grant that the question names alongside a granted
    # one -- the customer-overlap exception. Allowed through resolution, then
    # constrained: sql_validator_agent permits these codes only inside an
    # `opco_codes` array predicate, never as an `opco_code = '...'` filter.

    sql: Optional[str]
    plan_status: Literal["answerable", "needs_clarification", "unsupported"]
    plan_reason: Optional[str]
    needs_clarification: bool
    clarification_question: Optional[str]
    unsupported_reason: Optional[str]
    # Why the planner refused: "customer_identity" (a privacy rule, for anyone) or
    # "not_answerable" (the schema, for anyone). Only the first produces a policy
    # notice. See SqlPlan.unsupported_cause.
    #
    # There was a third, "out_of_scope", meaning the caller's own grant. It went
    # with the grants: no caller is scoped out of an OpCo or a category.
    unsupported_cause: Optional[str]
    missing_slots: List[str]

    validation_error: Optional[str]
    execution_error: Optional[str]
    retry_count: int
    sql_attempts: List[str]

    rows_payload: Dict[str, Any]
    analysis_result: Dict[str, Any]

    # Set when the request was refused for an authorization or privacy reason
    # rather than a data reason: a money metric for an EXEC caller, or a question
    # asking to identify individuals. Distinct from `error`, which means something
    # broke.
    out_of_scope_reason: Optional[str]
    # Which policy produced that refusal, as an AppliedPolicy code. Carried
    # alongside the prose rather than parsed back out of it, so the wording stays
    # free to change without breaking the machine-readable half.
    out_of_scope_code: Optional[str]

    # Policies that shaped this answer, as serialised AppliedPolicy dicts. Appended
    # to as the turn proceeds and copied onto the result by whichever terminal
    # node finishes the turn. See app/service/access_policy.py.
    applied_policies: List[Dict[str, Any]]

    result: Dict[str, Any]
    error: Optional[str]