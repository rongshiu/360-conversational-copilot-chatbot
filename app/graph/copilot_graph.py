# app/graph/copilot_graph.py
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from app.graph.copilot_state import CopilotState
from app.graph.nodes import CopilotGraphNodes
from app.core import settings
from app.service.glossary_service import get_glossary_service
from app.service.entity_resolution import get_entity_resolver


def build_copilot_graph(
    db: AsyncSession,
    user_id: str,
    principal: Any = None,
    checkpointer: Any = None,
):
    nodes = CopilotGraphNodes(
        db=db,
        user_id=user_id,
        principal=principal,
        settings=settings,
        glossary_service=get_glossary_service(),
        entity_resolver=get_entity_resolver(),
    )

    graph = StateGraph(CopilotState)

    graph.add_node("reset_turn_state", nodes.reset_turn_state)
    graph.add_node("load_context", nodes.load_context)
    graph.add_node("classify_intent", nodes.classify_intent)
    graph.add_node("answer_from_previous", nodes.answer_from_previous_node)
    graph.add_node("glossary_answer", nodes.glossary_answer)
    graph.add_node("smalltalk_answer", nodes.smalltalk_answer)
    graph.add_node("resolve_lookup_entities", nodes.resolve_lookup_entities)
    graph.add_node("clarify_from_lookup", nodes.clarify_from_lookup)
    graph.add_node("out_of_scope_answer", nodes.out_of_scope_answer)
    graph.add_node("make_plan", nodes.make_plan)
    graph.add_node("unsupported_from_plan", nodes.unsupported_from_plan)
    graph.add_node("clarify_from_plan", nodes.clarify_from_plan)
    graph.add_node("validate_sql", nodes.validate_sql_node)
    graph.add_node("execute_sql", nodes.execute_sql_node)
    graph.add_node("analyze_result", nodes.analyze_result_node)
    graph.add_node("retry_sql", nodes.retry_sql)
    graph.add_node("execute_error", nodes.execute_error)
    graph.add_node("synthesize", nodes.synthesize)
    graph.add_node("save_turn", nodes.save_turn)

    graph.add_edge(START, "reset_turn_state")
    graph.add_edge("reset_turn_state", "load_context")
    graph.add_edge("load_context", "classify_intent")

    # `clarify` goes to the planner, not to a terminal clarification.
    #
    # The router has no schema context, so it cannot tell a question that is
    # under-specified from one that is unanswerable. It used to terminate at a
    # generic_clarify node whose agent was forced by its response schema to return
    # a question, which made an `unsupported` verdict unreachable on that path --
    # so the verdict depended on whether history happened to exist. The same
    # two-metric question ("YoY revenue growth AND member overlap") answered
    # `unsupported` on a fresh thread, where the planner decided, and `clarify` on
    # a follow-up, where the previous refusal sitting in history pushed the router
    # into clarify and short-circuited the planner.
    #
    # Routing here means the only component that reads the schema always makes the
    # clarify-vs-unsupported call. Nothing is lost: clarify_from_plan runs the same
    # clarification agent with strictly more context, so a genuinely
    # under-specified question still comes back as a clarification.
    graph.add_conditional_edges(
        "classify_intent",
        nodes.route_after_intent,
        {
            "answer_from_previous": "answer_from_previous",
            "glossary": "glossary_answer",
            "resolve_lookup": "resolve_lookup_entities",
            "smalltalk": "smalltalk_answer",
            "clarify": "resolve_lookup_entities",
        },
    )

    graph.add_conditional_edges(
        "answer_from_previous",
        nodes.route_after_answer_from_previous,
        {
            "resolve_lookup": "resolve_lookup_entities",
            "end_answer_from_previous": "save_turn",
        },
    )

    graph.add_conditional_edges(
        "resolve_lookup_entities",
        nodes.route_after_lookup_resolution,
        {
            "clarify_from_lookup": "clarify_from_lookup",
            "out_of_scope": "out_of_scope_answer",
            "make_plan": "make_plan",
        },
    )

    graph.add_conditional_edges(
        "make_plan",
        nodes.route_after_plan,
        {
            "unsupported_from_plan": "unsupported_from_plan",
            "clarify_from_plan": "clarify_from_plan",
            "out_of_scope": "out_of_scope_answer",
            "validate_sql": "validate_sql",
        },
    )

    graph.add_conditional_edges(
        "validate_sql",
        nodes.route_after_validate,
        {
            "execute_sql": "execute_sql",
            "retry_sql": "retry_sql",
            "out_of_scope": "out_of_scope_answer",
            "execute_error": "execute_error",
        },
    )

    graph.add_edge("retry_sql", "make_plan")

    graph.add_conditional_edges(
        "execute_sql",
        nodes.route_after_execute,
        {
            "retry_sql": "retry_sql",
            "execute_error": "execute_error",
            "analyze_result": "analyze_result",
        },
    )

    graph.add_edge("analyze_result", "synthesize")

    graph.add_edge("glossary_answer", "save_turn")
    graph.add_edge("smalltalk_answer", "save_turn")
    graph.add_edge("clarify_from_lookup", "save_turn")
    graph.add_edge("out_of_scope_answer", "save_turn")
    graph.add_edge("unsupported_from_plan", "save_turn")
    graph.add_edge("clarify_from_plan", "save_turn")
    graph.add_edge("execute_error", "save_turn")
    graph.add_edge("synthesize", "save_turn")

    graph.add_edge("save_turn", END)

    return graph.compile(checkpointer=checkpointer)
