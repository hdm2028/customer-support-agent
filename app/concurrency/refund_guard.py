import time
from contextlib import contextmanager
from uuid import uuid4

from app.core.config import get_settings
from app.storage.cache import get_cache_backend, get_json_cache, set_json_cache


def refund_lock_key(order_id: str) -> str:
    return f"lock:refund_apply:{order_id}"


def refund_idempotency_key(order_id: str) -> str:
    return f"idempotency:refund_apply:{order_id}"


def get_refund_idempotency(order_id: str) -> dict | None:
    cached = get_json_cache(refund_idempotency_key(order_id))

    if isinstance(cached, dict):
        return cached

    return None


def cache_refund_idempotency(order_id: str, refund_request: dict) -> None:
    cached_result = {
        **refund_request,
        "idempotency_key": refund_idempotency_key(order_id),
    }
    set_json_cache(
        refund_idempotency_key(order_id),
        cached_result,
        ttl_seconds=get_settings().refund_idempotency_ttl_seconds,
    )


def build_idempotent_replay(refund_request: dict) -> dict:
    return {
        **refund_request,
        "idempotent_replay": True,
        "concurrency_control": {
            "strategy": "redis_lock_and_idempotency",
            "status": "reused_existing_refund_request",
        },
    }


@contextmanager
def refund_distributed_lock(order_id: str):
    settings = get_settings()
    backend = get_cache_backend()
    token = uuid4().hex
    lock_key = refund_lock_key(order_id)
    acquired = backend.set_if_absent(
        lock_key,
        token,
        ex=settings.refund_lock_ttl_seconds,
    )

    try:
        yield acquired
    finally:
        if acquired:
            backend.compare_and_delete(lock_key, token)


def wait_for_refund_idempotency(order_id: str) -> dict | None:
    deadline = time.monotonic() + get_settings().refund_lock_wait_seconds

    while time.monotonic() < deadline:
        cached = get_refund_idempotency(order_id)

        if cached is not None:
            return cached

        time.sleep(0.05)

    return None
