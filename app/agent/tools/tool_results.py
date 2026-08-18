from app.core.schemas import ToolResult


def get_tool_result(tool_results: list[ToolResult], tool_name: str) -> ToolResult | None:
    """按工具名获取工具结果。"""

    return next((item for item in tool_results if item.tool_name == tool_name), None)


def get_order_lookup_result(tool_results: list[ToolResult]) -> ToolResult | None:
    """从工具结果中取出订单查询结果。"""

    return get_tool_result(tool_results, "order_lookup")


def has_failed_order_lookup(tool_results: list[ToolResult]) -> bool:
    """判断订单查询是否失败。"""

    order_result = get_order_lookup_result(tool_results)

    return bool(order_result and not order_result.success)


def has_failed_policy_search(tool_results: list[ToolResult]) -> bool:
    """判断政策检索是否失败。"""

    policy_result = get_tool_result(tool_results, "policy_search")

    return bool(policy_result and not policy_result.success)


def is_system_tool_failure(tool_result: ToolResult | None) -> bool:
    """判断工具失败是否属于系统异常，而不是正常业务拒绝。"""

    if not tool_result or tool_result.success:
        return False

    return isinstance(tool_result.result, dict) and bool(tool_result.result.get("error_type"))


def is_low_confidence_evidence(tool_result: ToolResult | None) -> bool:
    """判断 RAG 失败是否来自证据低置信或意图不匹配。"""

    if not tool_result or tool_result.success or not isinstance(tool_result.result, dict):
        return False

    return tool_result.result.get("error_type") == "LowConfidenceEvidence"


def has_failed_tool_call(tool_results: list[ToolResult]) -> bool:
    """判断是否存在需要强制规则回复的工具失败。"""

    return any(
        not item.success
        and item.tool_name != "ticket_decision"
        for item in tool_results
    )
