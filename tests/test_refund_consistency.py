"""Real SQLite transactions and fault injection; every test starts empty."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
import os
import multiprocessing
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

from app.storage import database as db
from app.storage import cache
from app.tools import refund
from app.services import refund_service
from app.mq.queue import consume_messages
from app.mq.queue import fail_message


def _initialize_mysql_worker(dsn, barrier):
    """Each spawned process has its own in-memory cache and lock registry."""
    os.environ.update(DATABASE_BACKEND="mysql", MYSQL_DSN=dsn, REDIS_URL="", REDIS_HOST="")
    from app.storage import mysql_database
    db._INITIALIZED = True
    mysql_database._MYSQL_INITIALIZED = True  # Schema was initialized by the parent.
    cache._CACHE_BACKEND = cache.InMemoryTTLCache()
    global _worker_barrier
    _worker_barrier = barrier


def _mysql_worker_operation(operation):
    action, message = operation
    _worker_barrier.wait(timeout=30)
    if action == "create":
        result = refund.refund_apply("10009", "订单10009我要退款")
        if not result.success:
            raise AssertionError(result.result)
        return result.result
    if action == "claim":
        return consume_messages()
    return refund_service.process_refund_message(message)


class RefundConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The legacy connection context commits but does not close SQLite.
        # Track handles explicitly so Windows can remove the isolated fixture.
        handles = []
        connect = sqlite3.connect
        def tracked_connect(*args, **kwargs):
            kwargs["check_same_thread"] = False
            connection = connect(*args, **kwargs)
            handles.append(connection)
            return connection
        self.addCleanup(lambda: [connection.close() for connection in handles])
        patches = [
            patch.object(sqlite3, "connect", tracked_connect),
            patch.dict("os.environ", {"DATABASE_BACKEND": "sqlite", "REDIS_URL": "", "REDIS_HOST": "", "SEED_DEMO_DATA": "true"}),
            patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.db"),
            patch.object(db, "_INITIALIZED", False),
            patch.object(cache, "_CACHE_BACKEND", cache.InMemoryTTLCache()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        mysql_dsn = os.getenv("CONSISTENCY_MYSQL_DSN")
        if mysql_dsn:
            import pymysql
            from app.storage import mysql_database
            options = mysql_database.parse_mysql_dsn(mysql_dsn)
            admin = pymysql.connect(host=options["host"], port=options["port"],
                                    user=options["user"], password=options["password"], autocommit=True)
            schema = "support_test_" + uuid4().hex
            try:
                with admin.cursor() as cursor:
                    cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4")
            except Exception:
                admin.close()
                raise
            def cleanup_schema():
                # Only the freshly generated test schema can be removed.
                assert schema.startswith("support_test_") and len(schema) == 45
                try:
                    with admin.cursor() as cursor:
                        cursor.execute(f"DROP DATABASE `{schema}`")
                finally:
                    admin.close()
            self.addCleanup(cleanup_schema)
            parsed = urlsplit(mysql_dsn)
            isolated_dsn = urlunsplit(parsed._replace(path="/" + schema))
            for p in [patch.dict(os.environ, {"DATABASE_BACKEND": "mysql", "MYSQL_DSN": isolated_dsn, "SEED_DEMO_DATA": "true"}),
                      patch.object(mysql_database, "_MYSQL_INITIALIZED", False)]:
                p.start()
                self.addCleanup(p.stop)
        db.init_database()

    def create(self):
        result = refund.refund_apply("10009", "订单10009我要退款")
        self.assertTrue(result.success, result.result)
        return result.result

    def count(self, table):
        return self.sql(f"SELECT count(*) AS count FROM {table}", fetch=True)[0]["count"]

    def sql(self, sql, parameters=(), fetch=False):
        from app.storage.transactions import transaction, execute
        with transaction():
            return execute(sql, parameters, fetch=fetch)

    def test_publish_failure_rolls_back_refund_and_retry_creates_once(self):
        with patch.object(refund, "publish_message", side_effect=ConnectionError("injected publish failure")):
            with self.assertRaises(ConnectionError):
                self.create()
        self.assertEqual(self.count("refund_requests"), 0)
        self.assertEqual(self.count("mq_messages"), 0)
        self.create()
        self.assertEqual(self.count("refund_requests"), 1)
        self.assertEqual(self.count("mq_messages"), 1)

    def test_first_concurrent_creation_has_one_refund_and_event(self):
        self.assertEqual(self.count("refund_requests"), 0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.create(), range(50)))
        self.assertEqual(len({r["refund_id"] for r in results}), 1)
        self.assertEqual(sum(not r["idempotent_replay"] for r in results), 1)
        self.assertEqual(self.count("refund_requests"), 1)
        self.assertEqual(self.count("mq_messages"), 1)

    def test_cached_idempotency_returns_current_persisted_status(self):
        created = self.create()
        db.update_refund_request_in_db(created["refund_id"], {"status": "refund_processing"})
        replay = self.create()
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["status"], "refund_processing")
        self.assertEqual(self.count("refund_requests"), 1)

    @unittest.skipUnless(os.getenv("CONSISTENCY_MYSQL_DSN"), "requires dedicated MySQL")
    def test_independent_processes_create_claim_and_deliver_once(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(4)
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=4, mp_context=context, initializer=_initialize_mysql_worker,
            initargs=(os.environ["MYSQL_DSN"], barrier),
        ) as pool:
            created = list(pool.map(_mysql_worker_operation, [("create", None)] * 4))
            self.assertEqual(len({row["refund_id"] for row in created}), 1)
            self.assertEqual(sum(not row["idempotent_replay"] for row in created), 1)
            claimed = list(pool.map(_mysql_worker_operation, [("claim", None)] * 4))
            messages = [message for batch in claimed for message in batch]
            self.assertEqual(len(messages), 1)
            delivered = list(pool.map(_mysql_worker_operation, [("deliver", messages[0])] * 4))
            self.assertEqual(sum(row["business_executed"] for row in delivered), 1)
        self.assertEqual(self.count("refund_requests"), 1)
        self.assertEqual(self.count("mq_messages"), 1)
        self.assertEqual(self.count("notifications"), 1)
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "done")

    def test_order_failure_rolls_back_refund_transition(self):
        created = self.create()
        message = consume_messages()[0]
        original = db.get_order_from_db("10009")
        with patch.object(refund_service, "update_order_in_db", side_effect=RuntimeError("injected order failure")):
            with self.assertRaises(RuntimeError):
                refund_service.process_refund_message(message)
        self.assertEqual(db.get_refund_request_from_db(created["refund_id"])["status"], "queued")
        self.assertEqual(db.get_order_from_db("10009"), original)
        self.assertEqual(self.count("notifications"), 0)
        result = refund_service.process_refund_message(message)
        self.assertTrue(result["business_executed"])
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "refund_processing")

    def test_ack_failure_rolls_back_all_business_and_cache(self):
        created = self.create()
        message = consume_messages()[0]
        original = db.get_order_from_db("10009")
        with patch.object(refund_service, "ack_message", side_effect=RuntimeError("injected ack failure")):
            with self.assertRaises(RuntimeError):
                refund_service.process_refund_message(message)
        self.assertEqual(db.get_refund_request_from_db(created["refund_id"])["status"], "queued")
        self.assertEqual(db.get_order_from_db("10009"), original)
        self.assertEqual(self.count("notifications"), 0)
        self.assertTrue(refund_service.process_refund_message(message)["business_executed"])

    def test_simultaneous_duplicate_delivery_executes_business_once(self):
        self.create()
        message = consume_messages()[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(refund_service.process_refund_message, [message, message]))
        self.assertEqual(sum(r["business_executed"] for r in results), 1)
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "done")
        self.assertEqual(self.count("notifications"), 1)

    def test_consumer_crash_after_claim_is_recovered_after_lease(self):
        self.create()
        first = consume_messages()[0]
        self.assertEqual(consume_messages(), [])
        self.sql("UPDATE mq_messages SET updated_at = ? WHERE message_id = ?", ("2000-01-01T00:00:00", first["message_id"]))
        reclaimed = consume_messages()[0]
        self.assertEqual(reclaimed["message_id"], first["message_id"])
        self.assertEqual(reclaimed["attempts"], first["attempts"] + 1)
        self.assertTrue(refund_service.process_refund_message(reclaimed)["business_executed"])
        self.assertEqual(consume_messages(), [])

    def test_two_consumers_do_not_claim_same_live_message(self):
        self.create()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: consume_messages(), range(2)))
        self.assertEqual(sum(len(r) for r in results), 1)

    def test_stale_worker_cannot_fail_or_execute_reclaimed_message(self):
        self.create()
        old = consume_messages()[0]
        self.sql("UPDATE mq_messages SET updated_at = '2000-01-01T00:00:00'")
        current = consume_messages()[0]
        self.assertFalse(refund_service.process_refund_message(old)["business_executed"])
        fail_message(old["message_id"], {"error": "late failure"}, attempts=old["attempts"])
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "processing")
        refund_service.process_refund_message(current)
        fail_message(current["message_id"], {"error": "late failure"}, attempts=current["attempts"])
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "done")

    def test_failed_message_retries_then_becomes_visible_dead_letter(self):
        self.create()
        for expected_attempts in (1, 2, 3):
            message = consume_messages()[0]
            self.assertEqual(message["attempts"], expected_attempts)
            fail_message(message["message_id"], {"error": "injected"}, attempts=message["attempts"])
            self.sql("UPDATE mq_messages SET updated_at = '2000-01-01T00:00:00'")
        self.assertEqual(consume_messages(), [])
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "dead_letter")

    def test_legacy_missing_event_repair_is_idempotent(self):
        db.save_refund_request_to_db({"order_id": "10009", "user_id": "u009", "status": "queued", "reason": "refund_request", "risk_level": "low"})
        self.assertEqual(self.count("mq_messages"), 0)
        self.assertEqual(refund_service.repair_missing_refund_events()["repaired_count"], 1)
        self.assertEqual(refund_service.repair_missing_refund_events()["repaired_count"], 0)
        self.assertEqual(self.count("mq_messages"), 1)
        self.assertTrue(refund_service.process_refund_tasks()["results"][0]["business_executed"])


if __name__ == "__main__":
    unittest.main()
