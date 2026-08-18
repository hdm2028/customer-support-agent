from app.mq.queue import REFUND_REQUESTED_TOPIC, ack_message, consume_messages, fail_message
from app.storage.database import (
    get_refund_request_from_db,
    save_notification_to_db,
    update_order_in_db,
    update_refund_request_in_db,
)


def process_refund_message(message: dict) -> dict:
    """处理单条退款 MQ 消息，并推进订单和通知状态。"""

    payload = message["payload"]
    refund_id = payload["refund_id"]
    refund_request = get_refund_request_from_db(refund_id)

    if not refund_request:
        result = {
            "success": False,
            "error": f"退款申请 {refund_id} 不存在。",
        }
        fail_message(message["message_id"], result)
        return result

    if refund_request.get("status") == "pending_manual_review" or payload.get("review_required"):
        updated_refund = update_refund_request_in_db(
            refund_id,
            {
                "status": "pending_manual_review",
                "processor_note": "风控或业务规则要求人工审核，退款处理服务已暂停自动退款。",
            },
        )
        notification = save_notification_to_db(
            {
                "user_id": refund_request.get("user_id"),
                "channel": "system",
                "content": f"退款申请 {refund_id} 已进入人工审核，请等待客服复核。",
                "refund_id": refund_id,
            }
        )
        result = {
            "success": True,
            "action": "manual_review_required",
            "refund_request": updated_refund,
            "notification": notification,
        }
        ack_message(message["message_id"], result)
        return result

    updated_refund = update_refund_request_in_db(
        refund_id,
        {
            "status": "refund_processing",
            "processor_note": "退款任务已被业务处理服务消费，订单状态已更新为退款处理中。",
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
            "content": f"退款申请 {refund_id} 已受理，订单进入退款处理中。",
            "refund_id": refund_id,
        }
    )
    result = {
        "success": True,
        "action": "refund_processing",
        "refund_request": updated_refund,
        "order": updated_order,
        "notification": notification,
    }
    ack_message(message["message_id"], result)

    return result


def process_refund_tasks(limit: int = 10) -> dict:
    """消费一批退款任务，模拟独立业务处理服务。"""

    messages = consume_messages(topic=REFUND_REQUESTED_TOPIC, limit=limit)
    results = []

    for message in messages:
        try:
            results.append(process_refund_message(message))
        except Exception as error:
            result = {
                "success": False,
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
