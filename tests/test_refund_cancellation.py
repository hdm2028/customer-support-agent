import concurrent.futures
import unittest
from threading import Event, Barrier
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.core.security import Principal, current_principal
from app.mq.queue import consume_messages
from app.services import refund_service, review_service, payments
from app.services.refund_cancellation import cancel_refund
from app.storage import database as db
from app.tools.refund import refund_apply
from app.tools.human_review import create_manual_review


class RefundCancellationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        token = current_principal.set(Principal("u009", "customer"))
        self.addCleanup(current_principal.reset, token)

    def create(self):
        result = refund_apply("10009", "申请退款")
        self.assertTrue(result.success)
        return result.result["refund_id"]

    def test_cancel_queued_is_idempotent_and_late_message_cannot_advance(self):
        refund_id = self.create()
        message = consume_messages()[0]
        result = cancel_refund(refund_id, "暂不退款")
        self.assertEqual(result["refund"]["status"], "cancelled")
        self.assertTrue(cancel_refund(refund_id, "重复请求")["idempotent_replay"])
        self.assertFalse(refund_service.process_refund_message(message)["business_executed"])
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "none")
        self.assertEqual(self.fixture.count("notifications"), 1)

    def test_cancel_processing_restores_order_only_before_payment_dispatch(self):
        before = db.get_order_from_db("10009")
        refund_id = self.create()
        refund_service.process_refund_tasks()
        cancel_refund(refund_id, "撤销申请")
        after = db.get_order_from_db("10009")
        self.assertEqual(after["order_status"], before["order_status"])
        self.assertEqual(after["after_sales_status"], before["after_sales_status"])
        token = current_principal.set(Principal("operator", "admin"))
        self.addCleanup(current_principal.reset, token)
        with patch.object(payments, "get_payment_gateway") as gateway:
            with self.assertRaises(ValueError):
                payments.submit_refund_payment(refund_id)
            gateway.return_value.submit_refund.assert_not_called()

    def test_newer_shipping_state_or_legacy_missing_snapshot_is_not_overwritten(self):
        refund_id = self.create()
        refund_service.process_refund_tasks()
        db.update_order_in_db("10009", {"shipping_status": "仓库刚完成出库"})
        with self.assertRaisesRegex(ValueError, "状态已变化"):
            cancel_refund(refund_id, "撤销")
        self.assertEqual(db.get_order_from_db("10009")["shipping_status"], "仓库刚完成出库")
        db.update_refund_request_in_db(refund_id, {"order_before_refund": None})
        with self.assertRaises(ValueError):
            cancel_refund(refund_id, "撤销")

    def test_payment_unknown_or_success_cannot_be_cancelled(self):
        refund_id = self.create()
        for status in ("payment_submitting", "refund_unknown", "refund_succeeded"):
            db.update_refund_request_in_db(refund_id, {"status": status})
            with self.assertRaises(ValueError):
                cancel_refund(refund_id, "撤销")
            self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], status)

    def test_other_user_cannot_cancel_or_read_back_the_refund(self):
        refund_id = self.create()
        token = current_principal.set(Principal("u001", "customer"))
        try:
            with self.assertRaises(PermissionError):
                cancel_refund(refund_id, "越权撤销")
        finally:
            current_principal.reset(token)
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "queued")

    def test_review_cancellation_and_notification_failure_are_atomic(self):
        refund_id = self.create()
        db.update_refund_request_in_db(refund_id, {"status": "pending_manual_review"})
        review = create_manual_review("10009", "refund", "high", [], "退款", refund_id).result
        with patch("app.services.refund_cancellation.save_notification_to_db", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                cancel_refund(refund_id, "撤销")
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "pending_manual_review")
        self.assertEqual(db.list_manual_reviews_from_db()[0]["status"], "pending_review")
        cancel_refund(refund_id, "撤销")
        self.assertEqual(db.list_manual_reviews_from_db()[0]["status"], "cancelled")
        token = current_principal.set(Principal("operator", "admin"))
        self.addCleanup(current_principal.reset, token)
        with self.assertRaises(ValueError):
            review_service.resolve_review(review["review_id"], "approve", "晚到的批准")

    def test_consumer_racing_cancellation_leaves_cancelled_refund_and_original_order(self):
        refund_id = self.create()
        message = consume_messages()[0]
        def cancel():
            token = current_principal.set(Principal("u009", "customer"))
            try:
                return cancel_refund(refund_id, "撤销")
            finally:
                current_principal.reset(token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(refund_service.process_refund_message, message)
            second = pool.submit(cancel)
            first.result(timeout=15)
            second.result(timeout=15)
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "cancelled")
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "none")

    def test_approval_racing_cancellation_cannot_reactivate_the_refund(self):
        refund_id = self.create()
        db.update_refund_request_in_db(refund_id, {"status": "pending_manual_review"})
        review = create_manual_review("10009", "refund", "high", [], "退款", refund_id).result
        barrier = Barrier(2)
        def act(approve):
            token = current_principal.set(Principal("operator", "admin") if approve else Principal("u009", "customer"))
            try:
                barrier.wait(timeout=10)
                if approve:
                    try:
                        return review_service.resolve_review(review["review_id"], "approve", "核实通过")
                    except ValueError:
                        return None  # Cancellation completed before approval.
                return cancel_refund(refund_id, "撤销")
            finally:
                current_principal.reset(token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(act, (True, False)))
        refund_service.process_refund_tasks()
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "cancelled")
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "none")

    def test_mysql_approval_holding_review_does_not_deadlock_cancellation_scan(self):
        if not db.using_mysql_backend():
            self.skipTest("MySQL row/index locking schedule")
        from app.services import refund_cancellation
        refund_id = self.create()
        db.update_refund_request_in_db(refund_id, {"status": "pending_manual_review"})
        review = create_manual_review("10009", "refund", "high", [], "退款", refund_id).result
        locked, scanned = Event(), Event()
        original_lock, original_execute = review_service.lock_record, refund_cancellation.execute
        def hold_review(table, key, value):
            row = original_lock(table, key, value)
            if table == "manual_reviews":
                locked.set()
                if not scanned.wait(5):
                    raise TimeoutError("cancellation scan blocked on approval's review row")
            return row
        def observe_scan(sql, *args, **kwargs):
            rows = original_execute(sql, *args, **kwargs)
            if sql.startswith("SELECT") and "FROM manual_reviews" in sql:
                scanned.set()
            return rows
        def approve():
            token = current_principal.set(Principal("operator", "admin"))
            try:
                return review_service.resolve_review(review["review_id"], "approve", "核实通过")
            finally:
                current_principal.reset(token)
        def cancel():
            token = current_principal.set(Principal("u009", "customer"))
            try:
                if not locked.wait(5):
                    raise TimeoutError("approval did not acquire its review row")
                return cancel_refund(refund_id, "撤销")
            finally:
                current_principal.reset(token)
        with patch.object(review_service, "lock_record", side_effect=hold_review), \
                patch.object(refund_cancellation, "execute", side_effect=observe_scan):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                a, b = pool.submit(approve), pool.submit(cancel)
                self.assertTrue(a.result(timeout=15)["success"])
                self.assertTrue(b.result(timeout=15)["success"])
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "cancelled")
        self.assertEqual(db.list_manual_reviews_from_db()[0]["status"], "approved")
        refund_service.process_refund_tasks()
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "cancelled")

    def test_cancellation_after_payment_dispatch_is_rejected_while_channel_is_in_flight(self):
        refund_id = self.create()
        refund_service.process_refund_tasks()
        entered, release = Event(), Event()
        def submit(request):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("fixture release timeout")
            return payments.PaymentObservation(request.refund_id, "processing", request.amount, request.currency)
        def run():
            token = current_principal.set(Principal("operator", "admin"))
            try:
                return payments.submit_refund_payment(refund_id)
            finally:
                current_principal.reset(token)
        with patch.object(payments, "get_payment_gateway") as gateway, concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            gateway.return_value.submit_refund.side_effect = submit
            future = pool.submit(run)
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(ValueError):
                    cancel_refund(refund_id, "撤销")
            finally:
                release.set()
            future.result(timeout=10)
            gateway.return_value.submit_refund.assert_called_once()
        self.assertEqual(db.get_refund_request_from_db(refund_id)["status"], "payment_submitting")


if __name__ == "__main__":
    unittest.main()
