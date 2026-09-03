from app.agent.tools.tool_results import (
    get_tool_result,
    is_low_confidence_evidence,
    is_system_tool_failure,
)


def _extract_policy_text(policy_item: dict) -> str:
    """从 policy_search 单条结果中提取可用于回答的正文内容。"""

    if not isinstance(policy_item, dict):
        return str(policy_item)

    candidates = (
        "content",
        "text",
        "chunk",
        "document",
        "body",
        "answer",
        "summary",
    )

    for key in candidates:
        value = policy_item.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    metadata = policy_item.get("metadata")

    if isinstance(metadata, dict):
        for key in candidates:
            value = metadata.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def _extract_policy_citation(policy_item: dict) -> str:
    """从 policy_search 单条结果中提取来源信息。"""

    if not isinstance(policy_item, dict):
        return ""

    citation = (
        policy_item.get("citation")
        or policy_item.get("source")
        or policy_item.get("title")
    )

    if isinstance(citation, str) and citation.strip():
        return citation.strip()

    metadata = policy_item.get("metadata")

    if isinstance(metadata, dict):
        citation = (
            metadata.get("citation")
            or metadata.get("source")
            or metadata.get("title")
        )

        if isinstance(citation, str) and citation.strip():
            return citation.strip()

    return ""


def _build_policy_answer(policy_result) -> str:
    """把 policy_search 的检索结果转换成确定性回答。"""

    result = policy_result.result

    if not isinstance(result, list) or not result:
        return (
            "已检索到售后政策，但当前没有可直接展示的政策正文，"
            "建议补充具体问题后继续判断。"
        )

    policy_parts = []

    # fallback 回复不需要把所有召回 chunk 全塞给用户，
    # 取前两条高相关结果即可。
    for item in result[:2]:
        citation = _extract_policy_citation(item)
        text = _extract_policy_text(item)

        if citation and text:
            policy_parts.append(
                f"根据知识库来源《{citation}》：{text}"
            )
        elif text:
            policy_parts.append(text)
        elif citation:
            policy_parts.append(
                f"已检索到知识库来源《{citation}》，"
                "但该条结果没有返回可直接展示的正文。"
            )

    if policy_parts:
        return " ".join(policy_parts)

    return (
        "已检索到相关售后政策，但当前结果没有可直接展示的正文内容，"
        "建议补充问题细节后继续判断。"
    )


def build_fallback_answer(route, tool_results: list) -> str:
    if route.need_clarification:
        reply = (
            route.clarification_question
            or "请您补充订单号后，我再帮您继续处理。"
        )

        if route.handoff_required and route.handoff_reason:
            reply += route.handoff_reason

        return reply

    if route.handoff_required and not route.order_id:
        return (
            route.handoff_reason
            or "该问题需要人工客服进一步处理。"
        )

    if route.blocked_by_guardrail:
        return (
            route.guardrail_reason
            or "当前请求存在安全风险，已拒绝执行。"
        )

    order_result = get_tool_result(
        tool_results,
        "order_lookup",
    )

    if order_result and not order_result.success:
        return (
            f"{order_result.result} 请您核对订单号后重新提供，"
            "我再继续查询售后政策并判断是否需要创建工单。"
        )

    policy_result = get_tool_result(
        tool_results,
        "policy_search",
    )

    ticket_decision_result = get_tool_result(
        tool_results,
        "ticket_decision",
    )

    ticket_result = get_tool_result(
        tool_results,
        "create_ticket",
    )

    risk_result = get_tool_result(
        tool_results,
        "risk_check",
    )

    refund_result = get_tool_result(
        tool_results,
        "refund_apply",
    )

    manual_review_result = get_tool_result(
        tool_results,
        "create_manual_review",
    )

    handoff_result = get_tool_result(
        tool_results,
        "transfer_to_human",
    )

    plan_validation_result = get_tool_result(
        tool_results,
        "tool_plan_validation",
    )

    chain_validation_result = get_tool_result(
        tool_results,
        "tool_chain_validation",
    )

    parts = []

    if (
        plan_validation_result
        and not plan_validation_result.success
    ):
        return (
            "本轮工具调用计划没有通过校验，我不会继续执行可能错误的自动操作。"
            "请您补充订单号和具体售后诉求，或由人工客服继续处理。"
        )

    if order_result and order_result.success:
        order = order_result.result

        if isinstance(order, dict):
            parts.append(
                f"已查询到订单 {order.get('order_id')}，"
                f"商品是 {order.get('product_name')}，"
                f"当前订单状态为{order.get('order_status')}。"
            )

    if policy_result and not policy_result.success:
        if is_low_confidence_evidence(policy_result):
            parts.append(
                "但本轮没有检索到足够匹配的售后政策证据，"
                "我不能强行判断或创建工单。"
                "建议补充问题细节，或转人工客服核对政策后继续处理。"
            )

        elif is_system_tool_failure(policy_result):
            parts.append(
                "但本轮售后政策检索工具调用失败，"
                "我不能在缺少政策依据时直接判断或创建工单。"
                "建议转人工客服核对政策后继续处理。"
            )

        else:
            parts.append(
                "但本轮没有检索到足够匹配的售后政策，"
                "我不能编造不存在的政策结论。"
                "建议补充问题细节或转人工客服确认。"
            )

    if policy_result and policy_result.success:
        parts.append(
            _build_policy_answer(policy_result)
        )

    if risk_result and risk_result.success:
        risk = risk_result.result

        if isinstance(risk, dict):
            if risk.get("risk_level") in {
                "medium",
                "high",
            }:
                flags = (
                    "、".join(
                        risk.get(
                            "risk_flags",
                            [],
                        )
                    )
                    or "售后风险"
                )

                parts.append(
                    f"风控 Agent 判定风险等级为"
                    f"{risk.get('risk_level')}，"
                    f"命中原因：{flags}。"
                )

    if refund_result and not refund_result.success:
        result = refund_result.result

        reason = (
            result.get("reason")
            if isinstance(result, dict)
            else str(result)
        )

        parts.append(
            f"退款申请暂未创建成功：{reason}"
        )

    if refund_result and refund_result.success:
        refund = refund_result.result

        if isinstance(refund, dict):
            if (
                refund.get("status")
                == "pending_manual_review"
            ):
                parts.append(
                    f"已创建退款申请 "
                    f"{refund.get('refund_id')}，"
                    "当前状态为待人工审核。"
                )

            else:
                parts.append(
                    f"已创建退款申请 "
                    f"{refund.get('refund_id')}，"
                    "并投递 MQ 消息 "
                    f"{refund.get('mq_message_id')}，"
                    "等待退款处理服务异步处理。"
                )

    if (
        manual_review_result
        and manual_review_result.success
    ):
        review = manual_review_result.result

        if isinstance(review, dict):
            parts.append(
                f"已创建人工审核单 "
                f"{review.get('review_id')}，"
                "后续由人工客服复核后再执行高风险操作。"
            )

    if ticket_result and not ticket_result.success:
        if is_system_tool_failure(ticket_result):
            parts.append(
                "工单创建工具本轮调用失败，暂时没有生成工单。"
                "建议稍后重试，或由人工客服继续接入处理。"
            )

        else:
            parts.append(
                f"工单暂未创建成功："
                f"{ticket_result.result}"
            )

    if ticket_result and ticket_result.success:
        ticket = ticket_result.result

        if isinstance(ticket, dict):
            parts.append(
                f"我已生成{ticket.get('issue_type')}工单草稿，"
                "后续需要人工客服核对订单和凭证后处理。"
            )

    if handoff_result and handoff_result.success:
        handoff = handoff_result.result

        if isinstance(handoff, dict):
            parts.append(
                f"已生成转人工交接："
                f"{handoff.get('handoff_summary')} "
                "后续请人工客服继续处理。"
            )

    if (
        chain_validation_result
        and not chain_validation_result.success
    ):
        parts.append(
            "另外，本轮工具执行链路没有通过一致性校验，"
            "我不会继续扩大自动处理范围。"
            "建议转人工客服复核。"
        )

    if (
        ticket_decision_result
        and not ticket_decision_result.success
    ):
        result = ticket_decision_result.result

        reason = (
            result.get(
                "reason",
                "当前订单状态暂不满足创建工单条件。",
            )
            if isinstance(result, dict)
            else str(result)
        )

        parts.append(
            f"根据订单状态，当前暂不创建工单：{reason}"
        )

    if not parts:
        return (
            "您好，我暂时没有找到足够信息。"
            "请补充订单号和具体售后问题，我再帮您判断。"
        )

    return "".join(parts)