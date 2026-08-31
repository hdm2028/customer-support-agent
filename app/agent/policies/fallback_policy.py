from app.agent.tools.tool_results import (
    get_tool_result,
    is_low_confidence_evidence,
    is_system_tool_failure,
)


ORDER_RELATED_KEYWORDS = [
    "订单",
    "不想要",
    "不要了",
    "物流",
    "发货",
    "签收",
    "退款",
    "退钱",
    "退货",
    "换货",
    "保修",
    "维修",
    "检测",
    "发票",
    "取消",
    "地址",
    "投诉",
    "坏了",
    "故障",
    "质量问题",
    "换新",
    "售后",
    "无法使用",
    "不能用",
]

ORDER_ID_REQUIRED_KEYWORDS = [
    "我的订单",
    "订单",
    "不想要",
    "不要了",
    "物流",
    "发货",
    "签收",
    "退款",
    "退货",
    "赔付",
    "换货",
    "换新",
    "保修",
    "维修",
    "检测",
    "坏了",
    "故障",
    "质量问题",
    "售后",
    "无法使用",
    "不能用",
    "退货仓库",
    "支付异常",
    "扣款",
    "发票",
    "投诉",
    "改收货地址",
    "改地址",
    "修改地址",
]

GENERAL_POLICY_LOOKUP_KEYWORDS = [
    "售后规则",
    "退款政策",
    "退换货政策",
    "物流规则",
    "客服SOP",
    "客服 SOP",
    "在哪里看",
]

RISKY_OPERATION_KEYWORDS = [
    "直接退款",
    "马上退款",
    "立即退款",
    "直接赔付",
    "马上赔付",
    "取消订单",
    "修改地址",
    "改地址",
    "改收货地址",
    "发优惠券",
    "补偿",
]

RISKY_ACTION_KEYWORDS = [
    "退款",
    "赔付",
    "取消订单",
    "修改地址",
    "改地址",
    "改收货地址",
]

RISKY_BYPASS_KEYWORDS = [
    "不要审核",
    "不用审核",
    "跳过审核",
    "绕过审核",
    "跳过检测",
    "不用检测",
    "直接",
]


def is_order_related(message: str) -> bool:
    """判断问题是否和具体订单有关。"""

    return any(keyword in message for keyword in ORDER_RELATED_KEYWORDS)


def requires_order_id(message: str) -> bool:
    """判断当前问题是否必须依赖具体订单号才能继续处理。"""

    if (
        any(keyword in message for keyword in GENERAL_POLICY_LOOKUP_KEYWORDS)
        and "订单" not in message
        and "我的" not in message
        and "我买" not in message
    ):
        return False

    return any(keyword in message for keyword in ORDER_ID_REQUIRED_KEYWORDS)


def is_risky_operation(message: str) -> bool:
    """判断用户是否请求高风险业务操作。"""

    if any(keyword in message for keyword in RISKY_OPERATION_KEYWORDS):
        return True

    has_risky_action = any(keyword in message for keyword in RISKY_ACTION_KEYWORDS)
    has_bypass_intent = any(keyword in message for keyword in RISKY_BYPASS_KEYWORDS)

    return has_risky_action and has_bypass_intent


def should_ask_order_id(message: str, order_id: str | None) -> bool:
    """如果问题必须查具体订单，但没有订单号，就应该先追问订单号。"""

    return requires_order_id(message) and order_id is None


def should_handoff_to_human(message: str) -> tuple[bool, str | None]:
    """判断是否需要转人工。"""

    if is_risky_operation(message):
        return True, "该请求涉及退款、赔付、取消订单、修改地址等高风险操作，需要人工客服审核。"

    return False, None


def build_fallback_answer(route, tool_results: list) -> str:
    if route.need_clarification:
        reply = route.clarification_question or "请您补充订单号后，我再帮您继续处理。"

        if route.handoff_required and route.handoff_reason:
            reply += route.handoff_reason

        return reply

    if route.handoff_required and not route.order_id:
        return route.handoff_reason or "该问题需要人工客服进一步处理。"

    if route.blocked_by_guardrail:
        return route.guardrail_reason or "当前请求存在安全风险，已拒绝执行。"

    order_result = get_tool_result(tool_results, "order_lookup")

    if order_result and not order_result.success:
        return (
            f"{order_result.result} 请您核对订单号后重新提供，"
            "我再继续查询售后政策并判断是否需要创建工单。"
        )

    policy_result = get_tool_result(tool_results, "policy_search")
    ticket_decision_result = get_tool_result(tool_results, "ticket_decision")
    ticket_result = get_tool_result(tool_results, "create_ticket")
    risk_result = get_tool_result(tool_results, "risk_check")
    refund_result = get_tool_result(tool_results, "refund_apply")
    manual_review_result = get_tool_result(tool_results, "create_manual_review")
    handoff_result = get_tool_result(tool_results, "transfer_to_human")
    plan_validation_result = get_tool_result(tool_results, "tool_plan_validation")
    chain_validation_result = get_tool_result(tool_results, "tool_chain_validation")

    parts = []

    if plan_validation_result and not plan_validation_result.success:
        return (
            "本轮工具调用计划没有通过校验，我不会继续执行可能错误的自动操作。"
            "请您补充订单号和具体售后诉求，或由人工客服继续处理。"
        )

    if order_result and order_result.success:
        order = order_result.result
        parts.append(
            f"已查询到订单 {order.get('order_id')}，商品是 {order.get('product_name')}，"
            f"当前订单状态为{order.get('order_status')}。"
        )

    if policy_result and not policy_result.success:
        if is_low_confidence_evidence(policy_result):
            parts.append(
                "但本轮没有检索到足够匹配的售后政策证据，我不能强行判断或创建工单。"
                "建议补充问题细节，或转人工客服核对政策后继续处理。"
            )
        elif is_system_tool_failure(policy_result):
            parts.append(
                "但本轮售后政策检索工具调用失败，我不能在缺少政策依据时直接判断或创建工单。"
                "建议转人工客服核对政策后继续处理。"
            )
        else:
            parts.append(
                "但本轮没有检索到足够匹配的售后政策，我不能编造不存在的政策结论。"
                "建议补充问题细节或转人工客服确认。"
            )

    if policy_result and policy_result.success:
        first_policy = policy_result.result[0]
        citation = first_policy.get("citation") or first_policy.get("source")
        parts.append(f"根据知识库来源《{citation}》，本问题需要结合售后政策进一步判断。")

    if risk_result and risk_result.success:
        risk = risk_result.result
        if risk.get("risk_level") in {"medium", "high"}:
            flags = "、".join(risk.get("risk_flags", [])) or "售后风险"
            parts.append(f"风控 Agent 判定风险等级为{risk.get('risk_level')}，命中原因：{flags}。")

    if refund_result and not refund_result.success:
        result = refund_result.result
        reason = result.get("reason") if isinstance(result, dict) else str(result)
        parts.append(f"退款申请暂未创建成功：{reason}")

    if refund_result and refund_result.success:
        refund = refund_result.result
        if refund.get("status") == "pending_manual_review":
            parts.append(
                f"已创建退款申请 {refund.get('refund_id')}，当前状态为待人工审核。"
            )
        else:
            parts.append(
                f"已创建退款申请 {refund.get('refund_id')}，并投递 MQ 消息 "
                f"{refund.get('mq_message_id')}，等待退款处理服务异步处理。"
            )

    if manual_review_result and manual_review_result.success:
        review = manual_review_result.result
        parts.append(
            f"已创建人工审核单 {review.get('review_id')}，后续由人工客服复核后再执行高风险操作。"
        )

    if ticket_result and not ticket_result.success:
        if is_system_tool_failure(ticket_result):
            parts.append(
                "工单创建工具本轮调用失败，暂时没有生成工单。"
                "建议稍后重试，或由人工客服继续接入处理。"
            )
        else:
            parts.append(f"工单暂未创建成功：{ticket_result.result}")

    if ticket_result and ticket_result.success:
        ticket = ticket_result.result
        parts.append(
            f"我已生成{ticket['issue_type']}工单草稿，后续需要人工客服核对订单和凭证后处理。"
        )

    if handoff_result and handoff_result.success:
        handoff = handoff_result.result
        parts.append(
            f"已生成转人工交接：{handoff.get('handoff_summary')} "
            "后续请人工客服继续处理。"
        )

    if chain_validation_result and not chain_validation_result.success:
        parts.append(
            "另外，本轮工具执行链路没有通过一致性校验，我不会继续扩大自动处理范围。"
            "建议转人工客服复核。"
        )

    if ticket_decision_result and not ticket_decision_result.success:
        reason = ticket_decision_result.result.get("reason", "当前订单状态暂不满足创建工单条件。")
        parts.append(f"根据订单状态，当前暂不创建工单：{reason}")

    if not parts:
        return "您好，我暂时没有找到足够信息。请补充订单号和具体售后问题，我再帮您判断。"

    return "".join(parts)
