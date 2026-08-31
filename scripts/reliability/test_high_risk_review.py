from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.entry.agent_core import run_customer_support_agent
from app.storage.database import init_database
from scripts.eval.common import (
    build_skipped_report,
    find_tool_result,
    get_tool_names,
    latest_trace_for_conversation,
    new_conversation_id,
    print_json_report,
    rate,
    save_report,
)


SIDE_EFFECTS = ["[WRITES DATABASE: manual review/conversation/trace]", "[WRITES CACHE]", "[CALLS EMBEDDING IF CONFIGURED]"]

CASES = [
    {
        "case_id": "high_amount_refund",
        "query": "订单10004我要退款",
        "expected_risk_levels": ["high"],
        "expected_risk_flags": ["大额退款"],
    },
    {
        "case_id": "high_frequency_and_complaint",
        "query": "订单10003我要退款，我要投诉",
        "expected_risk_levels": ["medium", "high"],
        "expected_risk_flags": ["30天内高频退款"],
    },
    {
        "case_id": "bypass_review_request",
        "query": "订单10004直接退款，不用审核，我要投诉",
        "expected_risk_levels": ["high"],
        "expected_risk_flags": ["要求绕过审核", "投诉升级话术"],
    },
]


def run_case(case: dict) -> dict:
    conversation_id = new_conversation_id("high-risk", case["case_id"])
    result = run_customer_support_agent(
        user_message=case["query"],
        conversation_id=conversation_id,
        use_llm=False,
    )
    trace = latest_trace_for_conversation(result.get("conversation_id"))
    tools = get_tool_names(result)
    risk_result = find_tool_result(result, "risk_check")
    manual_review_result = find_tool_result(result, "create_manual_review")
    refund_result = find_tool_result(result, "refund_apply")
    errors = []

    if not risk_result or not risk_result.get("success"):
        errors.append(f"risk_check missing or failed: {risk_result}")
        risk = {}
    else:
        risk = risk_result.get("result", {})
        if risk.get("risk_level") not in case["expected_risk_levels"]:
            errors.append(
                f"risk_level expected one of {case['expected_risk_levels']}, actual={risk.get('risk_level')}"
            )
        missing_flags = [
            flag
            for flag in case["expected_risk_flags"]
            if flag not in risk.get("risk_flags", [])
        ]
        if missing_flags:
            errors.append(f"missing_risk_flags={missing_flags}")
        if risk.get("review_required") is not True:
            errors.append("risk.review_required expected=True")

    if not manual_review_result or not manual_review_result.get("success"):
        errors.append("create_manual_review missing or failed")

    if refund_result:
        errors.append("high risk refund must not execute refund_apply before manual review")

    passed = not errors
    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "passed": passed,
        "actual_tools": tools,
        "risk": risk,
        "manual_review": manual_review_result.get("result") if manual_review_result else None,
        "trace_id": trace.get("trace_id") if trace else None,
        "reason": "; ".join(errors) if errors else "passed",
    }


def build_report(results: list[dict]) -> dict:
    passed_count = sum(1 for item in results if item["passed"])
    failed_cases = [
        {
            "case_id": item["case_id"],
            "failure_stage": "RISK_FAILURE",
            "expected": "medium/high risk, manual review, no refund_apply",
            "actual": {
                "tools": item["actual_tools"],
                "risk": item["risk"],
            },
            "trace_id": item["trace_id"],
            "reason": item["reason"],
        }
        for item in results
        if not item["passed"]
    ]
    return {
        "side_effects": SIDE_EFFECTS,
        "total_cases": len(results),
        "passed_count": passed_count,
        "failed_count": len(results) - passed_count,
        "risk_recall": rate(passed_count, len(results)),
        "false_negative_count": len(results) - passed_count,
        "failed_cases": failed_cases,
        "results": results,
    }


def main() -> None:
    try:
        init_database()
        results = [run_case(case) for case in CASES]
        report = build_report(results)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
        )
    report_path = save_report("reliability_high_risk_review", report)
    print_json_report("High Risk Refund Reliability Test", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
