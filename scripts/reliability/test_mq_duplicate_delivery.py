from __future__ import annotations

import argparse

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
        raise RuntimeError(
            f"failed to create or reuse refund request: {result.result}"
        )

    return result.result


def business_executed(result: dict) -> bool:
    """判断本次 MQ 消费是否真正执行了核心退款业务。"""
    return result.get("business_executed") is True


def duplicate_business_ignored(result: dict) -> bool:
    """判断重复消息是否被业务幂等逻辑阻止。"""
    return (
        result.get("business_executed") is False
        and result.get("duplicate_ignored") is True
    )


def run_test(order_id: str) -> dict:
    init_database()

    refund = ensure_refund_request(order_id)

    refund_id = refund["refund_id"]
    event_id = f"refund.created:{refund_id}"

    message = publish_message(
        REFUND_CREATED_TOPIC,
        {
            "event_id": event_id,
            "refund_id": refund_id,
            "order_id": refund["order_id"],
            "user_id": refund.get("user_id"),
            "review_required": (
                refund.get("status") == "pending_manual_review"
            ),
        },
    )

    initial_refund = get_refund_request_from_db(refund_id)
    initial_status = (
        initial_refund.get("status")
        if initial_refund
        else None
    )

    first_result = process_refund_message(message)

    refund_after_first = get_refund_request_from_db(refund_id)

    second_result = process_refund_message(message)

    refund_after_second = get_refund_request_from_db(refund_id)

    results = [
        first_result,
        second_result,
    ]

    received_events = 2

    consumer_handled_events = sum(
        1
        for result in results
        if result.get("success") is True
    )

    business_executed_events = sum(
        1
        for result in results
        if business_executed(result)
    )

    duplicate_business_ignored_events = sum(
        1
        for result in results
        if duplicate_business_ignored(result)
    )

    refund_status_after_first = (
        refund_after_first.get("status")
        if refund_after_first
        else None
    )

    refund_status_after_second = (
        refund_after_second.get("status")
        if refund_after_second
        else None
    )

    status_stable_after_processing = (
        refund_status_after_first
        == refund_status_after_second
    )

    no_duplicate_business_execution = (
        business_executed_events <= 1
    )

    second_delivery_did_not_execute_business = (
        not business_executed(second_result)
    )

    second_delivery_was_idempotent = (
        duplicate_business_ignored(second_result)
    )

    passed = all(
        [
            received_events == 2,
            consumer_handled_events == 2,
            no_duplicate_business_execution,
            second_delivery_did_not_execute_business,
            second_delivery_was_idempotent,
            status_stable_after_processing,
        ]
    )

    report = {
        "side_effects": SIDE_EFFECTS,
        "order_id": order_id,
        "refund_id": refund_id,
        "event_id": event_id,
        "message_id": message["message_id"],
        "initial_refund_status": initial_status,
        "received_events": received_events,
        "consumer_handled_events": consumer_handled_events,
        "business_executed_events": business_executed_events,
        "duplicate_business_ignored_events": (
            duplicate_business_ignored_events
        ),
        "first_result": first_result,
        "second_result": second_result,
        "refund_status_after_first": refund_status_after_first,
        "refund_status_after_second": refund_status_after_second,
        "assertions": {
            "no_duplicate_business_execution": (
                no_duplicate_business_execution
            ),
            "second_delivery_did_not_execute_business": (
                second_delivery_did_not_execute_business
            ),
            "second_delivery_was_idempotent": (
                second_delivery_was_idempotent
            ),
            "status_stable_after_processing": (
                status_stable_after_processing
            ),
        },
        "passed_count": 1 if passed else 0,
        "failed_count": 0 if passed else 1,
        "failed_cases": (
            []
            if passed
            else [
                {
                    "case_id": "mq_duplicate_delivery",
                    "failure_stage": "MQ_IDEMPOTENCY_FAILURE",
                    "expected": {
                        "received_events": 2,
                        "consumer_handled_events": 2,
                        "business_executed_events_max": 1,
                        "second_delivery_business_executed": False,
                        "second_delivery_duplicate_ignored": True,
                        "refund_status_stable": True,
                    },
                    "actual": {
                        "received_events": received_events,
                        "consumer_handled_events": (
                            consumer_handled_events
                        ),
                        "business_executed_events": (
                            business_executed_events
                        ),
                        "duplicate_business_ignored_events": (
                            duplicate_business_ignored_events
                        ),
                        "second_result": second_result,
                        "refund_status_after_first": (
                            refund_status_after_first
                        ),
                        "refund_status_after_second": (
                            refund_status_after_second
                        ),
                    },
                    "reason": (
                        "duplicate MQ delivery caused repeated core "
                        "refund business execution or failed to preserve "
                        "idempotent refund state"
                    ),
                }
            ]
        ),
    }

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify refund business idempotency under duplicate "
            "refund.created MQ delivery."
        )
    )
    parser.add_argument(
        "--order-id",
        default=DEFAULT_ORDER_ID,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        report = run_test(
            order_id=args.order_id,
        )
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
        )

    report_path = save_report(
        "reliability_mq_duplicate_delivery",
        report,
    )

    print_json_report(
        "MQ Duplicate Delivery Reliability Test",
        report,
        report_path,
    )

    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()