from copy import deepcopy
from typing import Any

from app.core.schemas import ToolResult
from app.tools.support_tools import create_ticket, order_lookup, policy_search


FUNCTION_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "order_lookup",
            "description": "根据订单号查询订单状态、商品、物流和售后相关字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "用户提供的订单号。",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "policy_search",
            "description": "检索售后政策知识库，返回带 citation 的证据片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "融合用户问题和订单上下文后的检索问题。",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回的政策证据数量。",
                        "default": 2,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "创建待人工审核的售后工单草稿，不执行真实退款、赔付、取消或改地址。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "关联订单号。",
                    },
                    "issue_type": {
                        "type": "string",
                        "description": "售后问题类型。",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "用户原始诉求。",
                    },
                    "priority": {
                        "type": "string",
                        "description": "工单优先级。",
                        "default": "normal",
                    },
                },
                "required": ["order_id", "issue_type", "user_request"],
            },
        },
    },
]


TOOL_HANDLERS = {
    "order_lookup": order_lookup,
    "policy_search": policy_search,
    "create_ticket": create_ticket,
}


def get_function_tool_specs() -> list[dict[str, Any]]:
    """返回 Function Calling 风格的工具定义，供 API、评测和前端展示。"""

    return deepcopy(FUNCTION_TOOL_SPECS)


def get_required_arguments(tool_name: str) -> list[str]:
    """读取某个工具 schema 中声明的必填参数。"""

    for spec in FUNCTION_TOOL_SPECS:
        function = spec["function"]

        if function["name"] == tool_name:
            return list(function["parameters"].get("required", []))

    return []


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> tuple[bool, list[str]]:
    """执行工具前做最小参数校验，避免模型或路由层越权乱调。"""

    if tool_name not in TOOL_HANDLERS:
        return False, [f"未知工具：{tool_name}"]

    missing = [
        name for name in get_required_arguments(tool_name)
        if arguments.get(name) in (None, "")
    ]

    if missing:
        return False, [f"缺少必要参数：{', '.join(missing)}"]

    return True, []


def execute_registered_tool(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
    """只允许调用注册表中的工具，是本项目的受控 Function Calling 执行层。"""

    valid, errors = validate_tool_arguments(tool_name, arguments)

    if not valid:
        return ToolResult(
            tool_name=tool_name,
            success=False,
            result={
                "error_type": "InvalidToolArguments",
                "errors": errors,
                "fallback_action": "ask_user_or_handoff_to_human",
            },
        )

    handler = TOOL_HANDLERS[tool_name]
    result = handler(**arguments)

    if isinstance(result, ToolResult):
        return result

    return ToolResult(tool_name=tool_name, success=True, result=result)
