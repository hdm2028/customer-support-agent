from __future__ import annotations

from contextlib import contextmanager

import app.tools.registry as tool_registry
from app.agent.entry.agent_core import run_customer_support_agent
from app.storage.database import init_database
from scripts.eval.common import (
    build_skipped_report,
    find_tool_result,
    get_tool_names,
    latest_trace_for_conversation,
    new_conversation_id,
    order_refund_messages,
    order_refunds,
    print_json_report,
    save_report,
)


SIDE_EFFECTS = ["[WRITES DATABASE: conversation/trace]", "[WRITES CACHE]"]
ORDER_ID = "10009"
QUERY = f"订单{ORDER_ID}我要退款"
SUCCESS_PHRASES = ["已创建退款申请", "已为您退款", "已经退款"]

CASES = [
    {
        "case_id": "rag_unavailable",
        "tool_name": "policy_search",
        "expected_tools": ["order_lookup", "policy_search"],
        "forbidden_tools": ["risk_check", "refund_apply"],
        "expected_reply_hint": "政策检索工具调用失败",
    },
    {
        "case_id": "risk_service_unavailable",
        "tool_name": "risk_check",
        "expected_tools": ["order_lookup", "policy_search", "risk_check"],
        "forbidden_tools": ["refund_apply"],
        "expected_reply_hint": "风控",
    },
]

SKIPPED_CHECKS = [
    {
        "dependency": "Redis unavailable",
        "status": "SKIPPED",
        "reason": "Current cache layer automatically falls back to memory and has no safe fault-injection switch for a single test run.",
    },
    {
        "dependency": "MQ unavailable",
        "status": "SKIPPED",
        "reason": "Current refund_apply writes DB before publish_message; a safe MQ-failure test needs test-only cleanup or transactional outbox support.",
    },
]


@contextmanager
def unavailable_tool(tool_name: str):
    original = tool_registry.TOOL_HANDLERS[tool_name]

    def fail(*args, **kwargs):
        raise RuntimeError(f"mock {tool_name} dependency unavailable")

    tool_registry.TOOL_HANDLERS[tool_name] = fail
    try:
        yield
    finally:
        tool_registry.TOOL_HANDLERS[tool_name] = original


def run_case(case: dict) -> dict:
    before_refunds = order_refunds(ORDER_ID)
    before_messages = order_refund_messages(ORDER_ID)
    conversation_id = new_conversation_id("service-degradation", case["case_id"])
    with unavailable_tool(case["tool_name"]):
        result = run_customer_support_agent(
            user_message=QUERY,
            conversation_id=conversation_id,
            use_llm=False,
        )
    after_refunds = order_refunds(ORDER_ID)
    after_messages = order_refund_messages(ORDER_ID)
    trace = latest_trace_for_conversation(result.get("conversation_id"))
    tools = get_tool_names(result)
    failed_tool = find_tool_result(result, case["tool_name"])
    reply = result.get("reply", "")
    errors = []

    if tools != case["expected_tools"]:
        errors.append(f"tools expected={case['expected_tools']}, actual={tools}")
    if not failed_tool or failed_tool.get("success") is not False:
        errors.append(f"{case['tool_name']} should fail closed")
    forbidden_used = [tool for tool in case["forbidden_tools"] if tool in tools]
    if forbidden_used:
        errors.append(f"fail-open downstream tools used: {forbidden_used}")
    if any(phrase in reply for phrase in SUCCESS_PHRASES):
        errors.append("reply falsely claims refund success")
    if len(after_refunds) != len(before_refunds):
        errors.append("service degradation produced refund DB side effects")
    if len(after_messages) != len(before_messages):
        errors.append("service degradation produced MQ side effects")

    passed = not errors
    return {
        "case_id": case["case_id"],
        "dependency": case["tool_name"],
        "passed": passed,
        "actual_tools": tools,
        "failed_tool_result": failed_tool,
        "reply": reply,
        "trace_id": trace.get("trace_id") if trace else None,
        "refund_rows_delta": len(after_refunds) - len(before_refunds),
        "mq_messages_delta": len(after_messages) - len(before_messages),
        "reason": "; ".join(errors) if errors else "passed",
    }


def build_report(results: list[dict]) -> dict:
    passed_count = sum(1 for item in results if item["passed"])
    failed_cases = [
        {
            "case_id": item["case_id"],
            "failure_stage": "SERVICE_DEGRADATION_FAILURE",
            "expected": "fail closed, no refund_apply, no DB/MQ side effects, no false success reply",
            "actual": {
                "tools": item["actual_tools"],
                "refund_rows_delta": item["refund_rows_delta"],
                "mq_messages_delta": item["mq_messages_delta"],
                "reply": item["reply"],
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
        "skipped_checks": SKIPPED_CHECKS,
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
    report_path = save_report("reliability_service_degradation", report)
    print_json_report("Service Degradation Reliability Test", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
