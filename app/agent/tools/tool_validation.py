from app.core.schemas import RouteDecision, ToolResult


def validate_tool_plan(route: RouteDecision) -> tuple[bool, list[str]]:
    errors = []

    if route.need_order and not route.order_id:
        errors.append("需要查询订单，但缺少订单号。")

    if route.need_ticket and not route.order_id:
        errors.append("创建工单必须先有订单号。")

    if route.need_ticket and not route.need_order:
        errors.append("创建工单前必须先执行订单查询。")

    if route.need_refund_request and not route.order_id:
        errors.append("创建退款申请必须先有订单号。")

    if route.need_refund_request and not route.need_order:
        errors.append("创建退款申请前必须先执行订单查询。")

    if route.need_refund_request and not route.need_risk_check:
        errors.append("创建退款申请前必须执行风控检测。")

    return len(errors) == 0, errors


def validate_tool_chain(route: RouteDecision, tool_results: list[ToolResult]) -> tuple[bool, list[str]]:
    errors = []
    tool_names = [
        item.tool_name
        for item in tool_results
    ]

    if route.need_order and route.order_id and "order_lookup" not in tool_names:
        errors.append("路由要求查询订单，但实际没有调用 order_lookup。")

    if "policy_search" in tool_names and "order_lookup" in tool_names:
        if tool_names.index("policy_search") < tool_names.index("order_lookup"):
            errors.append("policy_search 不能早于 order_lookup 执行。")

    if "create_ticket" in tool_names:
        if "order_lookup" not in tool_names:
            errors.append("create_ticket 前缺少 order_lookup。")

        if route.need_policy and "policy_search" not in tool_names:
            errors.append("create_ticket 前缺少 policy_search。")

        if "ticket_decision" in tool_names:
            errors.append("ticket_decision 拒绝后不应继续 create_ticket。")

    if "risk_check" in tool_names:
        if "order_lookup" not in tool_names:
            errors.append("risk_check 前缺少 order_lookup。")

        if tool_names.index("risk_check") < tool_names.index("order_lookup"):
            errors.append("risk_check 不能早于 order_lookup 执行。")

    if "refund_apply" in tool_names:
        if "order_lookup" not in tool_names:
            errors.append("refund_apply 前缺少 order_lookup。")

        if route.need_policy and "policy_search" not in tool_names:
            errors.append("refund_apply 前缺少 policy_search。")

        if "risk_check" not in tool_names:
            errors.append("refund_apply 前缺少 risk_check。")

        if tool_names.index("refund_apply") < tool_names.index("risk_check"):
            errors.append("refund_apply 不能早于 risk_check 执行。")

    if "create_manual_review" in tool_names and "risk_check" in tool_names:
        if tool_names.index("create_manual_review") < tool_names.index("risk_check"):
            errors.append("create_manual_review 不能早于 risk_check 执行。")

    order_result = next((item for item in tool_results if item.tool_name == "order_lookup"), None)
    if order_result and not order_result.success:
        downstream_tools = [
            name
            for name in tool_names
            if name in {"policy_search", "ticket_decision", "create_ticket"}
        ]
        if downstream_tools:
            errors.append(f"订单查询失败后不应继续执行下游工具：{downstream_tools}")

    policy_result = next((item for item in tool_results if item.tool_name == "policy_search"), None)
    if policy_result and not policy_result.success and "create_ticket" in tool_names:
        errors.append("政策检索失败后不应继续 create_ticket。")

    return len(errors) == 0, errors
