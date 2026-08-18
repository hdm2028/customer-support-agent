import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.orchestration.after_sales_agent import evaluate_refund_eligibility
from app.agent.orchestration.risk_agent import evaluate_risk
from app.concurrency.refund_guard import refund_idempotency_key, refund_lock_key
from app.mq.queue import REFUND_REQUESTED_TOPIC, list_messages
from app.storage.cache import cache_health, delete_cache
from app.storage.database import (
    database_health,
    init_database,
    list_refund_requests_from_db,
)
from app.storage.store import get_order_by_id
from app.tools.support_tools import refund_apply


DEFAULT_ORDER_ID = "10009"
DEFAULT_USER_REQUEST = "订单 10009 我要退款。"
DEFAULT_CONCURRENCY = 50


def order_refunds(order_id: str) -> list[dict]:
    return [
        refund
        for refund in list_refund_requests_from_db(limit=1000)
        if str(refund.get("order_id")) == str(order_id)
    ]


def order_refund_messages(order_id: str) -> list[dict]:
    return [
        message
        for message in list_messages(limit=1000)
        if (
            message.get("topic") == REFUND_REQUESTED_TOPIC
            and str(message.get("payload", {}).get("order_id")) == str(order_id)
        )
    ]


def unsafe_refund_decision(order_id: str, user_request: str) -> bool:
    order = get_order_by_id(order_id)

    if not order:
        return False

    risk_assessment = evaluate_risk(order, user_request)
    eligibility = evaluate_refund_eligibility(
        order=order,
        user_request=user_request,
        risk_assessment=risk_assessment,
    )

    return bool(eligibility["eligible"])


def simulate_old_race(order_id: str, user_request: str, concurrency: int) -> dict:
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


def run_guarded_burst(order_id: str, user_request: str, concurrency: int) -> dict:
    delete_cache(refund_idempotency_key(order_id))
    delete_cache(refund_lock_key(order_id))

    before_refunds = len(order_refunds(order_id))
    before_messages = len(order_refund_messages(order_id))
    start = perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(refund_apply, order_id, user_request)
            for _ in range(concurrency)
        ]
        results = [future.result() for future in as_completed(futures)]

    after_refunds = len(order_refunds(order_id))
    after_messages = len(order_refund_messages(order_id))
    successful_results = [
        result.result
        for result in results
        if result.success and isinstance(result.result, dict)
    ]
    failed_results = [result for result in results if not result.success]
    refund_ids = {
        item.get("refund_id")
        for item in successful_results
        if item.get("refund_id")
    }
    created_count = sum(
        1
        for item in successful_results
        if item.get("idempotent_replay") is False
    )
    replayed_count = sum(
        1
        for item in successful_results
        if item.get("idempotent_replay") is True
    )

    return {
        "requests": concurrency,
        "success": len(successful_results),
        "failed": len(failed_results),
        "created_by_lock_owner": created_count,
        "idempotent_replayed": replayed_count,
        "unique_refund_ids": sorted(refund_ids),
        "new_refund_rows": after_refunds - before_refunds,
        "new_mq_messages": after_messages - before_messages,
        "duration_ms": round((perf_counter() - start) * 1000, 2),
    }


def main() -> None:
    init_database()
    order_id = DEFAULT_ORDER_ID
    user_request = DEFAULT_USER_REQUEST
    concurrency = DEFAULT_CONCURRENCY

    print("=" * 72)
    print("Refund Concurrency Stress Test")
    print("=" * 72)
    print(f"database: {database_health()}")
    print(f"cache: {cache_health()}")
    print(f"order_id: {order_id}")
    print(f"concurrency: {concurrency}")
    print("-" * 72)

    old_race = simulate_old_race(order_id, user_request, concurrency)
    print("old_flow_without_redis_guard:")
    print(old_race)
    print("-" * 72)

    guarded = run_guarded_burst(order_id, user_request, concurrency)
    print("new_flow_with_redis_guard:")
    print(guarded)
    print("-" * 72)

    passed = (
        old_race["would_create_duplicate_refunds"] > 0
        and guarded["failed"] == 0
        and guarded["created_by_lock_owner"] == 1
        and guarded["idempotent_replayed"] == concurrency - 1
        and guarded["new_refund_rows"] == 1
        and guarded["new_mq_messages"] == 1
        and len(guarded["unique_refund_ids"]) == 1
    )
    print(f"passed: {passed}")
    print("=" * 72)

    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
