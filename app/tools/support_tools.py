from app.agent.orchestration.after_sales_agent import evaluate_refund_eligibility, infer_refund_reason
from app.concurrency.refund_guard import (
    build_idempotent_replay,
    cache_refund_idempotency,
    get_refund_idempotency,
    refund_distributed_lock,
    wait_for_refund_idempotency,
)
from app.agent.orchestration.risk_agent import evaluate_risk
from app.core.schemas import ToolResult
from app.mq.queue import REFUND_REQUESTED_TOPIC, publish_message
from app.rag.rag import search_documents
from app.storage.database import (
    get_customer_profile_from_db,
    save_manual_review_to_db,
    save_refund_request_to_db,
    save_ticket_to_db,
    update_refund_request_in_db,
)
from app.storage.store import get_order_by_id


def order_lookup(order_id: str) -> ToolResult:
    order = get_order_by_id(order_id)

    if not order:
        return ToolResult(
            tool_name="order_lookup",
            success=False,
            result=f"未找到订单号 {order_id}，请核对订单号是否正确。",
        )

    return ToolResult(
        tool_name="order_lookup",
        success=True,
        result=order,
    )


def policy_search(query: str, top_k: int = 2) -> ToolResult:
    results = search_documents(query, top_k=top_k)

    if not results:
        return ToolResult(
            tool_name="policy_search",
            success=False,
            result="未找到匹配的售后知识，请重新描述问题。",
        )

    return ToolResult(
        tool_name="policy_search",
        success=True,
        result=results,
    )


def risk_check(order_id: str, user_request: str) -> ToolResult:
    """调用风控 Agent：识别高频退款、异常账号、恶意投诉和虚假描述。"""

    order = get_order_by_id(order_id)

    if not order:
        return ToolResult(
            tool_name="risk_check",
            success=False,
            result=f"未找到订单号 {order_id}，无法进行风控判断。",
        )

    profile = get_customer_profile_from_db(order.get("user_id"))
    assessment = evaluate_risk(
        order=order,
        user_request=user_request,
        profile=profile,
    )

    return ToolResult(
        tool_name="risk_check",
        success=True,
        result=assessment,
    )


def _create_refund_request_unlocked(
    order_id: str,
    user_request: str,
    risk_assessment: dict | None = None,
) -> ToolResult:
    """创建退款申请并投递 MQ，由退款处理服务异步更新订单状态。"""

    order = get_order_by_id(order_id)

    if not order:
        return ToolResult(
            tool_name="refund_apply",
            success=False,
            result={
                "reason": f"未找到订单号 {order_id}，无法创建退款申请。",
                "fallback_action": "ask_user_to_check_order_id",
            },
        )

    assessment = risk_assessment or evaluate_risk(order, user_request)
    eligibility = evaluate_refund_eligibility(
        order=order,
        user_request=user_request,
        risk_assessment=assessment,
    )

    if not eligibility["eligible"]:
        return ToolResult(
            tool_name="refund_apply",
            success=False,
            result={
                "reason": eligibility["reason"],
                "refund_reason": eligibility["refund_reason"],
                "review_required": eligibility["review_required"],
                "fallback_action": "create_manual_review_or_explain_policy",
            },
        )

    status = "pending_manual_review" if eligibility["review_required"] else "queued"
    refund_request = save_refund_request_to_db(
        {
            "order_id": order_id,
            "user_id": order.get("user_id"),
            "amount": float(order.get("amount") or 0),
            "reason": infer_refund_reason(user_request),
            "status": status,
            "risk_level": assessment["risk_level"],
            "risk_assessment": assessment,
            "eligibility": eligibility,
            "user_request": user_request,
            "next_step": "等待退款处理服务消费 MQ 消息。",
        }
    )
    mq_message = publish_message(
        REFUND_REQUESTED_TOPIC,
        {
            "refund_id": refund_request["refund_id"],
            "order_id": order_id,
            "user_id": order.get("user_id"),
            "review_required": eligibility["review_required"],
        },
    )

    refund_request["mq_message_id"] = mq_message["message_id"]
    update_refund_request_in_db(
        refund_request["refund_id"],
        {"mq_message_id": mq_message["message_id"]},
    )

    return ToolResult(
        tool_name="refund_apply",
        success=True,
        result=refund_request,
    )


def refund_apply(
    order_id: str,
    user_request: str,
    risk_assessment: dict | None = None,
) -> ToolResult:
    cached_refund = get_refund_idempotency(order_id)
    if cached_refund is not None:
        return ToolResult(
            tool_name="refund_apply",
            success=True,
            result=build_idempotent_replay(cached_refund),
        )

    with refund_distributed_lock(order_id) as lock_acquired:
        if not lock_acquired:
            cached_refund = wait_for_refund_idempotency(order_id)

            if cached_refund is not None:
                return ToolResult(
                    tool_name="refund_apply",
                    success=True,
                    result=build_idempotent_replay(cached_refund),
                )

            return ToolResult(
                tool_name="refund_apply",
                success=False,
                result={
                    "reason": "同一订单退款申请正在处理中，请稍后重试。",
                    "fallback_action": "retry_later",
                    "concurrency_control": {
                        "strategy": "redis_lock_and_idempotency",
                        "status": "lock_busy",
                    },
                },
            )

        cached_refund = get_refund_idempotency(order_id)
        if cached_refund is not None:
            return ToolResult(
                tool_name="refund_apply",
                success=True,
                result=build_idempotent_replay(cached_refund),
            )

        result = _create_refund_request_unlocked(
            order_id=order_id,
            user_request=user_request,
            risk_assessment=risk_assessment,
        )

        if result.success and isinstance(result.result, dict):
            result.result["idempotent_replay"] = False
            result.result["concurrency_control"] = {
                "strategy": "redis_lock_and_idempotency",
                "status": "created_by_lock_owner",
            }
            cache_refund_idempotency(order_id, result.result)

        return result


def create_manual_review(
    order_id: str | None,
    review_type: str,
    risk_level: str,
    risk_flags: list[str],
    user_request: str,
    related_id: str | None = None,
) -> ToolResult:
    """创建人工审核单，用于大额退款、异常账号、投诉升级等高风险动作。"""

    order = get_order_by_id(order_id) if order_id else None
    review = save_manual_review_to_db(
        {
            "order_id": order_id,
            "user_id": order.get("user_id") if order else None,
            "review_type": review_type,
            "risk_level": risk_level,
            "risk_flags": risk_flags,
            "user_request": user_request,
            "related_id": related_id,
            "next_step": "由人工客服复核订单、用户凭证、风控原因和政策依据后处理。",
        }
    )

    return ToolResult(
        tool_name="create_manual_review",
        success=True,
        result=review,
    )


def create_ticket(
    order_id: str | None,
    issue_type: str,
    user_request: str,
    priority: str = "normal",
) -> ToolResult:
    ticket = {
        "status": "pending_human_review",
        "risk_notice": "该工具只生成工单草稿，不会执行真实退款、赔付、取消订单或修改数据库。",
        "order_id": order_id or "未知订单",
        "issue_type": issue_type,
        "priority": priority,
        "user_request": user_request,
        "next_step": "请人工客服核对订单、凭证和售后政策后再处理。",
    }
    saved_ticket = save_ticket_to_db(ticket)

    return ToolResult(
        tool_name="create_ticket",
        success=True,
        result=saved_ticket,
    )


def transfer_to_human(reason: str, user_request: str, priority: str = "normal") -> ToolResult:
    """生成转人工交接单，用于人工审核或人工客服接管。"""

    return ToolResult(
        tool_name="transfer_to_human",
        success=True,
        result={
            "action": "transfer_to_human",
            "status": "pending_human_takeover",
            "reason": reason,
            "priority": priority,
            "user_request": user_request,
            "handoff_summary": f"用户诉求：{user_request}；转人工原因：{reason}。",
            "next_step": "请人工客服查看订单、聊天记录和工具结果后继续处理。",
        },
    )
