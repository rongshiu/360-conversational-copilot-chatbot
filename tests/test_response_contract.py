"""CopilotResponse has to describe what /ask actually sends.

The route returns a JSONResponse, so FastAPI never validates the payload against
the model -- the model is documentation, and documentation that drifts is worse
than none. Four fields were on the wire and undeclared before this test existed.
"""
from __future__ import annotations

import unittest

from app.agents.answer_agent import (
    build_clarify_response,
    build_error_response,
    build_glossary_response,
    build_unsupported_response,
)
from app.models.responses.copilot import CopilotResponse

# Keys attached to a result after the builders return, by the graph nodes and by
# CustomerIntelligenceCopilot.run. Listed here because they are set with
# result["..."] = rather than in one dict literal a test could read back.
LATE_BOUND_KEYS = {
    "analysis",
    "lookup_matches",
    "lookup_context",
    "lookup_plan",
    "policies",
    "denied_reason",
    "answer_source",
    "thread_id",
    "request_id",
}


class ResponseContractTests(unittest.TestCase):
    def test_every_builder_key_is_declared(self) -> None:
        payloads = [
            build_error_response(answer="failed"),
            build_clarify_response("which store?"),
            build_unsupported_response("not supported"),
            build_glossary_response([]),
        ]

        declared = set(CopilotResponse.model_fields)
        for payload in payloads:
            with self.subTest(response_type=payload["type"]):
                self.assertEqual(set(), set(payload) - declared)

    def test_late_bound_keys_are_declared(self) -> None:
        self.assertEqual(set(), LATE_BOUND_KEYS - set(CopilotResponse.model_fields))

    def test_an_analytics_payload_validates(self) -> None:
        """The shape the analytics path actually produces, extras included."""
        payload = {
            "type": "analytics",
            "answer": "Sales rose 4% in July.",
            "chart": {
                "chart_type": "bar_chart",
                "title": "Sales by store",
                "value_format": "currency",
                "currency": "MYR",
                "data": [{"label": "Inglegate Juniperford", "value": 1200.0, "percentage": 60.0}],
            },
            "rows": [{"store": "Inglegate Juniperford", "gross_sales_amount": 1200.0}],
            "sql": "SELECT 1",
            "glossary_matches": [],
            "intent_reason": "analytics question",
            "analysis": {
                "finding": "Inglegate Juniperford leads",
                "calculation_logic": ["summed gross_sales_amount by store"],
                "limitations": [],
            },
            "lookup_matches": [{"phrase": "inglegate juniperford", "status": "resolved"}],
            "lookup_context": '- "inglegate juniperford" resolves to store "Inglegate Juniperford"',
            "lookup_plan": {"slots": []},
            "policies": [],
            "thread_id": "t-1",
            "request_id": "r-1",
        }

        parsed = CopilotResponse.model_validate(payload)

        self.assertEqual("Inglegate Juniperford leads", parsed.analysis.finding)
        self.assertEqual("MYR", parsed.chart.currency)
        self.assertEqual(1, len(parsed.lookup_matches))


if __name__ == "__main__":
    unittest.main()
