import concurrent.futures
from decimal import Decimal
import threading
import unittest
from unittest.mock import Mock, patch

import test_refund_consistency as fixtures
from app.core.security import Principal, current_principal
from app.services import payments, refund_service
from app.storage import database as db
from app.tools import refund as refund_tool
from app.domain.refund_policy import evaluate_refund_eligibility


class PaymentReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        token = current_principal.set(Principal("operator", "admin"))
        self.addCleanup(current_principal.reset, token)
        self.refund = self.fixture.create()
        refund_service.process_refund_tasks()
        self.gateway = Mock()
        gateway_patch = patch.object(payments, "get_payment_gateway", return_value=self.gateway)
        gateway_patch.start()
        self.addCleanup(gateway_patch.stop)

    def observation(self, state="succeeded", amount=None):
        return payments.PaymentObservation(self.refund["refund_id"], state,
            Decimal(str(self.refund["amount"])) if amount is None else amount, "CNY", "sandbox-receipt")

    def status(self):
        return db.get_refund_request_from_db(self.refund["refund_id"])["status"]

    def test_confirmed_success_updates_order_and_repeated_submit_is_noop(self):
        self.gateway.submit_refund.return_value = self.observation()
        result = payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(result["refund"]["status"], "refund_succeeded")
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "refund_succeeded")
        count = self.fixture.count("notifications")
        self.assertTrue(payments.submit_refund_payment(self.refund["refund_id"])["idempotent_replay"])
        self.assertEqual(self.gateway.submit_refund.call_count, 1)
        self.assertEqual(self.fixture.count("notifications"), count)

    def test_timeout_requires_query_and_preserves_same_idempotency_key(self):
        self.gateway.submit_refund.side_effect = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "refund_unknown")
        with self.assertRaises(ValueError):
            payments.submit_refund_payment(self.refund["refund_id"])
        self.gateway.query_refund.return_value = self.observation()
        payments.reconcile_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "refund_succeeded")
        self.assertEqual(self.gateway.submit_refund.call_args.args[0].idempotency_key,
                         self.gateway.query_refund.call_args.args[0].idempotency_key)
        self.assertEqual(self.gateway.submit_refund.call_count, 1)

    def test_amount_mismatch_does_not_record_success(self):
        self.gateway.submit_refund.return_value = self.observation(amount=Decimal("1.00"))
        with self.assertRaises(ValueError):
            payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "refund_unknown")
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "refund_processing")

    def test_known_failure_is_queryable_but_not_blindly_resubmitted(self):
        self.gateway.submit_refund.return_value = self.observation("failed")
        payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "failed")
        payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.gateway.submit_refund.call_count, 1)

    def test_unconfigured_gateway_does_not_mutate_refund(self):
        with patch.object(payments, "get_payment_gateway", side_effect=payments.PaymentNotConfigured("not configured")):
            with self.assertRaises(payments.PaymentNotConfigured):
                payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "refund_processing")

    def test_submit_rechecks_confirmed_payment_before_dispatch(self):
        for payment_status in ["unpaid", "payment_pending", "pending", "failed", "unknown"]:
            with self.subTest(payment_status=payment_status):
                db.update_order_in_db("10009", {"payment_status": payment_status})
                before = db.get_refund_request_from_db(self.refund["refund_id"])
                with self.assertRaisesRegex(ValueError, "支付"):
                    payments.submit_refund_payment(self.refund["refund_id"])
                self.gateway.submit_refund.assert_not_called()
                self.assertEqual(db.get_refund_request_from_db(self.refund["refund_id"]), before)

    def test_reconciliation_remains_available_after_payment_status_changes(self):
        self.gateway.submit_refund.side_effect = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            payments.submit_refund_payment(self.refund["refund_id"])
        db.update_order_in_db("10009", {"payment_status": "pending"})
        self.gateway.query_refund.return_value = self.observation()
        self.assertEqual(payments.reconcile_refund_payment(self.refund["refund_id"])["refund"]["status"], "refund_succeeded")
        self.assertEqual(self.gateway.submit_refund.call_count, 1)
        self.assertEqual(self.gateway.query_refund.call_count, 1)

    def test_db_failure_after_channel_success_is_reconciled_without_resubmit(self):
        self.gateway.submit_refund.return_value = self.observation()
        with patch.object(payments, "save_notification_to_db", side_effect=RuntimeError("DB failure")):
            with self.assertRaises(RuntimeError):
                payments.submit_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "refund_unknown")
        self.assertEqual(db.get_order_from_db("10009")["after_sales_status"], "refund_processing")
        self.gateway.query_refund.return_value = self.observation()
        payments.reconcile_refund_payment(self.refund["refund_id"])
        self.assertEqual(self.status(), "refund_succeeded")
        self.assertEqual(self.gateway.submit_refund.call_count, 1)

    def test_concurrent_submit_calls_gateway_once(self):
        entered, release = threading.Event(), threading.Event()
        def dispatch(request):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.observation()
        self.gateway.submit_refund.side_effect = dispatch
        def submit():
            token = current_principal.set(Principal("operator", "admin"))
            try:
                return payments.submit_refund_payment(self.refund["refund_id"])
            finally:
                current_principal.reset(token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(ValueError):
                    submit()
            finally:
                release.set()
            self.assertTrue(first.result()["success"])
        self.assertEqual(self.gateway.submit_refund.call_count, 1)


class RefundPaymentPreconditionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_unconfirmed_payment_never_creates_refund_or_event(self):
        for payment_status in ["unpaid", "payment_pending", "pending", "failed", "unknown", ""]:
            with self.subTest(payment_status=payment_status):
                db.update_order_in_db("10009", {"payment_status": payment_status})
                result = refund_tool.refund_apply("10009", "商品破损，申请退款")
                self.assertFalse(result.success)
                self.assertFalse(result.result["review_required"])
                self.assertIn("支付", result.result["reason"])
                self.assertEqual(self.fixture.count("refund_requests"), 0)
                self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_missing_payment_snapshot_is_ineligible_without_risk_lookup(self):
        # MySQL forbids NULL payment_status at storage level; incomplete
        # adapter/cache snapshots must also fail closed at the domain boundary.
        for order in [{}, {"payment_status": None}]:
            with self.subTest(order=order), patch(
                "app.domain.refund_policy.get_customer_profile_from_db", side_effect=AssertionError("no risk lookup")
            ):
                result = evaluate_refund_eligibility(order, "申请退款")
                self.assertFalse(result["eligible"])
                self.assertFalse(result["review_required"])

    def test_stale_paid_snapshot_is_rechecked_in_creation_transaction(self):
        stale = db.get_order_from_db("10009")
        self.assertEqual(stale["payment_status"], "paid")
        db.update_order_in_db("10009", {"payment_status": "pending"})
        with patch.object(refund_tool, "get_order_by_id", return_value=stale):
            result = refund_tool.refund_apply("10009", "商品破损，申请退款")
        self.assertFalse(result.success)
        self.assertFalse(result.result["review_required"])
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_confirmed_payment_can_retry_after_initial_rejection(self):
        db.update_order_in_db("10009", {"payment_status": "pending"})
        self.assertFalse(refund_tool.refund_apply("10009", "申请退款").success)
        db.update_order_in_db("10009", {"payment_status": "paid"})
        first = refund_tool.refund_apply("10009", "申请退款")
        second = refund_tool.refund_apply("10009", "申请退款")
        self.assertTrue(first.success)
        self.assertEqual(first.result["refund_id"], second.result["refund_id"])
        self.assertTrue(second.result["idempotent_replay"])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)


if __name__ == "__main__":
    unittest.main()
