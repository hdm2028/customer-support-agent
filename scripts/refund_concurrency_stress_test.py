import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.concurrency.refund_guard import refund_idempotency_key, refund_lock_key
from app.domain.refund_policy import evaluate_refund_eligibility
from app.domain.risk_policy import evaluate_refund_risk
from app.mq.queue import REFUND_CREATED_TOPIC, list_messages
from app.storage.cache import cache_health, delete_cache
from app.storage.database import (
    database_health,
    get_customer_profile_from_db,
    get_active_refund_request_by_order_id_from_db,
    init_database,
    list_refund_requests_from_db,
)
from app.storage.store import get_order_by_id, load_orders
from app.tools.refund import refund_apply


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
            message.get("topic") == REFUND_CREATED_TOPIC
            and str(message.get("payload", {}).get("order_id")) == str(order_id)
        )
    ]


def build_refund_request(order_id: str) -> str:
    return f"订单 {order_id} 我要退款。"


def select_refund_stress_case() -> tuple[str, str]:
    for order in load_orders():
        order_id = str(order.get("order_id"))
        if get_active_refund_request_by_order_id_from_db(order_id):
            continue

        user_request = build_refund_request(order_id)
        if unsafe_refund_decision(order_id, user_request):
            return order_id, user_request

    return DEFAULT_ORDER_ID, DEFAULT_USER_REQUEST


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

    db_had_active_refund = get_active_refund_request_by_order_id_from_db(order_id) is not None
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
        "db_had_active_refund": db_had_active_refund,
        "unique_refund_ids": sorted(refund_ids),
        "new_refund_rows": after_refunds - before_refunds,
        "new_mq_messages": after_messages - before_messages,
        "duration_ms": round((perf_counter() - start) * 1000, 2),
    }


def main() -> None:
    init_database()
    order_id, user_request = select_refund_stress_case()
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

    if guarded["db_had_active_refund"]:
        passed = (
            old_race["would_create_duplicate_refunds"] > 0
            and guarded["failed"] == 0
            and guarded["created_by_lock_owner"] == 0
            and guarded["idempotent_replayed"] == concurrency
            and guarded["new_refund_rows"] == 0
            and guarded["new_mq_messages"] == 0
            and len(guarded["unique_refund_ids"]) == 1
        )
    else:
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
