from app.agent.tools.tool_results import (
    get_tool_result,
    is_low_confidence_evidence,
    is_system_tool_failure,
)
from app.agent.routing.refund_withdrawal import WITHDRAWAL_GUIDANCE, WITHDRAWAL_TOPIC
from app.agent.routing.pending_task import can_retrieve_policy_while_clarifying


def _build_policy_answer(policy_result) -> str:
    from app.agent.response.grounded import policy_excerpts, render_grounded_reply
    return render_grounded_reply([policy_result], policy_excerpts([policy_result]))


def refund_status_message(refund: dict) -> str:
    """Describe the observed state, including replay, without claiming settlement."""
    prefix = f"退款申请 {refund.get('refund_id')}"
    messages = {
        "queued": "已受理，正在等待处理。申请受理不代表资金已经退回。",
        "pending_manual_review": "当前状态为待人工审核。",
        "refund_processing": "正在处理中，尚未确认处理完成。",
        "payment_submitting": "已提交支付渠道处理，尚未确认结果。",
        "refund_unknown": "支付处理结果待核实，需先查询实际状态。",
        "refund_succeeded": "已由支付渠道确认处理成功，请以原支付渠道到账记录为准。",
        "failed": "处理失败，请查看失败原因或联系人工客服。",
        "rejected": "审核未通过，请查看审核说明。",
        "cancelled": "已取消。",
        "canceled": "已取消。",
    }
    return prefix + messages.get(refund.get("status"), "当前处理结果需要进一步核实，请查询详情或联系人工客服。")


def refund_outcome_reply(tool_results: list) -> str | None:
    """Execution outcomes come from tool state, never from retrieved policy prose."""
    result = get_tool_result(tool_results, "refund_apply")
    if result is None:
        return None
    if result.success and isinstance(result.result, dict):
        parts = [refund_status_message(result.result)]
    elif is_system_tool_failure(result) and result.result.get("failure_origin") != "tool_result":
        parts = ["本轮未能确认退款申请处理结果，请查询已有申请或联系人工核实，避免重复提交。"]
    else:
        reason = result.result.get("reason") if isinstance(result.result, dict) else str(result.result)
        parts = [f"退款申请暂未创建成功：{reason or '当前结果需要进一步核实。'}"]
    order = get_tool_result(tool_results, "order_lookup")
    if order and order.success and isinstance(order.result, dict):
        parts.append(f"关联订单：{order.result.get('order_id')}。")
    review = get_tool_result(tool_results, "create_manual_review")
    if review and review.success and isinstance(review.result, dict):
        parts.append(f"关联人工审核单：{review.result.get('review_id')}，请以审核单的实际处理状态为准。")
    ticket = get_tool_result(tool_results, "create_ticket")
    if ticket and ticket.success and isinstance(ticket.result, dict):
        status = ticket.result.get("status")
        label = {"pending_manual_review": "待人工审核", "pending_review": "待人工审核",
                 "pending_human_review": "工单草稿，待人工审核",
                 "draft": "草稿", "draft_ready": "草稿", "resolved": "已记录处理结果",
                 "cancelled": "已取消"}.get(status, "请查看详情核实处理进度")
        parts.append(f"关联工单：{ticket.result.get('ticket_id')}，{label}。")
    return "".join(parts)


def build_fallback_answer(route, tool_results: list) -> str:
    if route.topic == WITHDRAWAL_TOPIC:
        return WITHDRAWAL_GUIDANCE

    if route.need_clarification:
        if can_retrieve_policy_while_clarifying(route):
            policy = get_tool_result(tool_results, "policy_search")
            if policy:
                explanation = (_build_policy_answer(policy) if policy.success else
                               "本轮未取得可用的退款政策证据，不能据此判断退款资格。")
                return explanation + "\n\n尚未核实具体订单的退款资格；如需核实您的订单，请提供订单号。"
        reply = (
            route.clarification_question
            or "请您补充订单号后，我再帮您继续处理。"
        )

        if route.handoff_required and route.handoff_reason:
            reply += route.handoff_reason

        return reply

    if route.handoff_required and not route.order_id:
        handoff = get_tool_result(tool_results, "transfer_to_human")
        if handoff and handoff.success:
            from app.agent.response.grounded import handoff_receipt
            return handoff_receipt(handoff.result)
        if handoff and not handoff.success:
            return "本轮未能确认人工工单已创建，请查询已有工单或稍后重试。"
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

    refund_reply = refund_outcome_reply(tool_results)
    if refund_reply is not None:
        return refund_reply

    refund_decision = get_tool_result(tool_results, "refund_decision")
    if (refund_decision and refund_decision.success and isinstance(refund_decision.result, dict)
            and refund_decision.result.get("can_create") is False):
        decision = refund_decision.result
        if decision.get("existing_refund"):
            reply = refund_status_message(decision["existing_refund"]) + "本轮未重复创建退款申请、审核单或工单，可在退款申请详情查看处理进度。"
        elif decision.get("existing_review"):
            review = decision["existing_review"]
            label = {"pending_review": "待人工审核", "approved": "已通过", "rejected": "已拒绝", "cancelled": "已取消"}.get(review["status"], "请查看详情核实")
            reply = (f"退款审核单 {review['review_id']} 已存在，当前状态：{label}。"
                     "本轮未重复创建审核单或人工工单。")
            if review["status"] == "pending_review":
                reply += "审核通过前不会自动执行退款。"
        elif decision.get("kind") == "order_after_sales":
            reply = (f"订单 {decision['order_id']} 当前状态为{decision['order_status']}。{decision['reason']}"
                     "本轮未查到有效退款申请记录，不能将订单售后状态当成已创建退款申请的凭证。"
                     "本轮未新建申请；请查询现有售后进度，或联系人工核实关联记录。")
        else:
            reply = f"订单 {decision['order_id']}：{decision['reason']}本轮未创建退款申请或人工审核单。"
        if decision.get("supplement_recorded"):
            reply += "本轮补充说明已保存；补充材料不会自动改变已有审核决定。"
        policy = get_tool_result(tool_results, "policy_search")
        if policy and not policy.success:
            reply += "本轮政策证据未能取得，以上状态判断依据查询到的业务记录。"
        return reply

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

    if (
        manual_review_result
        and manual_review_result.success
    ):
        review = manual_review_result.result

        if isinstance(review, dict):
            parts.append(
                f"已创建人工审核单 "
                f"{review.get('review_id')}，"
                "后续由人工客服复核后继续处理。"
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
            from app.agent.response.grounded import handoff_receipt
            parts.append(handoff_receipt(handoff))

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
