# app/graph/nodes/copilot_nodes.py
from __future__ import annotations

from app.graph.nodes.base import CopilotNodeDependencies
from app.graph.nodes.context_nodes import ContextNodesMixin
from app.graph.nodes.intent_nodes import IntentNodesMixin
from app.graph.nodes.response_nodes import ResponseNodesMixin
from app.graph.nodes.lookup_nodes import LookupNodesMixin
from app.graph.nodes.planning_nodes import PlanningNodesMixin
from app.graph.nodes.execution_nodes import ExecutionNodesMixin


class CopilotGraphNodes(
    ContextNodesMixin,
    IntentNodesMixin,
    ResponseNodesMixin,
    LookupNodesMixin,
    PlanningNodesMixin,
    ExecutionNodesMixin,
    CopilotNodeDependencies,
):
    """LangGraph node collection for the Customer Intelligence Copilot.

    The graph wiring stays in app.graph.copilot_graph. Node behavior is split by
    responsibility so lookup, planning, response, and execution logic can be
    maintained independently.
    """

    pass
