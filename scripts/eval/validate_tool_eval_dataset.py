from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR
from app.tools.registry import AGENT_TOOL_PERMISSIONS, FUNCTION_TOOL_SPECS


EVAL_PATH = BASE_DIR / "data" / "eval" / "tool_eval.jsonl"
ORDERS_PATH = BASE_DIR / "data" / "orders.json"
VALID_MODES = {"execute", "plan_only", "permission"}
WRITE_TOOLS = {"refund_apply", "create_manual_review", "create_ticket"}
EXPECTED_PERMISSION_ERROR = "ToolPermissionDenied"


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    cases: list[dict[str, Any]] = []
    errors: list[str] = []

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue

        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as error:
            errors.append(f"line {line_number}: malformed JSON: {error}")
            continue

        if not isinstance(value, dict):
            errors.append(f"line {line_number}: case must be a JSON object")
            continue

        value["_line_number"] = line_number
        cases.append(value)

    return cases, errors


def tool_contract() -> dict[str, dict[str, Any]]:
    return {
        spec["function"]["name"]: spec["function"]["parameters"]
        for spec in FUNCTION_TOOL_SPECS
    }


def asserted_argument_name(name: str) -> str:
    return name.removesuffix("_contains")


def normalized_query(query: str) -> str:
    return re.sub(r"[\W_]+", "", query.lower())


def add_duplicate_errors(
    cases: list[dict[str, Any]],
    field: str,
    errors: list[str],
) -> None:
    values: dict[str, list[int]] = defaultdict(list)

    for case in cases:
        raw_value = case.get(field)
        if raw_value in (None, ""):
            continue

        value = normalized_query(raw_value) if field == "query" else str(raw_value)
        values[value].append(case["_line_number"])

    for value, lines in values.items():
        if len(lines) > 1:
            errors.append(f"duplicate {field} at lines {lines}: {value}")


def validate_case(
    case: dict[str, Any],
    contract: dict[str, dict[str, Any]],
    order_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    case_id = case.get("id") or f"line-{case['_line_number']}"
    mode = case.get("mode", "execute")
    registered_tools = set(contract)

    if not case.get("id"):
        errors.append(f"{case_id}: missing id")
    if mode not in VALID_MODES:
        errors.append(f"{case_id}: invalid mode {mode!r}")

    exact_present = "expected_tools" in case
    contains_present = "expected_tools_contains" in case
    if mode != "permission" and exact_present == contains_present:
        errors.append(
            f"{case_id}: agent case must define exactly one of expected_tools "
            "or expected_tools_contains"
        )

    expected_tools = list(
        case.get("expected_tools")
        or case.get("expected_tools_contains")
        or []
    )
    forbidden_tools = list(case.get("forbidden_tools") or [])
    referenced_tools = set(expected_tools) | set(forbidden_tools)
    invalid_tools = sorted(referenced_tools - registered_tools)
    if invalid_tools:
        errors.append(f"{case_id}: invalid tool names {invalid_tools}")

    overlap = sorted(set(expected_tools) & set(forbidden_tools))
    if overlap:
        errors.append(f"{case_id}: tools both expected and forbidden {overlap}")

    expected_arguments = case.get("expected_arguments") or {}
    if not isinstance(expected_arguments, dict):
        errors.append(f"{case_id}: expected_arguments must be an object")
        expected_arguments = {}

    for tool_name, assertions in expected_arguments.items():
        if tool_name not in contract:
            errors.append(f"{case_id}: arguments reference unknown tool {tool_name}")
            continue
        if tool_name not in expected_tools:
            errors.append(f"{case_id}: arguments supplied for non-expected tool {tool_name}")
        if not isinstance(assertions, dict):
            errors.append(f"{case_id}: {tool_name} assertions must be an object")
            continue

        properties = set(contract[tool_name].get("properties", {}))
        assertion_fields = {
            asserted_argument_name(name)
            for name in assertions
        }
        invalid_fields = sorted(assertion_fields - properties)
        if invalid_fields:
            errors.append(
                f"{case_id}: invalid {tool_name} argument fields {invalid_fields}"
            )

        required = set(contract[tool_name].get("required", []))
        missing_required = sorted(required - assertion_fields)
        if missing_required:
            errors.append(
                f"{case_id}: missing required {tool_name} argument assertions "
                f"{missing_required}"
            )

    if mode != "permission":
        missing_argument_blocks = sorted(
            tool_name
            for tool_name in expected_tools
            if tool_name not in expected_arguments
        )
        if missing_argument_blocks:
            errors.append(
                f"{case_id}: expected tools lack argument assertions "
                f"{missing_argument_blocks}"
            )

    if "policy_search" in expected_tools:
        policy_assertions = expected_arguments.get("policy_search", {})
        policy_fields = {
            asserted_argument_name(name)
            for name in policy_assertions
        }
        if "query" in policy_fields:
            errors.append(f"{case_id}: policy_search uses removed query field")
        for field in ("semantic_query", "lexical_query"):
            if field not in policy_fields:
                errors.append(
                    f"{case_id}: policy_search must assert {field}"
                )

    if mode == "permission":
        agent_key = case.get("agent_key")
        tool_name = case.get("tool_name")
        arguments = case.get("arguments") or {}
        if agent_key not in AGENT_TOOL_PERMISSIONS:
            errors.append(f"{case_id}: unknown permission agent {agent_key!r}")
        if tool_name not in contract:
            errors.append(f"{case_id}: unknown permission tool {tool_name!r}")
        elif tool_name in AGENT_TOOL_PERMISSIONS.get(agent_key, set()):
            errors.append(f"{case_id}: permission pair is allowed, not denied")
        if case.get("expected_error_type") != EXPECTED_PERMISSION_ERROR:
            errors.append(
                f"{case_id}: expected_error_type must be "
                f"{EXPECTED_PERMISSION_ERROR}"
            )
        if tool_name in contract:
            properties = set(contract[tool_name].get("properties", {}))
            invalid = sorted(set(arguments) - properties)
            if invalid:
                errors.append(
                    f"{case_id}: permission probe has invalid arguments {invalid}"
                )
            required = set(contract[tool_name].get("required", []))
            missing = sorted(
                name
                for name in required
                if arguments.get(name) in (None, "")
            )
            if missing:
                errors.append(
                    f"{case_id}: permission probe lacks required arguments {missing}"
                )

    expected_write_tools = set(expected_tools) & WRITE_TOOLS
    if mode != "permission" and expected_write_tools and mode != "plan_only":
        errors.append(
            f"{case_id}: write tools must use plan_only: "
            f"{sorted(expected_write_tools)}"
        )

    categories = set(case.get("categories") or [])
    if mode != "permission" and "dangerous_negative" in categories and not (
        set(forbidden_tools) & WRITE_TOOLS
    ):
        errors.append(
            f"{case_id}: dangerous_negative must forbid a write tool"
        )

    order_assertions = expected_arguments.get("order_lookup", {})
    order_id = order_assertions.get("order_id")
    if order_id and str(order_id) not in order_ids and "not_found" not in categories:
        errors.append(f"{case_id}: order fixture does not exist: {order_id}")

    return errors


def validate_permission_matrix(
    cases: list[dict[str, Any]],
    registered_tools: set[str],
) -> list[str]:
    errors: list[str] = []
    expected_denied = {
        (agent_key, tool_name)
        for agent_key, allowed_tools in AGENT_TOOL_PERMISSIONS.items()
        for tool_name in registered_tools
        if tool_name not in allowed_tools
    }
    actual_pairs = [
        (case.get("agent_key"), case.get("tool_name"))
        for case in cases
        if case.get("mode") == "permission"
    ]
    actual_counts = Counter(actual_pairs)
    duplicate_pairs = sorted(
        pair for pair, count in actual_counts.items() if count > 1
    )
    if duplicate_pairs:
        errors.append(f"duplicate permission pairs: {duplicate_pairs}")

    actual_denied = set(actual_pairs)
    missing = sorted(expected_denied - actual_denied)
    extra = sorted(actual_denied - expected_denied)
    if missing:
        errors.append(f"missing permission-denial pairs: {missing}")
    if extra:
        errors.append(f"unexpected permission pairs: {extra}")

    return errors


def validate_contrast_groups(cases: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for case in cases:
        group = case.get("contrast_group")
        if group:
            groups[group].append(case)

    for group, group_cases in groups.items():
        if len(group_cases) != 2:
            errors.append(f"contrast group {group} must contain exactly two cases")
        roles = [case.get("contrast_role") for case in group_cases]
        if any(not role for role in roles) or len(set(roles)) != len(roles):
            errors.append(f"contrast group {group} must have distinct roles")

    return errors


def main() -> None:
    cases, errors = load_jsonl(EVAL_PATH)
    contract = tool_contract()
    order_ids = {
        str(order["order_id"])
        for order in json.loads(ORDERS_PATH.read_text(encoding="utf-8"))
    }

    add_duplicate_errors(cases, "id", errors)
    add_duplicate_errors(cases, "query", errors)
    for case in cases:
        errors.extend(validate_case(case, contract, order_ids))
    errors.extend(validate_permission_matrix(cases, set(contract)))
    errors.extend(validate_contrast_groups(cases))

    category_counts = Counter(
        category
        for case in cases
        for category in case.get("categories", [])
    )
    plan_only_count = sum(
        case.get("mode") == "plan_only"
        for case in cases
    )
    permission_count = sum(
        case.get("mode") == "permission"
        for case in cases
    )
    report = {
        "dataset": str(EVAL_PATH),
        "total_cases": len(cases),
        "registered_tools": sorted(contract),
        "permission_denial_cases": permission_count,
        "plan_only_cases": plan_only_count,
        "category_counts": dict(sorted(category_counts.items())),
        "duplicate_ids": sum("duplicate id" in error for error in errors),
        "duplicate_queries": sum("duplicate query" in error for error in errors),
        "errors": errors,
        "passed": not errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
