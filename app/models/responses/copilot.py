from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class GlossaryMatch(BaseModel):
    field_name: str
    description: str
    data_type: Optional[str] = None
    sample_values: List[str] = Field(default_factory=list)


class ChartDatum(BaseModel):
    label: str
    value: float
    # Share of the total across all points, 0-100. Always populated for
    # bar/donut charts so the frontend can size slices consistently even when
    # `value` is an absolute count.
    percentage: Optional[float] = None


class ChartSeries(BaseModel):
    id: str
    label: str


class ChartSpec(BaseModel):
    chart_type: Literal["text", "bar_chart", "line_chart", "donut_chart", "table"] = "text"
    title: Optional[str] = None

    x_field: Optional[str] = None
    x_axis: Optional[str] = None
    y_axis: Optional[str] = None
    label_field: Optional[str] = None
    value_field: Optional[str] = None
    # How the frontend should format `value`. "percentage" means value is
    # already a 0-100 share; "count"/"currency"/"number" mean it is an absolute
    # measure and `percentage` on each calderhollow holds the share.
    value_format: Literal["count", "percentage", "currency", "number"] = "number"
    # ISO 4217 code, set only when value_format is "currency". The frontend formats
    # with its own locale rules; it must not have to parse the symbol out of prose.
    currency: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    series: List[ChartSeries] = Field(default_factory=list)
    data: List[Any] = Field(default_factory=list)


class PolicyNotice(BaseModel):
    """One authorization policy that shaped this answer.

    Emitted whichever way the policy went, not only on a refusal -- see
    app/service/access_policy.py for why all three effects are reported.

    Branch on `effect`. `code` is stable and safe to key on; `message` is written
    for a person and may be reworded. `scope` describes only what the caller WAS
    granted.

    Three codes were removed and a client branching on them can delete those arms.
    "opco_out_of_scope" and "category_out_of_scope" went with row-level security:
    every caller sees every OpCo and every category. "small_cell_suppressed" went
    with small-cell suppression, which used to blank counts of fewer than
    CI_MIN_CELL_SIZE customers; nothing blanks a count now, so the "suppressed"
    effect is gone with it.
    """

    code: Literal[
        "customer_identity",
        "role_money_withheld",
    ]
    effect: Literal["denied", "substituted"]
    # The mechanism that enforced it: "persona_view.ci_exec" or
    # "schema.no_customer_key".
    enforced_by: str = ""
    message: str
    # The caller's own grant. role_level is the whole of it now.
    scope: Dict[str, Any] = Field(default_factory=dict)
    # metric_definition keys: what was asked for, and what was used instead.
    # Set only when `effect` is "substituted".
    requested_metric: Optional[str] = None
    substituted_with: Optional[str] = None


class AnalysisDetail(BaseModel):
    """The structured reading of a result, alongside the prose answer.

    Produced by app/agents/analysis_agent.py, which either gets it from the model
    as JSON or derives it deterministically when the model is unavailable -- so a
    client can rely on the shape being present, not on it being model-written.

    `natural_answer` is the raw sentence the analysis produced; `answer` on the
    response is the composed version a client should render. Chart fields are the
    analysis's own recommendation, already applied to `chart`; they are echoed here
    because a client that builds its own visualisation needs the field names.
    """

    model_config = ConfigDict(extra="allow")

    natural_answer: str = ""
    finding: str = ""
    calculation_logic: List[str] = Field(default_factory=list)
    interpretation: List[str] = Field(default_factory=list)
    next_step: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)

    chart_type: Optional[str] = None
    chart_title: Optional[str] = None
    x_field: Optional[str] = None
    x_axis: Optional[str] = None
    y_axis: Optional[str] = None
    label_field: Optional[str] = None
    value_field: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    series: List[Dict[str, str]] = Field(default_factory=list)
    chart_data: List[Dict[str, Any]] = Field(default_factory=list)


class CopilotResponse(BaseModel):
    type: Literal[
        "analytics",
        "glossary",
        "clarify",
        "out_of_scope",
        "unsupported",
        "error",
        "smalltalk",
    ]
    answer: str
    chart: Optional[ChartSpec] = None
    sql: Optional[str] = None
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    glossary_matches: List[GlossaryMatch] = Field(default_factory=list)
    intent_reason: Optional[str] = None
    thread_id: Optional[str] = None
    request_id: Optional[str] = None

    # Every policy that changed what this response contains. Empty on an ordinary
    # answer. A client that renders rows into a table should check this before
    # concluding that a null or a missing measure means "no data".
    policies: List[PolicyNotice] = Field(default_factory=list)

    # The refusal sentence, on `out_of_scope` responses only. Predates `policies`
    # and is already on the wire; kept for compatibility. Prefer `policies`, which
    # is machine-readable and also covers substitutions and suppressions.
    denied_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Analytics extras.
    #
    # These four are on the wire and were not declared, which made this model a
    # description of a smaller response than the service actually sends -- and
    # since /ask returns a JSONResponse, nothing would have caught the drift.
    # ------------------------------------------------------------------

    # The structured reading behind `answer`. Present on analytics responses.
    analysis: Optional[AnalysisDetail] = None

    # What the entity resolver did with the names in the question. Exposed so a
    # caller can tell a resolved filter from a planner guess -- store IDs are
    # rewritten to branch labels first; see public_lookup_slots.
    lookup_matches: List[Dict[str, Any]] = Field(default_factory=list)
    # The same thing rendered as the prose line shown under an answer.
    lookup_context: str = ""
    # The full resolution plan, when there was one: slots, their status and the
    # options offered for anything ambiguous.
    lookup_plan: Optional[Dict[str, Any]] = None

    # Which branch of the graph wrote this answer. Set on refusals; a debugging aid
    # rather than something a client should branch on -- `type` and `policies` are.
    answer_source: Optional[str] = None
