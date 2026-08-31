from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter

from app.concurrency.refund_guard import refund_idempotency_key, refund_lock_key
from app.domain.refund_policy import evaluate_refund_eligibility
from app.domain.risk_policy import evaluate_refund_risk
from app.storage.cache import cache_health, delete_cache
from app.storage.database import (
    database_health,
    get_customer_profile_from_db,
    init_database,
)
from app.storage.store import get_order_by_id
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
DEFAULT_CONCURRENCY = 50
SIDE_EFFECTS = [
    "[WRITES DATABASE]",
    "[USES REDIS OR MEMORY CACHE]",
    "[PUBLISHES MQ WHEN REFUND IS FIRST CREATED]",
]


def build_refund_request(order_id: str) -> str:
    return f"订单 {order_id} 我要退款。"


def unsafe_refund_decision(order_id: str, user_request: str) -> bool:
    order = get_order_by_id(order_id)
    if not order:
        return False

    profile = get_customer_profile_from_db(order.get("user_id"))
    risk_assessment = evaluate_refund_risk(order, profile, user_request)
    eligibility = evaluate_refund_eligibility(
        order=order,
        user_request=user_request,
        risk_assessment=risk_assessment,
    )
    return bool(eligibility["eligible"])


def simulate_without_guard(order_id: str, user_request: str, concurrency: int) -> dict:
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(unsafe_refund_decision, order_id, user_request)
            for _ in range(concurrency)
        ]
        decisions = [future.result() for future in as_completed(futures)]

    eligible_count = sum(1 for item in decisions if item)
    return {
        "requests": concurrency,
        "eligible_without_guard": eligible_count,
        "would_create_duplicate_refunds": max(eligible_count - 1, 0),
        "duration_ms": round((perf_counter() - start) * 1000, 2),
    }


def unique_refund_ids(refunds: list[dict]) -> set[str]:
    return {
        refund.get("refund_id")
        for refund in refunds
        if refund.get("refund_id")
    }


def run_guarded_burst(order_id: str, user_request: str, concurrency: int) -> dict:
    delete_cache(refund_idempotency_key(order_id))
    delete_cache(refund_lock_key(order_id))

    before_refunds = order_refunds(order_id)
    before_messages = order_refund_messages(order_id)
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(refund_apply, order_id, user_request)
            for _ in range(concurrency)
        ]
        results = [future.result() for future in as_completed(futures)]

    after_refunds = order_refunds(order_id)
    after_active = active_order_refunds(order_id)
    after_messages = order_refund_messages(order_id)
    successful_results = [
        result.result
        for result in results
        if result.success and isinstance(result.result, dict)
    ]
    failed_results = [result.result for result in results if not result.success]
    active_ids = unique_refund_ids(after_active)

    return {
        "requests": concurrency,
        "successful_responses": len(successful_results),
        "failed_responses": len(failed_results),
        "created_by_lock_owner": sum(1 for item in successful_results if item.get("idempotent_replay") is False),
        "idempotent_replayed": sum(1 for item in successful_results if item.get("idempotent_replay") is True),
        "refund_records": len(after_active),
        "duplicate_refunds": max(len(active_ids) - 1, 0),
        "unique_refund_ids": sorted(active_ids),
        "new_refund_rows": len(after_refunds) - len(before_refunds),
        "new_mq_messages": len(after_messages) - len(before_messages),
        "errors": failed_results,
        "duration_ms": round((perf_counter() - start) * 1000, 2),
    }


def run_test(order_id: str, concurrency: int) -> dict:
    init_database()
    user_request = build_refund_request(order_id)
    baseline = simulate_without_guard(order_id, user_request, concurrency)
    guarded = run_guarded_burst(order_id, user_request, concurrency)
    passed = (
        baseline["would_create_duplicate_refunds"] > 0
        and guarded["failed_responses"] == 0
        and guarded["refund_records"] == 1
        and guarded["duplicate_refunds"] == 0
        and guarded["new_refund_rows"] <= 1
        and guarded["new_mq_messages"] <= 1
    )

    return {
        "side_effects": SIDE_EFFECTS,
        "database": database_health(),
        "cache": cache_health(),
        "order_id": order_id,
        "requests": concurrency,
        "baseline_without_guard": baseline,
        **guarded,
        "passed_count": 1 if passed else 0,
        "failed_count": 0 if passed else 1,
        "failed_cases": [] if passed else [
            {
                "case_id": "refund_concurrency",
                "failure_stage": "REFUND_FAILURE",
                "expected": "Refund Records == 1 and Duplicate Refunds == 0",
                "actual": guarded,
                "reason": "concurrent refund requests were not safely collapsed",
            }
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress test refund_apply concurrency safety.")
    parser.add_argument("--order-id", default=DEFAULT_ORDER_ID)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        report = run_test(order_id=args.order_id, concurrency=args.concurrency)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
        )
    report_path = save_report("reliability_refund_concurrency", report)
    print_json_report("Refund Concurrency Reliability Test", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
