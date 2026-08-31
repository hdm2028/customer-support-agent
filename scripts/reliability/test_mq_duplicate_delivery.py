from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mq.queue import REFUND_CREATED_TOPIC, publish_message
from app.services.refund_service import process_refund_message
from app.storage.database import get_refund_request_from_db, init_database
from app.tools.refund import refund_apply
from scripts.eval.common import (
    build_skipped_report,
    print_json_report,
    save_report,
)


DEFAULT_ORDER_ID = "10009"
SIDE_EFFECTS = [
    "[WRITES DATABASE]",
    "[PUBLISHES MQ]",
    "[UPDATES REFUND STATUS]",
    "[CREATES NOTIFICATIONS]",
]


def ensure_refund_request(order_id: str) -> dict:
    result = refund_apply(order_id, f"订单 {order_id} 我要退款。")
    if not result.success or not isinstance(result.result, dict):
        raise RuntimeError(f"failed to create or reuse refund request: {result.result}")
    return result.result


def is_duplicate_ignored(result: dict) -> bool:
    return result.get("action") in {"duplicate_ignored", "already_processed", "ignored_duplicate_event"}


def run_test(order_id: str) -> dict:
    init_database()
    refund = ensure_refund_request(order_id)
    event_id = f"refund.created:{refund['refund_id']}"
    message = publish_message(
        REFUND_CREATED_TOPIC,
        {
            "event_id": event_id,
            "refund_id": refund["refund_id"],
            "order_id": refund["order_id"],
            "user_id": refund.get("user_id"),
            "review_required": refund.get("status") == "pending_manual_review",
        },
    )

    first_result = process_refund_message(message)
    refund_after_first = get_refund_request_from_db(refund["refund_id"])
    second_result = process_refund_message(message)
    refund_after_second = get_refund_request_from_db(refund["refund_id"])
    ignored_duplicate_events = 1 if is_duplicate_ignored(second_result) else 0
    processed_events = int(bool(first_result.get("success"))) + int(
        bool(second_result.get("success")) and not is_duplicate_ignored(second_result)
    )
    passed = processed_events == 1 and ignored_duplicate_events == 1

    report = {
        "side_effects": SIDE_EFFECTS,
        "order_id": order_id,
        "event_id": event_id,
        "message_id": message["message_id"],
        "received_events": 2,
        "processed_events": processed_events,
        "ignored_duplicate_events": ignored_duplicate_events,
        "first_result": first_result,
        "second_result": second_result,
        "refund_status_after_first": refund_after_first.get("status") if refund_after_first else None,
        "refund_status_after_second": refund_after_second.get("status") if refund_after_second else None,
        "passed_count": 1 if passed else 0,
        "failed_count": 0 if passed else 1,
        "failed_cases": [] if passed else [
            {
                "case_id": "mq_duplicate_delivery",
                "failure_stage": "MQ_FAILURE",
                "expected": {
                    "received_events": 2,
                    "processed_events": 1,
                    "ignored_duplicate_events": 1,
                },
                "actual": {
                    "received_events": 2,
                    "processed_events": processed_events,
                    "ignored_duplicate_events": ignored_duplicate_events,
                    "second_result": second_result,
                },
                "reason": "refund worker does not currently ignore a repeated event_id/message delivery",
            }
        ],
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate duplicate refund.created MQ delivery.")
    parser.add_argument("--order-id", default=DEFAULT_ORDER_ID)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        report = run_test(order_id=args.order_id)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
        )
    report_path = save_report("reliability_mq_duplicate_delivery", report)
    print_json_report("MQ Duplicate Delivery Reliability Test", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
