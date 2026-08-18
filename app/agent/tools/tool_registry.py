from copy import deepcopy
from typing import Any

from app.core.schemas import ToolResult
from app.tools.support_tools import (
    create_manual_review,
    create_ticket,
    get_quick_reply,
    get_shop_products,
    order_lookup,
    policy_search,
    refund_apply,
    risk_check,
    send_goods_link,
    transfer_to_human,
)


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
            "name": "risk_check",
            "description": "调用风控 Agent 检测高频退款、异常账号、恶意投诉和虚假描述。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "关联订单号。",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "用户原始售后诉求。",
                    },
                },
                "required": ["order_id", "user_request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refund_apply",
            "description": "创建退款申请并投递 MQ，由退款处理服务异步更新订单状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "关联订单号。",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "用户原始退款诉求。",
                    },
                    "risk_assessment": {
                        "type": "object",
                        "description": "风控 Agent 输出的风险评估。",
                    },
                },
                "required": ["order_id", "user_request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_manual_review",
            "description": "创建人工审核单，处理大额退款、异常账号和投诉升级等高风险售后动作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "关联订单号。",
                    },
                    "review_type": {
                        "type": "string",
                        "description": "审核类型，如 refund、complaint、risk_control。",
                    },
                    "risk_level": {
                        "type": "string",
                        "description": "风控等级。",
                    },
                    "risk_flags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "触发的风险原因。",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "用户原始诉求。",
                    },
                    "related_id": {
                        "type": "string",
                        "description": "关联退款申请号或工单号。",
                    },
                },
                "required": ["review_type", "risk_level", "risk_flags", "user_request"],
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
    {
        "type": "function",
        "function": {
            "name": "get_shop_products",
            "description": "查询店铺商品列表，用于商品推荐、缺货替代推荐和客服主动推荐。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用户描述的商品需求或商品关键词。",
                    },
                    "platform": {
                        "type": "string",
                        "description": "电商平台，如 pinduoduo、taobao、jd、douyin。",
                    },
                    "in_stock_only": {
                        "type": "boolean",
                        "description": "是否只返回有库存商品。",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回商品数量。",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_goods_link",
            "description": "生成商品卡片发送结果；演示环境不会真实发送平台消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "要发送商品卡片的商品 ID。",
                    },
                    "platform": {
                        "type": "string",
                        "description": "目标电商平台。",
                    },
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_quick_reply",
            "description": "按业务意图获取客服工作台快捷回复模板。",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "快捷回复意图，如 payment_invoice、handoff、missing_order_id。",
                    },
                    "platform": {
                        "type": "string",
                        "description": "目标电商平台。",
                    },
                },
                "required": ["intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "生成转人工交接单，用于用户明确要求人工或高风险/缺货等需要人工接管的场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "转人工原因。",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "用户原始诉求。",
                    },
                    "priority": {
                        "type": "string",
                        "description": "交接优先级。",
                        "default": "normal",
                    },
                },
                "required": ["reason", "user_request"],
            },
        },
    },
]


TOOL_HANDLERS = {
    "order_lookup": order_lookup,
    "policy_search": policy_search,
    "risk_check": risk_check,
    "refund_apply": refund_apply,
    "create_manual_review": create_manual_review,
    "create_ticket": create_ticket,
    "get_shop_products": get_shop_products,
    "send_goods_link": send_goods_link,
    "get_quick_reply": get_quick_reply,
    "transfer_to_human": transfer_to_human,
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
