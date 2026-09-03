from __future__ import annotations

from typing import Any

from app.agent.entry.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR
from app.storage.database import init_database
from scripts.eval.common import (
    NA,
    active_order_refunds,
    build_skipped_report,
    dangerous_tool_misuse,
    find_tool_result,
    get_tool_results,
    get_tool_names,
    latest_trace_for_conversation,
    load_jsonl,
    new_conversation_id,
    order_refund_messages,
    order_refunds,
    print_json_report,
    rate,
    save_report,
    tool_arguments_from_trace,
)


EVAL_PATH = BASE_DIR / "data" / "eval" / "e2e_eval.jsonl"
SIDE_EFFECTS = [
    "[WRITES DATABASE]",
    "[USES REDIS OR MEMORY CACHE]",
    "[PUBLISHES MQ WHEN REFUND IS CREATED]",
    "[CALLS EMBEDDING IF CONFIGURED]",
]

TOOL_AGENT_MAP = {
    "order_lookup": "after_sales_agent",
    "refund_apply": "after_sales_agent",
    "create_ticket": "after_sales_agent",
    "create_manual_review": "after_sales_agent",
    "transfer_to_human": "after_sales_agent",
    "ticket_decision": "after_sales_agent",
    "policy_search": "customer_agent",
    "risk_check": "risk_agent",
}


def snapshot_order_side_effects(order_id: str | None) -> dict:
    if not order_id:
        return {
            "refunds": NA,
            "active_refunds": NA,
            "mq_messages": NA,
        }

    return {
        "refunds": order_refunds(order_id),
        "active_refunds": active_order_refunds(order_id),
        "mq_messages": order_refund_messages(order_id),
    }


def runtime_agent_steps(result: dict) -> list[dict]:
    return (
        result.get("orchestration", {})
        .get("runtime_agent_steps", [])
    )


def runtime_agent_keys(result: dict) -> list[str]:
    return [
        step.get("agent_key")
        for step in runtime_agent_steps(result)
        if step.get("tool_names")
    ]


def expected_agent_keys(case: dict) -> list[str]:
    keys = []
    for tool_name in case.get("expected_tools", []):
        key = TOOL_AGENT_MAP.get(tool_name)
        if key and key not in keys:
            keys.append(key)
    return keys


def check_route(result: dict, case: dict) -> tuple[bool, list[str]]:
    errors = []
    route = result.get("route", {})
    expected_order_id = case.get("expected_order_id")

    if route.get("order_id") != expected_order_id:
        errors.append(f"route.order_id expected={expected_order_id}, actual={route.get('order_id')}")

    for key, expected_value in case.get("expected_route", {}).items():
        actual_value = route.get(key)
        if actual_value != expected_value:
            errors.append(f"route.{key} expected={expected_value}, actual={actual_value}")

    return len(errors) == 0, errors


def check_agents(result: dict, case: dict) -> tuple[bool | str, list[str]]:
    expected = expected_agent_keys(case)
    if not expected:
        return NA, []

    actual = runtime_agent_keys(result)
    missing = [key for key in expected if key not in actual]
    if missing:
        return False, [f"missing_runtime_agents={missing}, actual={actual}"]
    return True, []


def check_tool_order(actual_tools: list[str], route: dict) -> list[str]:
    errors = []

    def require_before(first: str, second: str) -> None:
        if first in actual_tools and second in actual_tools and actual_tools.index(first) > actual_tools.index(second):
            errors.append(f"{first} must run before {second}")

    for downstream in ["policy_search", "risk_check", "refund_apply", "create_ticket", "create_manual_review"]:
        require_before("order_lookup", downstream)

    if route.get("need_policy"):
        require_before("policy_search", "refund_apply")
    require_before("risk_check", "refund_apply")
    require_before("risk_check", "create_manual_review")

    return errors


def check_tools(result: dict, case: dict) -> tuple[bool, list[str]]:
    errors = []
    actual_tools = get_tool_names(result)
    expected_tools = list(case.get("expected_tools", []))

    if actual_tools != expected_tools:
        errors.append(f"tools expected={expected_tools}, actual={actual_tools}")

    for tool_name, expected_success in case.get("expected_tool_success", {}).items():
        tool_result = find_tool_result(result, tool_name)
        if not tool_result:
            errors.append(f"missing tool result: {tool_name}")
            continue
        if tool_result.get("success") != expected_success:
            errors.append(f"{tool_name}.success expected={expected_success}, actual={tool_result.get('success')}")

    forbidden = list(case.get("forbidden_tools", []))
    if forbidden:
        misuse = dangerous_tool_misuse(result, forbidden)
        if misuse:
            errors.append(f"dangerous_tool_misuse={misuse}")

    errors.extend(check_tool_order(actual_tools, result.get("route", {})))
    return len(errors) == 0, errors


def check_tool_arguments(tool_calls: list[dict], result: dict, case: dict) -> tuple[bool | str, list[str]]:
    expected_order_id = case.get("expected_order_id")
    if not tool_calls:
        expected_tools = case.get("expected_tools", [])
        return (NA, []) if not expected_tools else (False, ["missing tool_call trace events"])

    errors = []
    for call in tool_calls:
        tool_name = call.get("tool_name")
        arguments = call.get("arguments", {})
        if expected_order_id and "order_id" in arguments and str(arguments["order_id"]) != str(expected_order_id):
            errors.append(f"{tool_name}.order_id expected={expected_order_id}, actual={arguments.get('order_id')}")

        if tool_name == "policy_search":
            for query_field in ("semantic_query", "lexical_query"):
                if not str(arguments.get(query_field, "")).strip():
                    errors.append(f"policy_search.{query_field} is empty")

        if tool_name == "refund_apply":
            user_request = str(arguments.get("user_request", ""))
            if expected_order_id and expected_order_id not in user_request:
                errors.append(f"refund_apply.user_request missing order_id={expected_order_id}")
            if "risk_assessment" not in arguments or not isinstance(arguments.get("risk_assessment"), dict):
                errors.append("refund_apply.risk_assessment missing")

    return len(errors) == 0, errors


def check_rag(result: dict, case: dict) -> tuple[bool | str, list[str]]:
    if "policy_search" not in case.get("expected_tools", []):
        return NA, []

    policy_result = find_tool_result(result, "policy_search")
    if not policy_result:
        return False, ["policy_search missing"]
    if not policy_result.get("success"):
        return False, [f"policy_search failed: {policy_result.get('result')}"]
    if not isinstance(policy_result.get("result"), list) or not policy_result["result"]:
        return False, ["policy_search returned empty evidence"]
    if not any(item.get("citation") or item.get("source") for item in policy_result["result"]):
        return False, ["policy_search evidence missing citation/source"]
    return True, []


def check_risk(result: dict, case: dict) -> tuple[bool | str, list[str]]:
    if "risk_check" not in case.get("expected_tools", []):
        return NA, []

    risk_result = find_tool_result(result, "risk_check")
    if not risk_result:
        return False, ["risk_check missing"]
    if not risk_result.get("success"):
        return False, [f"risk_check failed: {risk_result.get('result')}"]

    assessment = risk_result.get("result", {})
    if not isinstance(assessment, dict) or "risk_level" not in assessment:
        return False, ["risk_check result missing risk_level"]

    if case.get("expected_route", {}).get("risk_level") == "high" and assessment.get("risk_level") != "high":
        return False, [f"risk_level expected=high, actual={assessment.get('risk_level')}"]

    return True, []


def check_refund(result: dict, case: dict) -> tuple[bool | str, list[str]]:
    expects_refund = "refund_apply" in case.get("expected_tools", [])
    refund_result = find_tool_result(result, "refund_apply")

    if expects_refund and not refund_result:
        return False, ["refund_apply missing"]
    if not expects_refund and refund_result:
        return False, ["refund_apply ran when it was not expected"]
    if not refund_result:
        return NA, []

    expected_success = case.get("expected_tool_success", {}).get("refund_apply")
    if expected_success is not None and refund_result.get("success") != expected_success:
        return False, [f"refund_apply.success expected={expected_success}, actual={refund_result.get('success')}"]

    if refund_result.get("success") and not isinstance(refund_result.get("result"), dict):
        return False, ["refund_apply success result is not a dict"]

    return True, []


def infer_final_action(result: dict) -> str:
    route = result.get("route", {})
    tool_names = get_tool_names(result)
    create_ticket_result = find_tool_result(result, "create_ticket")
    ticket_decision_result = find_tool_result(result, "ticket_decision")
    order_lookup_result = find_tool_result(result, "order_lookup")
    refund_result = find_tool_result(result, "refund_apply")
    manual_review_result = find_tool_result(result, "create_manual_review")

    if route.get("blocked_by_guardrail"):
        return "blocked_by_guardrail"

    if route.get("need_clarification"):
        if route.get("order_id"):
            return "ask_for_missing_slot"
        return "ask_for_order_id"

    if route.get("handoff_required") and not route.get("order_id") and not tool_names:
        return "handoff_without_tools"

    if order_lookup_result and not order_lookup_result.get("success"):
        return "order_lookup_failed"

    if ticket_decision_result and not ticket_decision_result.get("success"):
        return "ticket_blocked"

    if refund_result and refund_result.get("success"):
        refund = refund_result.get("result", {})
        if refund.get("status") == "pending_manual_review":
            return "refund_pending_manual_review"
        return "refund_queued"

    if manual_review_result and manual_review_result.get("success"):
        return "manual_review_created"

    if refund_result and not refund_result.get("success"):
        return "refund_apply_failed"

    if create_ticket_result and create_ticket_result.get("success"):
        ticket = create_ticket_result.get("result", {})
        if ticket.get("status") == "pending_human_review":
            return "create_ticket_pending_review"
        return "create_ticket"

    if find_tool_result(result, "policy_search"):
        return "policy_answer"

    return "reply_only"


def check_final_action(result: dict, case: dict) -> tuple[bool, list[str]]:
    expected_action = case.get("expected_final_action")
    if not expected_action:
        return True, []

    actual_action = infer_final_action(result)
    if actual_action != expected_action:
        return False, [f"final_action expected={expected_action}, actual={actual_action}"]

    return True, []


def check_reply(result: dict, case: dict) -> tuple[bool, list[str]]:
    errors = []
    reply = result.get("reply", "")
    for phrase in case.get("must_include", []):
        if phrase not in reply:
            errors.append(f"reply missing phrase: {phrase}")
    for phrase in case.get("must_not_include", []):
        if phrase in reply:
            errors.append(f"reply contains forbidden phrase: {phrase}")
    return len(errors) == 0, errors


def check_database_and_mq(before: dict, after: dict, result: dict, case: dict) -> tuple[bool | str, bool | str, list[str]]:
    order_id = case.get("expected_order_id")
    if not order_id:
        return NA, NA, []

    errors = []
    refund_result = find_tool_result(result, "refund_apply")
    before_refunds = before["refunds"]
    after_refunds = after["refunds"]
    before_active = before["active_refunds"]
    after_active = after["active_refunds"]
    before_messages = before["mq_messages"]
    after_messages = after["mq_messages"]
    new_refund_rows = len(after_refunds) - len(before_refunds)
    new_mq_messages = len(after_messages) - len(before_messages)

    if refund_result and refund_result.get("success"):
        refund = refund_result.get("result", {})
        refund_id = refund.get("refund_id")
        active_ids = {item.get("refund_id") for item in after_active}
        if refund_id not in active_ids:
            errors.append(f"database missing active refund_id={refund_id}")
        if len(active_ids) != 1:
            errors.append(f"duplicate active refunds for order {order_id}: {sorted(active_ids)}")

        replay = refund.get("idempotent_replay") is True
        if replay and new_refund_rows != 0:
            errors.append(f"idempotent replay created new refund rows: {new_refund_rows}")
        if not replay and new_refund_rows not in {0, 1}:
            errors.append(f"unexpected new refund rows: {new_refund_rows}")

        mq_message_id = refund.get("mq_message_id")
        if replay:
            mq_pass = new_mq_messages == 0
            if not mq_pass:
                errors.append(f"idempotent replay published new MQ messages: {new_mq_messages}")
        else:
            mq_ids = {item.get("message_id") for item in after_messages}
            mq_pass = bool(mq_message_id and mq_message_id in mq_ids and new_mq_messages == 1)
            if not mq_pass:
                errors.append(
                    f"MQ event mismatch expected_id={mq_message_id}, new_mq_messages={new_mq_messages}"
                )

        db_pass = not any("database" in error or "refund rows" in error or "duplicate active" in error for error in errors)
        return db_pass, mq_pass, errors

    if refund_result and not refund_result.get("success"):
        db_pass = new_refund_rows == 0
        mq_pass = new_mq_messages == 0
        if not db_pass:
            errors.append(f"failed refund created DB rows: {new_refund_rows}")
        if not mq_pass:
            errors.append(f"failed refund published MQ messages: {new_mq_messages}")
        return db_pass, mq_pass, errors

    forbidden_refund = "refund_apply" not in case.get("expected_tools", [])
    if forbidden_refund:
        db_pass = new_refund_rows == 0
        mq_pass = new_mq_messages == 0
        if not db_pass:
            errors.append(f"non-refund case created DB rows: {new_refund_rows}")
        if not mq_pass:
            errors.append(f"non-refund case published MQ messages: {new_mq_messages}")
        return db_pass, mq_pass, errors

    return NA, NA, []


def classify_failure(checks: dict, errors_by_stage: dict[str, list[str]]) -> str:
    ordered = [
        ("route_pass", "ROUTING_FAILURE"),
        ("agent_pass", "AGENT_FAILURE"),
        ("rag_pass", "RETRIEVAL_FAILURE"),
        ("tool_pass", "TOOL_SELECTION_FAILURE"),
        ("tool_argument_pass", "TOOL_ARGUMENT_FAILURE"),
        ("risk_pass", "RISK_FAILURE"),
        ("refund_pass", "REFUND_FAILURE"),
        ("database_pass", "DATABASE_FAILURE"),
        ("mq_pass", "MQ_FAILURE"),
        ("action_pass", "WORKFLOW_FAILURE"),
        ("answer_pass", "ANSWER_FAILURE"),
    ]
    for key, stage in ordered:
        if checks.get(key) is False:
            return stage
    if any(errors_by_stage.values()):
        return "WORKFLOW_FAILURE"
    return "NONE"


def is_success(value: bool | str) -> bool:
    return value is True or value == NA


def run_single_case(case: dict) -> dict[str, Any]:
    init_database()
    order_id = case.get("expected_order_id")
    before = snapshot_order_side_effects(order_id)
    conversation_id = new_conversation_id("e2e-eval", case["id"])
    result = run_customer_support_agent(
        user_message=case["user_message"],
        conversation_id=conversation_id,
        use_llm=False,
    )
    trace = latest_trace_for_conversation(result.get("conversation_id"))
    tool_calls = tool_arguments_from_trace(trace)
    after = snapshot_order_side_effects(order_id)

    route_pass, route_errors = check_route(result, case)
    agent_pass, agent_errors = check_agents(result, case)
    rag_pass, rag_errors = check_rag(result, case)
    tool_pass, tool_errors = check_tools(result, case)
    tool_argument_pass, argument_errors = check_tool_arguments(tool_calls, result, case)
    risk_pass, risk_errors = check_risk(result, case)
    refund_pass, refund_errors = check_refund(result, case)
    action_pass, action_errors = check_final_action(result, case)
    answer_pass, answer_errors = check_reply(result, case)
    database_pass, mq_pass, persistence_errors = check_database_and_mq(before, after, result, case)

    checks = {
        "route_pass": route_pass,
        "agent_pass": agent_pass,
        "rag_pass": rag_pass,
        "tool_pass": tool_pass,
        "tool_argument_pass": tool_argument_pass,
        "risk_pass": risk_pass,
        "refund_pass": refund_pass,
        "database_pass": database_pass,
        "mq_pass": mq_pass,
        "action_pass": action_pass,
        "answer_pass": answer_pass,
    }
    errors_by_stage = {
        "route": route_errors,
        "agent": agent_errors,
        "rag": rag_errors,
        "tool": tool_errors,
        "tool_argument": argument_errors,
        "risk": risk_errors if isinstance(risk_errors, list) else [risk_errors],
        "refund": refund_errors,
        "database_mq": persistence_errors,
        "action": action_errors,
        "answer": answer_errors,
    }
    failure_stage = classify_failure(checks, errors_by_stage)
    passed = all(is_success(value) for value in checks.values())

    return {
        "case_id": case["id"],
        "intent": case.get("intent"),
        "user_message": case["user_message"],
        "passed": passed,
        "failure_stage": failure_stage,
        "checks": checks,
        "expected_order_id": order_id,
        "actual_order_id": result.get("route", {}).get("order_id"),
        "expected_agents": expected_agent_keys(case),
        "actual_agents": runtime_agent_keys(result),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": get_tool_names(result),
        "tool_calls": tool_calls,
        "expected_final_action": case.get("expected_final_action"),
        "actual_final_action": infer_final_action(result),
        "route": result.get("route", {}),
        "tool_results": get_tool_results(result),
        "reply": result.get("reply", ""),
        "trace_id": trace.get("trace_id") if trace else NA,
        "side_effects": {
            "before": {
                "refund_count": len(before["refunds"]) if before["refunds"] != NA else NA,
                "active_refund_count": len(before["active_refunds"]) if before["active_refunds"] != NA else NA,
                "mq_message_count": len(before["mq_messages"]) if before["mq_messages"] != NA else NA,
            },
            "after": {
                "refund_count": len(after["refunds"]) if after["refunds"] != NA else NA,
                "active_refund_count": len(after["active_refunds"]) if after["active_refunds"] != NA else NA,
                "mq_message_count": len(after["mq_messages"]) if after["mq_messages"] != NA else NA,
            },
        },
        "errors_by_stage": errors_by_stage,
    }


def pass_rate(results: list[dict], key: str) -> float | str:
    supported = [item for item in results if item["checks"][key] != NA]
    return rate(sum(1 for item in supported if item["checks"][key] is True), len(supported))


def build_report(results: list[dict]) -> dict:
    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    failed_cases = [
        {
            "case_id": item["case_id"],
            "failure_stage": item["failure_stage"],
            "expected": {
                "order_id": item["expected_order_id"],
                "agents": item["expected_agents"],
                "tools": item["expected_tools"],
                "final_action": item["expected_final_action"],
            },
            "actual": {
                "order_id": item["actual_order_id"],
                "agents": item["actual_agents"],
                "tools": item["actual_tools"],
                "tool_calls": item["tool_calls"],
                "final_action": item["actual_final_action"],
                "reply": item["reply"],
                "side_effects": item["side_effects"],
            },
            "trace_id": item["trace_id"],
            "reason": item["errors_by_stage"],
        }
        for item in results
        if not item["passed"]
    ]

    return {
        "side_effects": SIDE_EFFECTS,
        "dataset": str(EVAL_PATH),
        "total_cases": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "task_success_rate": rate(passed_count, total),
        "routing_success_rate": pass_rate(results, "route_pass"),
        "agent_success_rate": pass_rate(results, "agent_pass"),
        "rag_success_rate": pass_rate(results, "rag_pass"),
        "tool_success_rate": pass_rate(results, "tool_pass"),
        "tool_argument_success_rate": pass_rate(results, "tool_argument_pass"),
        "risk_success_rate": pass_rate(results, "risk_pass"),
        "refund_success_rate": pass_rate(results, "refund_pass"),
        "database_success_rate": pass_rate(results, "database_pass"),
        "mq_success_rate": pass_rate(results, "mq_pass"),
        "workflow_success_rate": rate(
            sum(
                1
                for item in results
                if (
                    is_success(item["checks"]["agent_pass"])
                    and is_success(item["checks"]["tool_argument_pass"])
                    and is_success(item["checks"]["database_pass"])
                    and is_success(item["checks"]["mq_pass"])
                    and item["checks"]["action_pass"] is True
                )
            ),
            total,
        ),
        "answer_success_rate": pass_rate(results, "answer_pass"),
        "failed_cases": failed_cases,
        "results": results,
    }


def main() -> None:
    cases = []
    try:
        cases = load_jsonl(EVAL_PATH)
        init_database()
        results = [run_single_case(case) for case in cases]
        report = build_report(results)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
            dataset=str(EVAL_PATH),
            dataset_cases=len(cases),
        )
    report_path = save_report("eval_e2e", report)
    print_json_report("End-to-End Refund Workflow Evaluation", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
