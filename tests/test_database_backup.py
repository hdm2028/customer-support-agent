import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import io
from threading import Event
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from app.storage import database as db
from app.storage.mysql_database import parse_mysql_dsn
from scripts.maintenance.database_backup import (
    connect_mysql, mysql_restore, mysql_snapshot, sqlite_restore, sqlite_snapshot,
)


class BackupTests(unittest.TestCase):
    def setUp(self):
        from tests.test_refund_consistency import RefundConsistencyTests
        RefundConsistencyTests.setUp(self)

    def seed_business_state(self):
        from app.tools.refund import refund_apply
        from app.mq.queue import consume_messages
        from app.services.refund_service import process_refund_message
        refund = refund_apply("10009", "订单10009我要退款")
        self.assertTrue(refund.success)
        process_refund_message(consume_messages()[0])
        db.append_message_to_db("recovery-conversation", "user", "恢复演练对话")
        return refund.result["refund_id"]

    def test_restart_does_not_reset_existing_order_after_refund_processing(self):
        self.seed_business_state()
        original = db.get_order_from_db("10009")
        self.assertEqual(original["after_sales_status"], "refund_processing")
        db._INITIALIZED = False
        if db.using_mysql_backend():
            from app.storage import mysql_database
            mysql_database._MYSQL_INITIALIZED = False
        from app.storage import cache
        cache._CACHE_BACKEND = cache.InMemoryTTLCache()
        db.init_database()
        self.assertEqual(db.get_order_from_db("10009"), original)

    def test_local_business_cache_cannot_hide_another_workers_committed_order(self):
        original = db.get_order_from_db("10009")
        changed = {**original, "after_sales_status": "refund_processing"}
        # Write through a separate connection without this process's invalidation.
        if db.using_mysql_backend():
            with connect_mysql(parse_mysql_dsn(os.environ["MYSQL_DSN"])) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE orders SET payload = %s WHERE order_id = %s",
                                   (json.dumps(changed, ensure_ascii=False), "10009"))
        else:
            with sqlite3.connect(db.DB_PATH) as connection:
                connection.execute("UPDATE orders SET payload = ? WHERE order_id = ?",
                                   (json.dumps(changed, ensure_ascii=False), "10009"))
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "refund_processing")

    def test_worker_processes_once_and_stops_without_extra_delivery(self):
        from app.tools.refund import refund_apply
        from scripts.maintenance.refund_worker import run_worker
        self.assertTrue(refund_apply("10009", "订单10009我要退款").success)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(run_worker(once=True), 0)
        report = json.loads(output.getvalue())
        self.assertEqual((report["processed"], report["business_executed"]), (1, 1))
        self.assertNotIn("10009", output.getvalue())
        stop = Event()
        stop.set()
        with patch("scripts.maintenance.refund_worker.process_refund_tasks") as process:
            self.assertEqual(run_worker(stop=stop), 0)
            process.assert_not_called()

    def test_sqlite_wal_snapshot_restore_and_existing_target_are_preserved(self):
        if db.using_mysql_backend():
            self.skipTest("SQLite backup check")
        refund_id = self.seed_business_state()
        # Keep WAL open across the snapshot to test committed WAL content.
        connection = sqlite3.connect(db.DB_PATH)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE refund_requests SET reason='wal_snapshot_marker'")
        connection.commit()
        directory = Path(self.tmp.name) / "backup"
        manifest = sqlite_snapshot(db.DB_PATH, directory)
        connection.execute("UPDATE refund_requests SET reason='changed_after_backup'")
        connection.commit()
        restored = Path(self.tmp.name) / "restored.db"
        sqlite_restore(directory, restored)
        target = sqlite3.connect(restored)
        self.addCleanup(target.close)
        self.assertEqual(target.execute("SELECT reason FROM refund_requests WHERE refund_id=?", (refund_id,)).fetchone()[0], "wal_snapshot_marker")
        self.assertEqual(target.execute("SELECT status FROM mq_messages").fetchone()[0], "done")
        self.assertEqual(target.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            sqlite_restore(directory, restored)
        self.assertEqual(connection.execute("SELECT reason FROM refund_requests").fetchone()[0], "changed_after_backup")
        self.assertEqual(manifest["status"], "complete")

    def test_corrupted_backup_is_rejected_before_creating_destination(self):
        if db.using_mysql_backend():
            self.skipTest("SQLite corruption check")
        directory = Path(self.tmp.name) / "backup"
        sqlite_snapshot(db.DB_PATH, directory)
        with (directory / "snapshot.sqlite").open("ab") as output:
            output.write(b"corruption")
        target = Path(self.tmp.name) / "rejected.db"
        with self.assertRaises(ValueError):
            sqlite_restore(directory, target)
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.getenv("CONSISTENCY_MYSQL_DSN"), "requires dedicated MySQL")
    def test_mysql_full_restore_matches_all_tables_and_rejects_existing_database(self):
        self.seed_business_state()
        dsn = os.environ["MYSQL_DSN"]
        directory = Path(self.tmp.name) / "mysql-backup"
        manifest = mysql_snapshot(dsn, directory)
        options = parse_mysql_dsn(dsn)
        target = "support_test_" + uuid4().hex
        def cleanup():
            assert target.startswith("support_test_") and len(target) == 45
            with connect_mysql(options, database=False) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE IF EXISTS `{target}`")
        self.addCleanup(cleanup)
        mysql_restore(directory, dsn, target)
        restored_options = {**options, "database": target}
        with connect_mysql(options) as original, connect_mysql(restored_options) as restored:
            with original.cursor() as left, restored.cursor() as right:
                left.execute("SHOW TABLES")
                tables = [row[0] for row in left.fetchall()]
                self.assertEqual(len(tables), manifest["source"]["tables"])
                for table in tables:
                    left.execute(f"SELECT * FROM `{table}`")
                    right.execute(f"SELECT * FROM `{table}`")
                    self.assertEqual(sorted(left.fetchall(), key=repr), sorted(right.fetchall(), key=repr), table)
        with self.assertRaises(Exception) as error:
            mysql_restore(directory, dsn, target)
        self.assertEqual(error.exception.args[0], 1007)  # MySQL: database already exists.
        self.assertNotIn(options["password"], (directory / "manifest.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
