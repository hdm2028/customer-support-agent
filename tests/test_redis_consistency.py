"""Only run via run_isolated_redis_checks: the Redis instance is disposable."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from threading import Event
import json
import multiprocessing
import os
import subprocess
import time
import unittest
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.concurrency.refund_guard import RefundLock, refund_lock_key
from app.storage import cache, database as db
from app.tools import refund


def _initialize(dsn, url, barrier, initialize_from_settings=False):
    os.environ.update(DATABASE_BACKEND="mysql", MYSQL_DSN=dsn, REDIS_URL=url,
                      REDIS_HOST="", REFUND_LOCK_TTL_SECONDS="2", REFUND_LOCK_WAIT_SECONDS="4")
    from app.storage import mysql_database
    db._INITIALIZED = mysql_database._MYSQL_INITIALIZED = True
    cache._CACHE_BACKEND = None if initialize_from_settings else cache.RedisJsonCache(url)
    global _barrier
    _barrier = barrier


def _create(_):
    _barrier.wait(timeout=15)
    result = refund.refund_apply("10009", "申请退款")
    return {"success": result.success, "result": result.result, "backend": cache.cache_backend_name()}


def _hold(url, ready):
    os.environ.update(REDIS_URL=url, REFUND_LOCK_TTL_SECONDS="2")
    cache._CACHE_BACKEND = cache.RedisJsonCache(url)
    lock = RefundLock("crash-order")
    ready.put(lock.acquire())
    time.sleep(30)


@unittest.skipUnless(os.getenv("CONSISTENCY_REDIS_URL") and os.getenv("DISPOSABLE_REDIS_CONTAINER"),
                     "requires runner-owned disposable Redis and dedicated MySQL")
class RedisConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.container = os.environ["DISPOSABLE_REDIS_CONTAINER"]
        details = json.loads(subprocess.check_output(["docker", "inspect", self.container], text=True))[0]
        self.assertEqual(details["Config"]["Labels"].get("support.test"), os.environ["DISPOSABLE_REDIS_MARKER"])
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.url = os.environ["CONSISTENCY_REDIS_URL"]
        backend = cache.RedisJsonCache(self.url)
        backend.client.flushdb()  # Verified runner-owned instance, never the project Redis.
        self.addCleanup(backend.client.close)
        for p in [patch.object(cache, "_CACHE_BACKEND", backend),
                  patch.dict(os.environ, {"REDIS_URL": self.url, "REFUND_LOCK_TTL_SECONDS": "2", "REFUND_LOCK_WAIT_SECONDS": "4"})]:
            p.start()
            self.addCleanup(p.stop)
        self.assertTrue(db.using_mysql_backend())

    def test_four_processes_create_one_refund_and_event_using_redis(self):
        context = multiprocessing.get_context("spawn")
        with context.Manager() as manager:
            barrier = manager.Barrier(4)
            with ProcessPoolExecutor(max_workers=4, mp_context=context, initializer=_initialize,
                                     initargs=(os.environ["MYSQL_DSN"], self.url, barrier)) as pool:
                results = list(pool.map(_create, range(4)))
        self.assertTrue(all(r["success"] and r["backend"] == "redis" for r in results), results)
        self.assertEqual(len({r["result"]["refund_id"] for r in results}), 1)
        self.assertEqual(sum(not r["result"]["idempotent_replay"] for r in results), 1)
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)

    def test_expired_owner_cannot_delete_successors_lock(self):
        old, new = RefundLock("expiry-order"), RefundLock("expiry-order")
        self.assertTrue(old.acquire())
        self.assertFalse(new.acquire())
        deadline = time.monotonic() + 5
        while not new.acquire():
            if time.monotonic() > deadline:
                self.fail("lock TTL did not expire")
            time.sleep(.05)
        old.release()
        self.assertEqual(cache.get_cache_backend().get(new.lock_key), new.token)
        new.release()
        self.assertIsNone(cache.get_cache_backend().get(new.lock_key))

    def test_expired_refund_lock_still_has_one_database_intent(self):
        entered, release = Event(), Event()
        original = refund._persist_refund_and_event
        def slow_first(payload):
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("fixture did not release first writer")
            return original(payload)
        with patch.object(refund, "_persist_refund_and_event", side_effect=slow_first):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(refund.refund_apply, "10009", "申请退款")
                try:
                    self.assertTrue(entered.wait(5))
                    deadline = time.monotonic() + 5
                    while cache.get_cache_backend().get(refund_lock_key("10009")):
                        if time.monotonic() > deadline:
                            self.fail("first refund lock did not expire")
                        time.sleep(.05)
                    second = refund.refund_apply("10009", "再次申请退款")
                finally:
                    release.set()
                late = first.result(timeout=10)
        self.assertTrue(second.success and late.success)
        self.assertEqual(second.result["refund_id"], late.result["refund_id"])
        self.assertTrue(late.result["idempotent_replay"])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)

    def test_outage_before_creation_fails_without_writes_and_cold_workers_use_db_uniqueness(self):
        from app.tools.executor import safe_tool_call
        subprocess.run(["docker", "stop", "--time", "1", self.container], capture_output=True, check=True, timeout=15)
        try:
            result = safe_tool_call("refund_apply", lambda: refund.refund_apply("10009", "申请退款"))
            self.assertFalse(result.success)
            self.assertEqual(self.fixture.count("refund_requests"), 0)
            self.assertFalse(cache.cache_health()["reachable"])
            context = multiprocessing.get_context("spawn")
            with context.Manager() as manager:
                barrier = manager.Barrier(4)
                with ProcessPoolExecutor(max_workers=4, mp_context=context, initializer=_initialize,
                                         initargs=(os.environ["MYSQL_DSN"], self.url, barrier, True)) as pool:
                    results = list(pool.map(_create, range(4)))
            self.assertTrue(all(r["success"] and r["backend"] == "memory" for r in results), results)
            self.assertEqual(len({r["result"]["refund_id"] for r in results}), 1)
            self.assertEqual(self.fixture.count("refund_requests"), 1)
            self.assertEqual(self.fixture.count("mq_messages"), 1)
        finally:
            subprocess.run(["docker", "start", self.container], capture_output=True, check=True, timeout=15)
            deadline = time.monotonic() + 10
            while True:
                try:
                    cache.get_cache_backend().ping()
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.1)

    def test_terminated_owner_recovers_by_ttl(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        process = context.Process(target=_hold, args=(self.url, ready))
        process.start()
        try:
            self.assertTrue(ready.get(timeout=10))
            self.assertIsNotNone(cache.get_cache_backend().get(refund_lock_key("crash-order")))
            process.terminate()
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            replacement = RefundLock("crash-order")
            deadline = time.monotonic() + 5
            while not replacement.acquire():
                if time.monotonic() > deadline:
                    self.fail("terminated owner's lock did not expire")
                time.sleep(.05)
            replacement.release()
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            ready.close()
            ready.join_thread()

    def test_post_commit_redis_outage_does_not_report_refund_creation_failure(self):
        from app.tools.executor import safe_tool_call
        original = refund.cache_refund_idempotency
        stopped = False
        def stop_before_cache(*args):
            nonlocal stopped
            if not stopped:
                subprocess.run(["docker", "stop", "--time", "1", self.container], capture_output=True, check=True, timeout=15)
                stopped = True
            return original(*args)
        try:
            with patch.object(refund, "cache_refund_idempotency", side_effect=stop_before_cache):
                result = safe_tool_call("refund_apply", lambda: refund.refund_apply("10009", "申请退款"))
            self.assertEqual(self.fixture.count("refund_requests"), 1)
            self.assertEqual(self.fixture.count("mq_messages"), 1)
            self.assertTrue(result.success, result.result)
        finally:
            subprocess.run(["docker", "start", self.container], capture_output=True, check=True, timeout=15)
            deadline = time.monotonic() + 10
            while True:
                try:
                    if cache.get_cache_backend().ping():
                        break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.1)
        replay = refund.refund_apply("10009", "重试退款")
        self.assertTrue(replay.success)
        self.assertTrue(replay.result["idempotent_replay"])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)
