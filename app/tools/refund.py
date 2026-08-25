from app.concurrency.refund_guard import (
    build_idempotent_replay,
    cache_refund_idempotency,
    get_refund_idempotency,
    refund_distributed_lock,
    wait_for_refund_idempotency,
)
from app.core.schemas import ToolResult
from app.domain.refund_policy import evaluate_refund_eligibility, infer_refund_reason
from app.domain.risk_policy import evaluate_refund_risk
from app.mq.queue import REFUND_CREATED_TOPIC, publish_message
from app.storage.database import (
    get_customer_profile_from_db,
    get_active_refund_request_by_order_id_from_db,
    save_refund_request_to_db,
    update_refund_request_in_db,
)
from app.storage.store import get_order_by_id


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

    profile = get_customer_profile_from_db(order.get("user_id"))
    assessment = risk_assessment or evaluate_refund_risk(order, profile, user_request)
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
    try:
        refund_request = save_refund_request_to_db(
            {
                "order_id": order_id,
                "idempotency_key": f"refund_apply:{order_id}",
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
    except Exception:
        existing_refund = get_active_refund_request_by_order_id_from_db(order_id)
        if existing_refund is not None:
            cache_refund_idempotency(order_id, existing_refund)
            return ToolResult(
                tool_name="refund_apply",
                success=True,
                result=build_idempotent_replay(existing_refund),
            )

        raise
    mq_message = publish_message(
        REFUND_CREATED_TOPIC,
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

            existing_refund = get_active_refund_request_by_order_id_from_db(order_id)
            if existing_refund is not None:
                cache_refund_idempotency(order_id, existing_refund)
                return ToolResult(
                    tool_name="refund_apply",
                    success=True,
                    result=build_idempotent_replay(existing_refund),
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

        existing_refund = get_active_refund_request_by_order_id_from_db(order_id)
        if existing_refund is not None:
            cache_refund_idempotency(order_id, existing_refund)
            return ToolResult(
                tool_name="refund_apply",
                success=True,
                result=build_idempotent_replay(existing_refund),
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
