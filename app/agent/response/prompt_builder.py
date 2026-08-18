from app.agent.tools.tool_results import get_tool_result
from app.core.schemas import ToolResult


def build_order_context(tool_results: list[ToolResult]) -> str:
    """把订单查询结果整理成大模型容易理解的业务上下文。"""

    order_result = get_tool_result(tool_results, "order_lookup")

    if not order_result:
        return ""

    if not order_result.success:
        return f"[订单信息]\n查询失败：{order_result.result}"

    order = order_result.result

    return "\n".join(
        [
            "[订单信息]",
            f"订单号：{order.get('order_id')}",
            f"商品名称：{order.get('product_name')}",
            f"订单状态：{order.get('order_status')}",
            f"物流状态：{order.get('shipping_status')}",
            f"签收日期：{order.get('signed_date')}",
            f"保修月数：{order.get('warranty_months')}",
            f"七天无理由时限：{order.get('return_window_days')} 天",
            f"是否可直接退款：{order.get('can_refund_directly')}",
            f"备注：{order.get('notes')}",
        ]
    )


def build_policy_evidence(tool_results: list[ToolResult]) -> str:
    """把 RAG 检索结果整理成 evidence context，减少模型读错或漏读证据。"""

    policy_result = get_tool_result(tool_results, "policy_search")

    if not policy_result:
        return ""

    if not policy_result.success:
        return f"[售后政策证据]\n检索失败：{policy_result.result}"

    lines = ["[售后政策证据]"]

    for index, item in enumerate(policy_result.result, start=1):
        citation = item.get("citation") or item.get("source") or "未知来源"
        score = item.get("score", "未知")
        text = item.get("text", "").strip()

        lines.extend(
            [
                f"证据 {index}",
                f"来源：{citation}",
                f"相关分数：{score}",
                "内容：",
                text,
                "",
            ]
        )

    return "\n".join(lines).strip()


def build_ticket_context(tool_results: list[ToolResult]) -> str:
    """把工单工具结果整理成客服后续动作说明。"""

    ticket_result = get_tool_result(tool_results, "create_ticket")

    if not ticket_result:
        return ""

    if not ticket_result.success:
        return f"[工单信息]\n创建失败：{ticket_result.result}"

    ticket = ticket_result.result

    return "\n".join(
        [
            "[工单信息]",
            f"工单状态：{ticket.get('status')}",
            f"风险提示：{ticket.get('risk_notice')}",
            f"关联订单：{ticket.get('order_id')}",
            f"问题类型：{ticket.get('issue_type')}",
            f"优先级：{ticket.get('priority')}",
            f"用户诉求：{ticket.get('user_request')}",
            f"下一步：{ticket.get('next_step')}",
        ]
    )


def build_after_sales_context(tool_results: list[ToolResult]) -> str:
    """整理退款申请、风控和人工审核上下文。"""

    lines = []
    risk_result = get_tool_result(tool_results, "risk_check")
    refund_result = get_tool_result(tool_results, "refund_apply")
    review_result = get_tool_result(tool_results, "create_manual_review")

    if risk_result:
        lines.append("[风控结果]")
        lines.append(str(risk_result.result))

    if refund_result:
        lines.append("[退款申请]")
        lines.append(str(refund_result.result))

    if review_result:
        lines.append("[人工审核]")
        lines.append(str(review_result.result))

    return "\n".join(lines)


def build_workbench_context(tool_results: list[ToolResult]) -> str:
    """整理商品推荐、快捷回复和转人工等工作台工具结果。"""

    lines = []
    product_result = get_tool_result(tool_results, "get_shop_products")
    goods_link_result = get_tool_result(tool_results, "send_goods_link")
    quick_reply_result = get_tool_result(tool_results, "get_quick_reply")
    handoff_result = get_tool_result(tool_results, "transfer_to_human")

    if product_result:
        lines.append("[商品推荐]")
        if product_result.success:
            for product in product_result.result:
                lines.append(
                    f"- {product.get('title')}，价格 {product.get('price')} 元，"
                    f"库存 {product.get('stock')}，卖点：{'；'.join(product.get('selling_points', [])[:2])}"
                )
        else:
            lines.append(f"查询失败：{product_result.result}")

    if goods_link_result:
        lines.append("[商品卡片]")
        lines.append(str(goods_link_result.result))

    if quick_reply_result:
        lines.append("[快捷回复]")
        lines.append(str(quick_reply_result.result))

    if handoff_result:
        lines.append("[转人工交接]")
        lines.append(str(handoff_result.result))

    return "\n".join(lines)


def build_tool_context(tool_results: list[ToolResult]) -> str:
    """统一组装工具上下文，避免把原始 JSON 直接塞给大模型。"""

    context_parts = [
        build_order_context(tool_results),
        build_policy_evidence(tool_results),
        build_after_sales_context(tool_results),
        build_ticket_context(tool_results),
        build_workbench_context(tool_results),
    ]
    context_parts = [part for part in context_parts if part]

    if not context_parts:
        return "本轮没有调用工具。"

    return "\n\n==========\n\n".join(context_parts)


def build_model_messages(
    user_message: str,
    history: list[dict],
    tool_results: list[ToolResult],
) -> list[dict]:
    """把历史消息和工具结果整理成大模型 messages。"""

    tool_context = build_tool_context(tool_results)

    system_prompt = (
        "你是中文电商平台的智能售后客服 Agent。"
        "你必须根据订单信息、售后政策、工单结果回答用户。"
        "不要编造工具结果里不存在的信息。"
        "涉及退款、赔付、取消订单、修改地址等高风险操作时，只能按工具结果说明申请、MQ 异步处理或人工审核状态，不能承诺已经完成。"
        "如果信息不足，要明确告诉用户还需要补充什么。"
        "如果工具生成了商品卡片或快捷回复，要把这些工作台结果自然告知用户。"
    )

    user_prompt = (
        f"用户当前问题：\n{user_message}\n\n"
        f"工具执行结果：\n{tool_context}\n\n"
        "请生成客服回复，要求：\n"
        "1. 先直接回答用户最关心的问题。\n"
        "2. 说明依据了哪些订单信息或政策。\n"
        "3. 如果使用了售后政策证据，必须引用对应来源。\n"
        "4. 如果生成了工单，告诉用户后续需要人工审核。\n"
        "5. 如果证据没有覆盖用户问题，要明确说明资料不足，不能编造。\n"
        "6. 语气礼貌、清楚、不要夸大承诺。"
    )

    return [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_prompt},
    ]
