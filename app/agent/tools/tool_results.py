from app.core.schemas import ToolResult


def get_tool_result(tool_results: list[ToolResult], tool_name: str) -> ToolResult | None:
    return next((item for item in tool_results if item.tool_name == tool_name), None)


def get_order_lookup_result(tool_results: list[ToolResult]) -> ToolResult | None:
    return get_tool_result(tool_results, "order_lookup")


def has_failed_order_lookup(tool_results: list[ToolResult]) -> bool:
    order_result = get_order_lookup_result(tool_results)

    return bool(order_result and not order_result.success)


def has_failed_policy_search(tool_results: list[ToolResult]) -> bool:
    policy_result = get_tool_result(tool_results, "policy_search")

    return bool(policy_result and not policy_result.success)


def is_system_tool_failure(tool_result: ToolResult | None) -> bool:
    if not tool_result or tool_result.success:
        return False

    return isinstance(tool_result.result, dict) and bool(tool_result.result.get("error_type"))


def is_low_confidence_evidence(tool_result: ToolResult | None) -> bool:
    if not tool_result or tool_result.success or not isinstance(tool_result.result, dict):
        return False

    return tool_result.result.get("error_type") == "LowConfidenceEvidence"


def has_failed_tool_call(tool_results: list[ToolResult]) -> bool:
    return any(
        not item.success
        and item.tool_name != "ticket_decision"
        for item in tool_results
    )
