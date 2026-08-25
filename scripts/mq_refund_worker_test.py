import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.mq.queue import REFUND_CREATED_TOPIC, publish_message
from app.services.refund_service import process_refund_message
from app.storage.database import (
    get_refund_request_from_db,
    init_database,
    list_refund_requests_from_db,
)
from app.tools.refund import refund_apply


def ensure_refund_request() -> dict:
    refunds = list_refund_requests_from_db(limit=20)
    if refunds:
        return refunds[0]

    result = refund_apply("10001", "订单 10001 耳机坏了我要退款")
    if not result.success:
        raise AssertionError(f"failed to create refund request: {result.result}")

    return result.result


def main() -> None:
    init_database()
    refund = ensure_refund_request()
    message = publish_message(
        REFUND_CREATED_TOPIC,
        {
            "refund_id": refund["refund_id"],
            "order_id": refund["order_id"],
            "user_id": refund.get("user_id"),
            "review_required": refund.get("status") == "pending_manual_review",
        },
    )
    result = process_refund_message(message)
    updated_refund = get_refund_request_from_db(refund["refund_id"])

    if not result["success"]:
        raise AssertionError(f"worker failed: {result}")

    if updated_refund["status"] not in {"refund_processing", "pending_manual_review"}:
        raise AssertionError(f"unexpected refund status: {updated_refund['status']}")

    print("mq_refund_worker_test: passed")
    print({"message_id": message["message_id"], "refund_status": updated_refund["status"]})


if __name__ == "__main__":
    main()
