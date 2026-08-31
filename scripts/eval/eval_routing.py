from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.orchestrator import DEFAULT_ORCHESTRATOR
from app.core.config import BASE_DIR
from scripts.eval.common import (
    NA,
    load_jsonl,
    precision_recall_f1_by_label,
    print_json_report,
    rate,
    result_to_dict,
    save_report,
)


EVAL_PATH = BASE_DIR / "data" / "eval" / "routing_eval.jsonl"
SIDE_EFFECTS = ["[READ ONLY]"]


def check_route(actual_route: dict, expected_route: dict) -> tuple[bool, list[str]]:
    errors = []
    for key, expected in expected_route.items():
        actual = actual_route.get(key)
        if actual != expected:
            errors.append(f"route.{key} expected={expected}, actual={actual}")
    return len(errors) == 0, errors


def check_agents(actual_agents: list[str], expected_agents: list[str]) -> tuple[bool, list[str]]:
    missing = sorted(set(expected_agents) - set(actual_agents))
    extra = sorted(set(actual_agents) - set(expected_agents))
    errors = []
    if missing:
        errors.append(f"missing_agents={missing}")
    if extra:
        errors.append(f"extra_agents={extra}")
    return len(errors) == 0, errors


def run_single_case(case: dict) -> dict:
    route = DEFAULT_ORCHESTRATOR.route(case["query"])
    actual_route = result_to_dict(route)
    actual_agents = actual_route.get("agent_plan", [])
    expected_agents = case.get("expected_agents", [])

    intent_pass = True
    intent_error = []
    if case.get("expected_intent") and actual_route.get("intent") != case["expected_intent"]:
        intent_pass = False
        intent_error.append(
            f"intent expected={case['expected_intent']}, actual={actual_route.get('intent')}"
        )

    route_pass, route_errors = check_route(actual_route, case.get("expected_route", {}))
    agent_pass, agent_errors = check_agents(actual_agents, expected_agents)
    errors = intent_error + route_errors + agent_errors
    passed = intent_pass and route_pass and agent_pass

    return {
        "case_id": case["id"],
        "query": case["query"],
        "passed": passed,
        "intent_pass": intent_pass,
        "route_pass": route_pass,
        "agent_pass": agent_pass,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": actual_route.get("intent"),
        "expected_route": case.get("expected_route", {}),
        "actual_route": actual_route,
        "expected_agents": expected_agents,
        "actual_agents": actual_agents,
        "reason": "; ".join(errors) if errors else "passed",
        "notes": case.get("notes", ""),
    }


def build_report(results: list[dict]) -> dict:
    total = len(results)
    correct = sum(1 for item in results if item["passed"])
    route_correct = sum(1 for item in results if item["route_pass"] and item["intent_pass"])
    agent_metrics = precision_recall_f1_by_label(results, "expected_agents", "actual_agents")
    failed_cases = [
        {
            "case_id": item["case_id"],
            "query": item["query"],
            "expected": {
                "intent": item["expected_intent"],
                "route": item["expected_route"],
                "agents": item["expected_agents"],
            },
            "actual": {
                "intent": item["actual_intent"],
                "route": item["actual_route"],
                "agents": item["actual_agents"],
            },
            "reason": item["reason"],
        }
        for item in results
        if not item["passed"]
    ]

    return {
        "created_at": None,
        "side_effects": SIDE_EFFECTS,
        "dataset": str(EVAL_PATH),
        "total_cases": total,
        "correct_cases": correct,
        "passed_count": correct,
        "failed_count": total - correct,
        "routing_accuracy": rate(route_correct, total),
        "agent_precision": agent_metrics["macro_precision"],
        "agent_recall": agent_metrics["macro_recall"],
        "agent_f1": agent_metrics["macro_f1"],
        "agent_metrics_by_agent": agent_metrics["per_label"] if total else NA,
        "failed_cases": failed_cases,
        "results": results,
    }


def main() -> None:
    cases = load_jsonl(EVAL_PATH)
    results = [run_single_case(case) for case in cases]
    report = build_report(results)
    report_path = save_report("eval_routing", report)
    print_json_report("Routing Evaluation", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
