"""Token / route measurement harness for the Customer Intelligence Copilot.

Runs a fixed golden set of questions through CustomerIntelligenceCopilot.run()
and prints, per query, the routed response type and the LLM token usage. Use it
to baseline before a change and re-run after, to confirm:

  * routes are unchanged (same result["type"] per query),
  * clarify cases still clarify,
  * total tokens and LLM call count drop.

Requires a reachable Postgres (main + checkpoint DBs) and a configured LLM
provider (Vertex/Gemini or OpenAI), exactly like the running service.

Usage:
    uv run python scripts/eval_copilot.py
    uv run python scripts/eval_copilot.py --json baseline.json   # save raw results
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Optional

from app.db.postgres import AsyncSessionLocal
from app.service import copilot_usage_service as usage
from app.service.copilot_service import CustomerIntelligenceCopilot


USER_ID = "eval-harness-user"

# Each item shares a thread_id with the item that must see its history (e.g. a
# follow-up reuses the analytics turn's thread). expect is a soft check only.
GOLDEN: list[dict[str, Any]] = [
    {
        "label": "analytics_with_period",
        "query": "How many total customers were active in March 2026?",
        "thread_id": "eval-analytics-1",
        "expect": "analytics",
    },
    {
        "label": "analytics_missing_period",
        "query": "What is the total revenue?",
        "thread_id": "eval-analytics-2",
        "expect": "clarify",
    },
    {
        "label": "analytics_lookup",
        "query": "Show me the top product categories by revenue for NorthCo Mart in Q1 2026.",
        "thread_id": "eval-analytics-3",
        "expect": "analytics",
    },
    {
        "label": "glossary",
        "query": "What does the lifecycle_stage column mean?",
        "thread_id": "eval-glossary-1",
        "expect": "glossary",
    },
    {
        "label": "smalltalk",
        "query": "Hi, what can this copilot do?",
        "thread_id": "eval-smalltalk-1",
        "expect": "smalltalk",
    },
    # Follow-up: first ask an analytics question, then react to it on the SAME thread.
    {
        "label": "followup_seed",
        "query": "Who are the top 10 customers by total revenue in March 2026?",
        "thread_id": "eval-followup-1",
        "expect": "analytics",
    },
    {
        "label": "followup_reaction",
        "query": "Why do you say so?",
        "thread_id": "eval-followup-1",
        "expect": "analytics",
    },
]


def _install_usage_capture() -> list[dict[str, Any]]:
    """Snapshot each request's usage tracker just before it is cleared.

    run() clears the tracker in a finally block, so we wrap clear_usage_tracking
    to grab the totals first. Non-invasive: production code is untouched.
    """
    captured: list[dict[str, Any]] = []
    original_clear = usage.clear_usage_tracking

    def patched_clear() -> None:
        tracker = usage.get_usage_tracker()
        if tracker is not None:
            captured.append(
                {
                    "request_id": tracker.request_id,
                    "status": tracker.status,
                    "llm_call_count": tracker.llm_call_count,
                    "sql_call_count": tracker.sql_call_count,
                    "input_tokens": tracker.input_tokens,
                    "output_tokens": tracker.output_tokens,
                    "total_tokens": tracker.total_tokens,
                    "spans": [s.get("name") for s in tracker.spans if s.get("type") == "llm"],
                }
            )
        original_clear()

    usage.clear_usage_tracking = patched_clear  # type: ignore[assignment]
    return captured


async def _run_one(item: dict[str, Any]) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        copilot = CustomerIntelligenceCopilot(db)
        try:
            result = await copilot.run(
                item["query"],
                item.get("thread_id"),
                user_id=USER_ID,
            )
            return {"type": result.get("type"), "answer": (result.get("answer") or "")[:160]}
        except Exception as exc:  # noqa: BLE001 - harness reports, never crashes
            return {"type": "EXCEPTION", "answer": f"{type(exc).__name__}: {exc}"}


async def main(out_json: Optional[str]) -> None:
    captured = _install_usage_capture()
    rows: list[dict[str, Any]] = []

    for item in GOLDEN:
        outcome = await _run_one(item)
        snapshot = captured[-1] if captured else {}
        ok = "ok" if outcome["type"] == item.get("expect") else "DIFF"
        rows.append(
            {
                "label": item["label"],
                "expect": item.get("expect"),
                "got": outcome["type"],
                "match": ok,
                "llm_calls": snapshot.get("llm_call_count"),
                "total_tokens": snapshot.get("total_tokens"),
                "input_tokens": snapshot.get("input_tokens"),
                "output_tokens": snapshot.get("output_tokens"),
                "llm_spans": snapshot.get("spans"),
                "answer": outcome["answer"],
            }
        )

    header = f"{'label':<22}{'expect':<11}{'got':<11}{'match':<6}{'calls':<6}{'tokens':<9}{'in':<8}{'out':<7}"
    print(header)
    print("-" * len(header))
    tot_calls = tot_tokens = tot_in = tot_out = 0
    for r in rows:
        print(
            f"{r['label']:<22}{str(r['expect']):<11}{str(r['got']):<11}{r['match']:<6}"
            f"{str(r['llm_calls']):<6}{str(r['total_tokens']):<9}{str(r['input_tokens']):<8}{str(r['output_tokens']):<7}"
        )
        tot_calls += r["llm_calls"] or 0
        tot_tokens += r["total_tokens"] or 0
        tot_in += r["input_tokens"] or 0
        tot_out += r["output_tokens"] or 0

    print("-" * len(header))
    print(f"{'TOTAL':<22}{'':<11}{'':<11}{'':<6}{tot_calls:<6}{tot_tokens:<9}{tot_in:<8}{tot_out:<7}")

    diffs = [r["label"] for r in rows if r["match"] == "DIFF"]
    if diffs:
        print(f"\nROUTE MISMATCHES (review): {', '.join(diffs)}")

    if out_json:
        with open(out_json, "w") as fh:
            json.dump(rows, fh, indent=2)
        print(f"\nWrote raw results to {out_json}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_path", default=None, help="write raw results to this path")
    args = parser.parse_args()
    asyncio.run(main(args.json_path))
