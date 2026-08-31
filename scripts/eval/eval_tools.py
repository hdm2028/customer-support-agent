from __future__ import annotations

import argparse

from app.agent.orchestrator import DEFAULT_ORCHESTRATOR
from app.agent.entry.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR
from app.storage.database import init_database
from app.tools.executor import execute_agent_tool
from scripts.eval.common import (
    NA,
    build_skipped_report,
    dangerous_tool_misuse,
    get_tool_names,
    latest_trace_for_conversation,
    load_jsonl,
    new_conversation_id,
    ordered_subsequence,
    print_json_report,
    rate,
    result_to_dict,
    save_report,
    tool_arguments_from_trace,
)


EVAL_PATH = BASE_DIR / "data" / "eval" / "tool_eval.jsonl"
SIDE_EFFECTS = [
    "[WRITES DATABASE: conversation/trace]",
    "[WRITES CACHE]",
    "[CALLS EMBEDDING IF CONFIGURED]",
    "[WRITES REFUND DATABASE AND PUBLISHES MQ WHEN --execute-writes IS USED]",
]


def expected_tool_list(case: dict) -> list[str]:
    return list(case.get("expected_tools") or case.get("expected_tools_contains") or [])


def check_tool_selection(actual_tools: list[str], case: dict) -> tuple[bool, list[str]]:
    if case.get("expected_tools") is not None:
        expected = list(case.get("expected_tools", []))
        if actual_tools != expected:
            return False, [f"tools expected={expected}, actual={actual_tools}"]
        return True, []

    expected_contains = list(case.get("expected_tools_contains", []))
    if expected_contains and not ordered_subsequence(actual_tools, expected_contains):
        return False, [f"tools should contain ordered sequence {expected_contains}, actual={actual_tools}"]

    missing = [
        tool_name
        for tool_name in expected_contains
        if tool_name not in actual_tools
    ]
    if missing:
        return False, [f"missing_tools={missing}"]
    return True, []


def check_argument_rule(actual_value: Any, expected_rule: Any) -> bool:
    if isinstance(expected_rule, list):
        actual_text = "" if actual_value is None else str(actual_value)
        return all(item in actual_text for item in expected_rule)
    return actual_value == expected_rule


def check_arguments(tool_calls: list[dict], expected_arguments: dict) -> tuple[bool | str, list[str]]:
    if not expected_arguments:
        return NA, ["no expected_arguments field"]

    if not tool_calls:
        return NA, ["tool arguments unavailable; execute the case to collect trace tool_call events"]

    errors = []
    for tool_name, expected in expected_arguments.items():
        call = next((item for item in tool_calls if item.get("tool_name") == tool_name), None)
        if not call:
            errors.append(f"missing argument trace for {tool_name}")
            continue

        arguments = call.get("arguments", {})
        for key, expected_rule in expected.items():
            if key.endswith("_contains"):
                actual_key = key.removesuffix("_contains")
                actual_value = arguments.get(actual_key)
            else:
                actual_key = key
                actual_value = arguments.get(actual_key)

            if not check_argument_rule(actual_value, expected_rule):
                errors.append(
                    f"{tool_name}.{actual_key} expected_rule={expected_rule}, actual={actual_value}"
                )

    return len(errors) == 0, errors


def execution_success(result: dict, expected_tools: list[str]) -> bool:
    tool_results = result.get("tool_results", [])
    if not tool_results:
        return False

    relevant_results = [
        item
        for item in tool_results
        if item.get("tool_name") in expected_tools
    ]
    if not relevant_results:
        return False
    return all(item.get("success") is True for item in relevant_results)


def run_permission_case(case: dict) -> dict:
    result = execute_agent_tool(
        agent_key=case["agent_key"],
        tool_name=case["tool_name"],
        arguments=case.get("arguments", {}),
    )
    expected_error_type = case.get("expected_error_type")
    error_type = result.result.get("error_type") if isinstance(result.result, dict) else None
    passed = (result.success is False) and (error_type == expected_error_type)
    reason = "passed" if passed else f"expected_error_type={expected_error_type}, actual={error_type}"

    return {
        "case_id": case["id"],
        "query": f"{case['agent_key']} -> {case['tool_name']}",
        "mode": "permission",
        "passed": passed,
        "selection_pass": passed,
        "argument_pass": NA,
        "execution_pass": passed,
        "dangerous_misuse": [],
        "actual_tools": [case["tool_name"]],
        "expected_tools": [case["tool_name"]],
        "tool_calls": [],
        "tool_result": result.model_dump() if hasattr(result, "model_dump") else result.dict(),
        "reason": reason,
        "notes": case.get("notes", ""),
    }


def run_agent_case(case: dict, execute_writes: bool) -> dict:
    mode = case.get("mode", "execute")
    query = case["query"]
    expected_tools = expected_tool_list(case)

    if mode == "plan_only" and not execute_writes:
        route = DEFAULT_ORCHESTRATOR.route(query)
        actual_route = result_to_dict(route)
        actual_tools = list(actual_route.get("tool_plan", []))
        selection_pass, selection_errors = check_tool_selection(actual_tools, case)
        misuse = [
            tool_name
            for tool_name in actual_tools
            if tool_name in set(case.get("forbidden_tools", []))
        ]
        passed = selection_pass and not misuse
        reason_parts = list(selection_errors)
        if misuse:
            reason_parts.append(f"dangerous_tool_misuse={misuse}")
        reason_parts.append("execution skipped to avoid refund side effects")
        return {
            "case_id": case["id"],
            "query": query,
            "mode": mode,
            "passed": passed,
            "selection_pass": selection_pass,
            "argument_pass": NA,
            "execution_pass": NA,
            "dangerous_misuse": misuse,
            "actual_tools": actual_tools,
            "expected_tools": expected_tools,
            "tool_calls": [],
            "route": actual_route,
            "reason": "; ".join(reason_parts),
            "notes": case.get("notes", ""),
        }

    conversation_id = new_conversation_id("tool-eval", case["id"])
    result = run_customer_support_agent(
        user_message=query,
        conversation_id=conversation_id,
        use_llm=False,
    )
    trace = latest_trace_for_conversation(result.get("conversation_id"))
    tool_calls = tool_arguments_from_trace(trace)
    actual_tools = get_tool_names(result)
    selection_pass, selection_errors = check_tool_selection(actual_tools, case)
    argument_pass, argument_errors = check_arguments(tool_calls, case.get("expected_arguments", {}))
    execution_pass = execution_success(result, expected_tools) if expected_tools else NA
    misuse = dangerous_tool_misuse(result, case.get("forbidden_tools", []))

    passed = bool(
        selection_pass
        and argument_pass is not False
        and execution_pass is not False
        and not misuse
    )
    reason_parts = selection_errors + argument_errors
    if execution_pass is False:
        reason_parts.append("expected tool execution did not succeed")
    if misuse:
        reason_parts.append(f"dangerous_tool_misuse={misuse}")

    return {
        "case_id": case["id"],
        "query": query,
        "mode": mode,
        "passed": passed,
        "selection_pass": selection_pass,
        "argument_pass": argument_pass,
        "execution_pass": execution_pass,
        "dangerous_misuse": misuse,
        "actual_tools": actual_tools,
        "expected_tools": expected_tools,
        "tool_calls": tool_calls,
        "trace_id": trace.get("trace_id") if trace else NA,
        "route": result.get("route", {}),
        "reply": result.get("reply", ""),
        "reason": "; ".join(reason_parts) if reason_parts else "passed",
        "notes": case.get("notes", ""),
    }


def run_single_case(case: dict, execute_writes: bool = False) -> dict:
    if case.get("mode") == "permission":
        return run_permission_case(case)
    return run_agent_case(case, execute_writes=execute_writes)


def build_report(results: list[dict], execute_writes: bool) -> dict:
    total = len(results)
    selection_supported = [item for item in results if item["selection_pass"] != NA]
    argument_supported = [item for item in results if item["argument_pass"] != NA]
    execution_supported = [item for item in results if item["execution_pass"] != NA]
    passed_count = sum(1 for item in results if item["passed"])
    dangerous_count = sum(len(item["dangerous_misuse"]) for item in results)
    failed_cases = [
        {
            "case_id": item["case_id"],
            "query": item["query"],
            "expected": {
                "tools": item["expected_tools"],
            },
            "actual": {
                "tools": item["actual_tools"],
                "tool_calls": item.get("tool_calls", []),
                "reply": item.get("reply"),
            },
            "reason": item["reason"],
        }
        for item in results
        if not item["passed"]
    ]

    return {
        "side_effects": SIDE_EFFECTS,
        "dataset": str(EVAL_PATH),
        "execute_writes": execute_writes,
        "total_cases": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "tool_selection_accuracy": rate(
            sum(1 for item in selection_supported if item["selection_pass"] is True),
            len(selection_supported),
        ),
        "tool_argument_accuracy": rate(
            sum(1 for item in argument_supported if item["argument_pass"] is True),
            len(argument_supported),
        ),
        "tool_execution_success_rate": rate(
            sum(1 for item in execution_supported if item["execution_pass"] is True),
            len(execution_supported),
        ),
        "dangerous_tool_misuse_count": dangerous_count,
        "failed_cases": failed_cases,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate refund workflow tool selection and arguments.")
    parser.add_argument(
        "--execute-writes",
        action="store_true",
        help="Execute refund_apply cases. This can create refund rows and MQ messages.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases = []
    try:
        cases = load_jsonl(EVAL_PATH)
        init_database()
        results = [run_single_case(case, execute_writes=args.execute_writes) for case in cases]
        report = build_report(results, execute_writes=args.execute_writes)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
            dataset=str(EVAL_PATH),
            dataset_cases=len(cases),
        )
    report_path = save_report("eval_tools", report)
    print_json_report("Tool Calling Evaluation", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
