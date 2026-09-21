#!/usr/bin/env python
"""End-to-end check of the persona question set.

    python scripts/eval_persona_questions.py
    python scripts/eval_persona_questions.py --case Q9 --verbose
    python scripts/eval_persona_questions.py --json results.json

These are the questions the copilot exists to answer, one per persona, and each
one is here because it once could not be answered. Q2 needed customer x category
revenue and a store on the bridge; Q5 needed a financing count; Q6 and Q9 needed
credit card holding; Q8 needed brand on the customer bridge; Q10 needed a
standardized region. So this is a regression suite for the schema as much as for
the copilot.

It drives the HTTP API rather than the graph, because the failures being guarded
against were only ever visible end to end: an entity resolving to the wrong
column, a clarification loop that could not terminate, a filter on a column the
view does not expose.

Assertions are deliberately structural, not numeric. The answer text comes from a
model and will not be stable, but WHICH COLUMNS the SQL touches is exactly what
these fixes were about, and a wrong column is the failure that reads as a
plausible number. `sql_must_include` is therefore the real assertion;
`expect_rows` only distinguishes "answered" from "answered with nothing".

Clarifications are answered automatically -- option 1 unless the case names its
own replies -- so a question that starts asking and never stops shows up as a
failure rather than as a hang.

Requires the stack up (docker compose up -d) and seeded (docker compose --profile
seed run --rm mock-data).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

import httpx

URL = "http://localhost:8080/v1/copilot/ask"
USER = "eval-persona"
PERMISSION = {
    "principal_id": USER,
    "opco_codes": ["N360"],
    "role_level": "HOD",
    "category_keys": None,
}

# N360 is the group entity, so the caller sees every OpCo. The period is inside
# the seeded window (2026-01-01 .. 2026-06-30); "YTD" and "last quarter" are left
# in where the persona would say them, because resolving a relative period is part
# of what is being tested.
CASES: list[dict[str, Any]] = [
    {
        "id": "Q1",
        "persona": "Group Executive / Ecosystem VP",
        "question": (
            "What percentage of our active retail members shopped at both NorthCo "
            "and NorthCo Mart, and are also NorthCo Bank members, between January and "
            "June 2026?"
        ),
        "expect": "analytics",
        "sql_must_include": ["opco_codes"],
        "expect_rows": True,
        "why": "Cross-OpCo overlap is only expressible on the group view's array.",
    },
    {
        "id": "Q2",
        "persona": "Category Manager (Fashion, NorthCo)",
        "question": (
            "What is the member penetration and average basket size for Gen Z vs "
            "Millennial customers in the Fashion category at INGLEGATE JUNIPERFORD, January "
            "to June 2026?"
        ),
        # 2 = INGLEGATE JUNIPERFORD (NorthCo), then 1 = HARD > HOME FASHION.
        "replies": ["2", "1"],
        "expect": "analytics",
        # SQL is withheld from the payload whenever it names an internal store id,
        # so a store-filtered case is asserted on what the resolver produced.
        "lookup_must_include": ["INGLEGATE JUNIPERFORD", "generation_bucket"],
        "expect_rows": True,
        "why": "Needed store_id and gross_sales_amount on the customer bridge.",
    },
    {
        "id": "Q3",
        "persona": "NorthCo Bank HOD Marketing",
        "question": (
            "How does monthly retail spend at NorthCo and NorthCo Mart compare between "
            "customers who are active NorthCo Bank members and those who are not, in "
            "June 2026?"
        ),
        "expect": "analytics",
        "sql_must_include": ["NORTHCO_BANK"],
        "expect_rows": True,
        "why": "Bank relationship is OpCo membership; no extra column needed.",
    },
    {
        "id": "Q4",
        "persona": "Operation HOD",
        "question": (
            "What is the member penetration rate and grocery revenue for NorthCo Mart "
            "VELDRA SELBYCROSS compared with GLEDEHOLT VANTRYTON in June 2026?"
        ),
        "expect": "analytics",
        "lookup_must_include": ["VELDRA SELBYCROSS"],
        "expect_rows": True,
        "why": "Answerable before any of this work; guards against a regression.",
    },
    {
        "id": "Q5",
        "persona": "ACSM Marketing HOD",
        "question": (
            "What is the average NorthCo Mart appliance spend of customers who use "
            "NorthCo Credit financing versus those who do not, in June 2026?"
        ),
        "expect": "analytics",
        "sql_must_include": ["active_loan_count"],
        "expect_rows": True,
        "why": "Financing usage had no column at all before active_loan_count.",
    },
    {
        "id": "Q6",
        "persona": "NORTHCO360 Marketing",
        "question": (
            "How many active NorthCo Credit cardholders moved into the Declining "
            "lifecycle stage at Metro Valley stores in June 2026?"
        ),
        "expect": "analytics",
        "sql_must_include": ["active_credit_card_count", "store_location"],
        "expect_rows": False,
        "why": (
            "Cardholding plus a standardized region. Rows may legitimately be zero: "
            "NorthCo Credit branches carry no Metro Valley label in the seed."
        ),
    },
    {
        "id": "Q8",
        "persona": "Category Buyer (repeat purchase)",
        "question": (
            "Which product categories saw the highest repeat purchase rate among "
            "active Gen Y female members between April and June 2026?"
        ),
        "expect": "analytics",
        "sql_must_include": ["customer_key"],
        "expect_rows": True,
        "why": (
            "Guards the per-customer-then-aggregate shape: the prompts used to "
            "forbid grouping by customer_key even in a CTE, so this was answered "
            "'not supported by the available data' for a figure the data and the "
            "validator both support. Brand was the original subject and is gone -- "
            "no table pairs a customer with a brand any more, by design."
        ),
    },
    {
        "id": "Q9",
        "persona": "NORTHCO360 Marketing HOD",
        "question": (
            "How many 3 Star members upgraded to 4 Star in June 2026, and how many "
            "of them hold an active NorthCo Credit Card?"
        ),
        "expect": "analytics",
        "sql_must_include": ["membership_tier", "active_credit_card_count"],
        "expect_rows": True,
        "why": (
            "Tier migration used to read previous_membership_tier. That column is "
            "gone, so this is now the case that proves the planner "
            "can reach last month's state the only way left: a self join of "
            "v_customer_opco_monthly on customer_key with the two month_start_date "
            "values, June for the 4 Star side and May for the 3 Star side. It is a "
            "harder question than it was and a more useful one to keep -- if the "
            "planner cannot write that join, no movement question is answerable. "
            "The card side still needs the group mirror so no cross-OpCo self join "
            "is required on top."
        ),
    },
    {
        "id": "Q10",
        "persona": "NORTHCO360 HOD",
        "question": (
            "What is the combined revenue and active member count across NorthCo, "
            "NorthCo Mart and NorthCo Credit for Southern stores in June 2026?"
        ),
        "expect": "analytics",
        "sql_must_include": ["store_location"],
        "expect_rows": True,
        "why": "Regional questions need the standardized store_location.",
    },
]

MAX_TURNS = 4


def ask(client: httpx.Client, query: str, thread_id: str) -> dict[str, Any]:
    response = client.post(
        URL,
        headers={"Content-Type": "application/json", "x-user-id": USER},
        json={"query": query, "thread_id": thread_id, "permission": PERMISSION},
    )
    response.raise_for_status()
    return response.json()


def run_case(client: httpx.Client, case: dict[str, Any], verbose: bool) -> dict[str, Any]:
    thread_id = f"eval-{case['id']}-{abs(hash(case['question'])) % 10**8}"
    replies = list(case.get("replies") or [])
    turns: list[str] = []

    payload = ask(client, case["question"], thread_id)
    turns.append(payload.get("type", "?"))

    # Answer clarifications until the copilot commits to something. A question that
    # keeps asking is a failure, not a pass with a caveat.
    while payload.get("type") == "clarify" and len(turns) < MAX_TURNS:
        reply = replies.pop(0) if replies else "1"
        if verbose:
            print(f"    clarify: {(payload.get('answer') or '')[:110]}")
            print(f"    -> replying {reply!r}")
        payload = ask(client, reply, thread_id)
        turns.append(payload.get("type", "?"))

    sql = " ".join((payload.get("sql") or "").split())
    rows = payload.get("rows") or []
    lookup = json.dumps(payload.get("lookup_matches") or [])

    problems: list[str] = []
    if payload.get("type") != case["expect"]:
        problems.append(f"type={payload.get('type')} expected {case['expect']}")

    # SQL is redacted whenever it names an internal store id, so a store case is
    # asserted on the resolver output instead. Treating the redaction as a failure
    # would train everyone to ignore this suite.
    wanted_sql = case.get("sql_must_include", [])
    if wanted_sql and not sql:
        problems.append("sql not returned")
    elif wanted_sql:
        missing = [c for c in wanted_sql if c.lower() not in sql.lower()]
        if missing:
            problems.append("sql missing " + ", ".join(missing))

    missing_lookup = [
        c for c in case.get("lookup_must_include", []) if c.lower() not in lookup.lower()
    ]
    if missing_lookup:
        problems.append("lookup missing " + ", ".join(missing_lookup))

    if case.get("expect_rows") and not rows:
        problems.append("no rows")

    return {
        "id": case["id"],
        "persona": case["persona"],
        "turns": turns,
        "type": payload.get("type"),
        "rows": len(rows),
        "sql": sql,
        "answer": (payload.get("answer") or "").replace("\n", " ")[:300],
        "problems": problems,
        "ok": not problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="run one case by id, e.g. Q9")
    parser.add_argument("--json", dest="out", help="write full results here")
    parser.add_argument("--verbose", action="store_true", help="show clarification turns")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.case or c["id"] == args.case.upper()]
    if not cases:
        print(f"no case named {args.case}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=300.0) as client:
        for case in cases:
            print(f"{case['id']}  {case['persona']}")
            print(f"    {case['question'][:150]}")
            try:
                outcome = run_case(client, case, args.verbose)
            except Exception as exc:  # noqa: BLE001 - the harness reports, never crashes
                outcome = {
                    "id": case["id"],
                    "persona": case["persona"],
                    "turns": [],
                    "type": "EXCEPTION",
                    "rows": 0,
                    "sql": "",
                    "answer": f"{type(exc).__name__}: {exc}",
                    "problems": [f"{type(exc).__name__}: {exc}"],
                    "ok": False,
                }
            results.append(outcome)
            mark = "PASS" if outcome["ok"] else "FAIL"
            print(f"    {mark}  turns={'>'.join(outcome['turns'])}  rows={outcome['rows']}")
            if outcome["problems"]:
                print(f"          {'; '.join(outcome['problems'])}")
            if args.verbose:
                print(f"          {outcome['answer']}")
                print(f"          SQL: {outcome['sql'][:400]}")
            print()

    passed = sum(1 for r in results if r["ok"])
    print(f"{passed}/{len(results)} passed")
    for r in results:
        if not r["ok"]:
            print(f"  {r['id']}: {'; '.join(r['problems'])}")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nwrote {args.out}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
