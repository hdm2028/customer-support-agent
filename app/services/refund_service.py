from app.mq.queue import (
    REFUND_CREATED_TOPIC,
    ack_message,
    consume_messages,
    fail_message,
)
from app.storage.database import (
    get_refund_request_from_db,
    save_notification_to_db,
    update_order_in_db,
    update_refund_request_in_db,
)
from app.storage.transactions import lock_record, transaction
import json


def repair_missing_refund_events(limit: int = 100) -> dict:
    """Repair legacy committed intents lacking any queue event, never re-pay.

    Failed/dead-letter events are intentionally retained for explicit inspection;
    they are not mistaken for absent events and duplicated.
    """
    import json
    from app.mq.queue import publish_message
    from app.storage.transactions import execute
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    repaired = []
    with transaction() as state:
        refund_ref = "JSON_EXTRACT(m.payload, '$.refund_id')"
        if state.mysql:
            refund_ref = f"JSON_UNQUOTE({refund_ref})"
        rows = execute("SELECT r.refund_id FROM refund_requests r WHERE r.status IN ('queued', 'pending_manual_review') "
                       "AND NOT EXISTS (SELECT 1 FROM mq_messages m WHERE m.topic = ? AND " + refund_ref +
                       " = r.refund_id) ORDER BY r.created_at LIMIT ?" + (" FOR UPDATE" if state.mysql else ""),
                       (REFUND_CREATED_TOPIC, limit), fetch=True)
        for row in rows:
            locked = lock_record("refund_requests", "refund_id", row["refund_id"])
            refund = locked["payload"] if isinstance(locked["payload"], dict) else json.loads(locked["payload"])
            message = publish_message(REFUND_CREATED_TOPIC, {
                "refund_id": refund["refund_id"], "order_id": refund["order_id"],
                "user_id": refund.get("user_id"),
                "review_required": refund["status"] == "pending_manual_review" or refund.get("eligibility", {}).get("review_required", False),
            })
            update_refund_request_in_db(refund["refund_id"], {"mq_message_id": message["message_id"]})
            state.invalidate_keys.add(f"idempotency:refund_apply:{refund['order_id']}")
            repaired.append(refund["refund_id"])
    return {"success": True, "repaired_count": len(repaired), "refund_ids": repaired}


def process_refund_message(message: dict) -> dict:
    # Lock before reading state. Refund, order, notification and ack either all
    # commit or all roll back; an expired lease can safely retry after a crash.
    with transaction() as state:
        stored = lock_record("mq_messages", "message_id", message["message_id"])
        if not stored:
            return {"success": False, "business_executed": False, "action": "message_not_found"}
        if stored["attempts"] != message.get("attempts", 0):
            return {"success": True, "business_executed": False, "duplicate_ignored": True, "action": "stale_delivery"}
        if stored["status"] == "done":
            return {"success": True, "business_executed": False, "duplicate_ignored": True,
                    "action": "message_already_done"}
        lock_record("refund_requests", "refund_id", message["payload"]["refund_id"])
        result = _process_refund_message(message)
        order = result.get("refund_request", {}).get("order_id")
        if order:
            state.invalidate_keys.add(f"idempotency:refund_apply:{order}")
        return result


def _process_refund_message(message: dict) -> dict:
    """
    处理退款创建消息。

    MQ 允许重复投递，但退款核心业务状态迁移必须保持幂等。
    通知属于非核心副作用，允许根据业务策略重复发送。
    """
    payload = message["payload"]
    message_id = message["message_id"]
    refund_id = payload["refund_id"]

    refund_request = get_refund_request_from_db(refund_id)

    if not refund_request:
        result = {
            "success": False,
            "action": "refund_not_found",
            "business_executed": False,
            "error": f"退款申请 {refund_id} 不存在。",
        }
        fail_message(message_id, result)
        return result

    current_status = refund_request.get("status")

    if current_status in {"cancelled", "canceled", "rejected", "failed", "refund_succeeded", "refund_unknown", "payment_submitting"}:
        result = {"success": True, "action": "refund_event_noop", "business_executed": False,
                  "duplicate_ignored": True, "refund_request": refund_request}
        ack_message(message_id, result)
        return result

    # 已经进入人工审核，不再执行自动退款处理。
    if current_status == "pending_manual_review":
        notification = save_notification_to_db(
            {
                "user_id": refund_request.get("user_id"),
                "channel": "system",
                "content": f"退款申请 {refund_id} 正在人工审核，请等待客服复核。",
                "refund_id": refund_id,
            }
        )

        result = {
            "success": True,
            "action": "manual_review_already_pending",
            "business_executed": False,
            "duplicate_ignored": True,
            "refund_request": refund_request,
            "notification": notification,
        }

        ack_message(message_id, result)
        return result

    # 当前消息要求进入人工审核。
    if payload.get("review_required") and not refund_request.get("review_approved"):
        if current_status != "queued":
            result = {
                "success": True,
                "action": "refund_event_noop",
                "business_executed": False,
                "duplicate_ignored": True,
                "reason": (
                    f"退款当前状态为 {current_status}，"
                    "不允许重复进入人工审核。"
                ),
                "refund_request": refund_request,
            }

            ack_message(message_id, result)
            return result

        updated_refund = update_refund_request_in_db(
            refund_id,
            {
                "status": "pending_manual_review",
                "processor_note": (
                    "风控或业务规则要求人工审核，"
                    "退款处理服务已暂停自动退款。"
                ),
            },
        )

        notification = save_notification_to_db(
            {
                "user_id": refund_request.get("user_id"),
                "channel": "system",
                "content": (
                    f"退款申请 {refund_id} 已进入人工审核，"
                    "请等待客服复核。"
                ),
                "refund_id": refund_id,
            }
        )

        result = {
            "success": True,
            "action": "manual_review_required",
            "business_executed": True,
            "duplicate_ignored": False,
            "refund_request": updated_refund,
            "notification": notification,
        }

        ack_message(message_id, result)
        return result

    # refund.created 只允许推动 queued -> refund_processing。
    #
    # 如果退款已经进入其他状态，说明核心业务已经处理过，
    # 重复 MQ 消息不再执行退款和订单状态更新。
    if current_status != "queued":
        notification = save_notification_to_db(
            {
                "user_id": refund_request.get("user_id"),
                "channel": "system",
                "content": (
                    f"退款申请 {refund_id} 已受理，"
                    f"当前状态为 {current_status}。"
                ),
                "refund_id": refund_id,
            }
        )

        result = {
            "success": True,
            "action": "refund_event_noop",
            "business_executed": False,
            "duplicate_ignored": True,
            "reason": (
                f"退款当前状态为 {current_status}，"
                "无需重复执行业务处理。"
            ),
            "refund_request": refund_request,
            "notification": notification,
        }

        ack_message(message_id, result)
        return result

    # 核心业务状态迁移：
    # queued -> refund_processing
    order_row = lock_record("orders", "order_id", refund_request["order_id"])
    if order_row is None:
        raise RuntimeError("Refund order is missing; transaction rolled back")
    original_order = order_row["payload"] if isinstance(order_row["payload"], dict) else json.loads(order_row["payload"])
    updated_refund = update_refund_request_in_db(
        refund_id,
        {
            "status": "refund_processing",
            "order_before_refund": {key: original_order.get(key) for key in
                                    ("order_status", "after_sales_status", "last_refund_id", "shipping_status", "signed_date")},
            "processor_note": (
                "退款任务已被业务处理服务消费，"
                "订单状态已更新为退款处理中。"
            ),
        },
    )

    updated_order = update_order_in_db(
        refund_request["order_id"],
        {
            "order_status": "退款处理中",
            "after_sales_status": "refund_processing",
            "last_refund_id": refund_id,
        },
    )
    if updated_order is None:
        raise RuntimeError("Refund order is missing; transaction rolled back")

    notification = save_notification_to_db(
        {
            "user_id": refund_request.get("user_id"),
            "channel": "system",
            "content": (
                f"退款申请 {refund_id} 已受理，"
                "订单进入退款处理中。"
            ),
            "refund_id": refund_id,
        }
    )

    result = {
        "success": True,
        "action": "refund_processing",
        "business_executed": True,
        "duplicate_ignored": False,
        "refund_request": updated_refund,
        "order": updated_order,
        "notification": notification,
    }

    ack_message(message_id, result)
    return result


def process_refund_tasks(limit: int = 10) -> dict:
    """批量消费退款创建消息。"""
    messages = consume_messages(
        topic=REFUND_CREATED_TOPIC,
        limit=limit,
    )
    results = []

    for message in messages:
        try:
            results.append(process_refund_message(message))
        except Exception as error:
            result = {
                "success": False,
                "business_executed": False,
                "error_type": type(error).__name__,
                "error_message": str(error),
            }

            fail_message(message["message_id"], result, attempts=message.get("attempts"))
            results.append(result)

    return {
        "success": all(result.get("success", False) for result in results),
        "processed": len(results),
        "results": results,
    }
