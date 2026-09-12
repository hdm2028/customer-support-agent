"""Worker control tests and real child processes against disposable databases."""
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import unittest
from unittest.mock import patch

from app.storage import database as db
from app.services import refund_service
from scripts.maintenance import refund_worker


EMPTY_BATCH = {"processed": 0, "results": []}


class RecordingStop:
    def __init__(self, iterations):
        self.iterations = iterations
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.iterations

    def wait(self, duration):
        self.waits.append(duration)
        return self.is_set()


class WorkerControlTests(unittest.TestCase):
    def test_unavailable_backoff_is_bounded_and_resets_after_recovery(self):
        stop = RecordingStop(5)
        results = [ConnectionError("private DSN"), ConnectionError(), ConnectionError(),
                   EMPTY_BATCH, ConnectionError()]
        with patch.object(refund_worker, "process_refund_tasks", side_effect=results), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(refund_worker.run_worker(interval=2, max_backoff=5, stop=stop), 0)
        self.assertEqual(stop.waits, [2, 4, 5, 2, 2])
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["next_poll_seconds"] for r in rows], stop.waits)
        self.assertEqual(rows[3]["event"], "refund_worker_batch")
        self.assertNotIn("private DSN", output.getvalue())

    def test_rejects_nonfinite_intervals_before_processing(self):
        with patch.object(refund_worker, "process_refund_tasks", return_value=EMPTY_BATCH) as process:
            for interval in (0, -1, float("nan"), float("inf")):
                with self.subTest(interval=interval), self.assertRaises(ValueError):
                    refund_worker.run_worker(once=True, interval=interval)
            for maximum in (0, float("nan"), float("inf")):
                with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                    refund_worker.run_worker(once=True, max_backoff=maximum)
        process.assert_not_called()

    def test_once_distinguishes_empty_message_failure_and_unavailable(self):
        for result, code in ((EMPTY_BATCH, 0),
                             ({"processed": 1, "results": [{"success": False}]}, 1),
                             (ConnectionError("private DSN"), 2)):
            stop = RecordingStop(1)
            with self.subTest(code=code), \
                 patch.object(refund_worker, "process_refund_tasks", side_effect=[result]), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(refund_worker.run_worker(once=True, stop=stop), code)
                self.assertEqual(stop.waits, [])
                self.assertNotIn("private DSN", output.getvalue())

    def test_stop_interrupts_idle_wait_without_claiming_another_batch(self):
        stop = Event()
        def finish_batch(**_):
            stop.set()
            return EMPTY_BATCH
        with patch.object(refund_worker, "process_refund_tasks", side_effect=finish_batch) as process, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(refund_worker.run_worker(interval=60, stop=stop), 0)
        self.assertEqual(process.call_count, 1)


def _run_paused_worker(environment, started, release, stop, log_path, crash_after_claim):
    """Pause either before a real batch or after its durable claim, for fault tests."""
    os.environ.update(environment)
    from app.storage import cache, mysql_database
    db.DB_PATH = Path(environment["DATABASE_PATH"])
    db._INITIALIZED = True
    mysql_database._MYSQL_INITIALIZED = True
    cache._CACHE_BACKEND = cache.InMemoryTTLCache()
    target = "process_refund_message" if crash_after_claim else "process_refund_tasks"
    module = refund_service if crash_after_claim else refund_worker
    original = getattr(module, target)
    def paused(*args, **kwargs):
        started.set()
        if not release.wait(20):
            raise TimeoutError("test parent did not release worker")
        return original(*args, **kwargs)
    with open(log_path, "w", encoding="utf-8") as log, \
         patch("sys.stdout", log), patch.object(module, target, side_effect=paused):
        raise SystemExit(refund_worker.run_worker(interval=.05, stop=stop))


class WorkerDatabaseTests(unittest.TestCase):
    def setUp(self):
        from tests.test_refund_consistency import RefundConsistencyTests
        RefundConsistencyTests.setUp(self)

    def create(self):
        from app.tools.refund import refund_apply
        result = refund_apply("10009", "订单10009我要退款")
        self.assertTrue(result.success, result.result)
        return result.result

    def child_environment(self):
        return {**os.environ, "DATABASE_PATH": str(db.DB_PATH), "SEED_DEMO_DATA": "false",
                "REDIS_URL": "", "REDIS_HOST": "", "PAYMENT_ADAPTER": "",
                "MQ_LEASE_SECONDS": "120", "MQ_RETRY_SECONDS": "30", "MQ_MAX_ATTEMPTS": "3",
                "PYTHONIOENCODING": "utf-8"}

    def once(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m",
                                 "scripts.maintenance.refund_worker", "--once"],
                                env=self.child_environment(), capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.stderr, "")
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        return result.returncode, rows[0]

    def paused_child(self, *, crash_after_claim):
        context = multiprocessing.get_context("spawn")
        started, release, stop = context.Event(), context.Event(), context.Event()
        child = context.Process(target=_run_paused_worker,
                                args=(self.child_environment(), started, release, stop,
                                      str(Path(self.tmp.name) / "worker.jsonl"), crash_after_claim))
        child.start()
        def cleanup():
            # A terminated process may leave multiprocessing condition waiters
            # unusable; never signal its events after force-killing it.
            if child.is_alive():
                release.set()
                stop.set()
            child.join(3)
            if child.is_alive():
                child.terminate()
                child.join(5)
            child.close()
        self.addCleanup(cleanup)
        self.assertTrue(started.wait(20), "worker did not reach the controlled pause")
        return child, release, stop

    def assert_processed_once(self, refund_id):
        from app.storage.transactions import transaction, execute
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "refund_processing")
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "refund_processing")
        with transaction():
            self.assertEqual(execute("SELECT COUNT(*) AS count FROM notifications", fetch=True)[0]["count"], 1)
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "done")

    def test_persistent_worker_finishes_current_batch_on_stop(self):
        created = self.create()
        child, release, stop = self.paused_child(crash_after_claim=False)
        stop.set()
        release.set()
        child.join(20)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 0)
        self.assert_processed_once(created["refund_id"])
        rows = Path(self.tmp.name, "worker.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["business_executed"], 1)

    def test_killed_worker_claim_is_recovered_by_new_cli_process_once(self):
        from app.storage.transactions import transaction, execute
        created = self.create()
        child, _, _ = self.paused_child(crash_after_claim=True)
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "processing")
        child.terminate()
        child.join(10)
        self.assertFalse(child.is_alive())
        # Before lease expiry the new process must leave the claim alone.
        code, report = self.once()
        self.assertEqual((code, report["processed"]), (0, 0))
        self.assertEqual(db.get_refund_request_from_db(created["refund_id"])["status"], "queued")
        with transaction():
            execute("UPDATE mq_messages SET updated_at = '2000-01-01T00:00:00'")
        code, report = self.once()
        self.assertEqual((code, report["business_executed"]), (0, 1))
        self.assert_processed_once(created["refund_id"])
        self.assertEqual(db.list_mq_messages_from_db()[0]["attempts"], 2)
        code, report = self.once()
        self.assertEqual((code, report["processed"]), (0, 0))
        self.assert_processed_once(created["refund_id"])

    def test_poison_message_is_delayed_then_dead_letter_and_observable(self):
        from app.mq.queue import REFUND_CREATED_TOPIC, publish_message
        from app.observability.operations import operational_summary
        from app.storage.transactions import transaction, execute
        publish_message(REFUND_CREATED_TOPIC, {"refund_id": "missing-test-refund"})
        for attempt in range(1, 4):
            code, report = self.once()
            self.assertEqual((code, report["failures"]), (1, 1))
            self.assertEqual(db.list_mq_messages_from_db()[0]["attempts"], attempt)
            code, report = self.once()
            self.assertEqual((code, report["processed"]), (0, 0))
            with transaction():
                execute("UPDATE mq_messages SET updated_at = '2000-01-01T00:00:00'")
        self.assertEqual(db.list_mq_messages_from_db()[0]["status"], "dead_letter")
        self.assertIn("mq_dead_letter", {a["code"] for a in operational_summary()["alerts"]})
