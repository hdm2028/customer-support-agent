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


def process_refund_message(message: dict) -> dict:
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
    if payload.get("review_required"):
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
    updated_refund = update_refund_request_in_db(
        refund_id,
        {
            "status": "refund_processing",
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

            fail_message(message["message_id"], result)
            results.append(result)

    return {
        "success": True,
        "processed": len(results),
        "results": results,
    }