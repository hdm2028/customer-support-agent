import time
import logging
from uuid import uuid4

from app.core.config import get_settings
from app.storage.cache import get_cache_backend, get_json_cache, set_json_cache


logger = logging.getLogger(__name__)


def refund_lock_key(order_id: str) -> str:
    return f"lock:refund_apply:{order_id}"


def refund_idempotency_key(order_id: str) -> str:
    return f"idempotency:refund_apply:{order_id}"


def get_refund_idempotency(order_id: str) -> dict | None:
    cached = get_json_cache(refund_idempotency_key(order_id))

    if isinstance(cached, dict) and cached.get("refund_id"):
        # The cache locates the existing intent; its status snapshot may be old
        # after another process consumes, reviews or reconciles the refund.
        from app.storage.database import get_refund_request_from_db
        current = get_refund_request_from_db(cached["refund_id"])
        if current is not None and str(current.get("order_id")) == str(order_id):
            return current

    return None


def cache_refund_idempotency(order_id: str, refund_request: dict) -> None:
    cached_result = {
        **refund_request,
        "idempotency_key": refund_idempotency_key(order_id),
    }
    try:
        set_json_cache(
            refund_idempotency_key(order_id),
            cached_result,
            ttl_seconds=get_settings().refund_idempotency_ttl_seconds,
        )
    except Exception as error:
        # The database unique key and transactional event remain authoritative.
        logger.warning("Refund idempotency cache update unavailable: %s", type(error).__name__)


def build_idempotent_replay(refund_request: dict) -> dict:
    return {
        **refund_request,
        "idempotent_replay": True,
        "concurrency_control": {
            "strategy": "redis_lock_and_idempotency",
            "status": "reused_existing_refund_request",
        },
    }


class RefundLock:
    """订单粒度的退款分布式锁，底层由 Redis SET NX + TTL 实现。"""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.token = uuid4().hex
        self.lock_key = refund_lock_key(order_id)
        self.acquired = False

    def acquire(self) -> bool:
        settings = get_settings()
        backend = get_cache_backend()
        self.acquired = backend.set_if_absent(
            self.lock_key,
            self.token,
            ex=settings.refund_lock_ttl_seconds,
        )

        return self.acquired

    def release(self) -> None:
        if self.acquired:
            try:
                get_cache_backend().compare_and_delete(self.lock_key, self.token)
            except Exception as error:
                # A lost connection must not replace a committed result (or the
                # original business error); the owned key can expire by TTL.
                logger.warning("Refund lock release unavailable: %s", type(error).__name__)
            finally:
                self.acquired = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def refund_distributed_lock(order_id: str) -> RefundLock:
    return RefundLock(order_id)


def wait_for_refund_idempotency(order_id: str) -> dict | None:
    deadline = time.monotonic() + get_settings().refund_lock_wait_seconds

    while time.monotonic() < deadline:
        cached = get_refund_idempotency(order_id)

        if cached is not None:
            return cached

        time.sleep(0.05)

    return None
