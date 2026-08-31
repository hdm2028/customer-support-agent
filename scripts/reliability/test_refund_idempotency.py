from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.concurrency.refund_guard import refund_idempotency_key, refund_lock_key
from app.storage.cache import delete_cache
from app.storage.database import init_database
from app.tools.refund import refund_apply
from scripts.eval.common import (
    active_order_refunds,
    build_skipped_report,
    order_refund_messages,
    order_refunds,
    print_json_report,
    save_report,
)


DEFAULT_ORDER_ID = "10009"
DEFAULT_REQUESTS = 10
SIDE_EFFECTS = [
    "[WRITES DATABASE]",
    "[USES REDIS OR MEMORY CACHE]",
    "[PUBLISHES MQ WHEN REFUND IS FIRST CREATED]",
]


def unique_refund_ids(refunds: list[dict]) -> set[str]:
    return {
        refund.get("refund_id")
        for refund in refunds
        if refund.get("refund_id")
    }


def run_test(order_id: str, requests: int) -> dict:
    init_database()
    user_request = f"订单 {order_id} 我要退款。"
    delete_cache(refund_idempotency_key(order_id))
    delete_cache(refund_lock_key(order_id))

    before_refunds = order_refunds(order_id)
    before_messages = order_refund_messages(order_id)

    results = [
        refund_apply(order_id=order_id, user_request=user_request)
        for _ in range(requests)
    ]

    after_refunds = order_refunds(order_id)
    after_active = active_order_refunds(order_id)
    after_messages = order_refund_messages(order_id)
    successful = [
        result.result
        for result in results
        if result.success and isinstance(result.result, dict)
    ]
    failed = [result.result for result in results if not result.success]
    active_ids = unique_refund_ids(after_active)
    duplicate_refunds = max(len(active_ids) - 1, 0)
    new_refund_rows = len(after_refunds) - len(before_refunds)
    new_mq_messages = len(after_messages) - len(before_messages)
    replayed = sum(1 for item in successful if item.get("idempotent_replay") is True)
    created = sum(1 for item in successful if item.get("idempotent_replay") is False)

    passed = (
        len(successful) == requests
        and len(failed) == 0
        and len(after_active) >= 1
        and len(active_ids) == 1
        and duplicate_refunds == 0
        and new_refund_rows <= 1
        and new_mq_messages <= 1
    )

    return {
        "side_effects": SIDE_EFFECTS,
        "order_id": order_id,
        "requests": requests,
        "successful_responses": len(successful),
        "failed_responses": len(failed),
        "created_by_first_request": created,
        "idempotent_replayed": replayed,
        "refund_records": len(after_active),
        "duplicate_refunds": duplicate_refunds,
        "unique_refund_ids": sorted(active_ids),
        "new_refund_rows": new_refund_rows,
        "new_mq_messages": new_mq_messages,
        "refund_status": after_active[0].get("status") if after_active else None,
        "passed_count": 1 if passed else 0,
        "failed_count": 0 if passed else 1,
        "failed_cases": [] if passed else [
            {
                "case_id": "refund_idempotency",
                "failure_stage": "REFUND_FAILURE",
                "expected": "one active refund and zero duplicate side effects",
                "actual": {
                    "refund_records": len(after_active),
                    "duplicate_refunds": duplicate_refunds,
                    "new_refund_rows": new_refund_rows,
                    "new_mq_messages": new_mq_messages,
                    "failed": failed,
                },
                "reason": "same business refund was not idempotent",
            }
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify same refund request is idempotent.")
    parser.add_argument("--order-id", default=DEFAULT_ORDER_ID)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        report = run_test(order_id=args.order_id, requests=args.requests)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
        )
    report_path = save_report("reliability_refund_idempotency", report)
    print_json_report("Refund Idempotency Reliability Test", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
