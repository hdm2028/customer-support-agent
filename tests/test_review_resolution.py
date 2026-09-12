import concurrent.futures
import threading
import multiprocessing
import os
import unittest
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.core.security import Principal, current_principal
from app.mq.queue import consume_messages
from app.services import review_service, refund_service
from app.storage import database as db
from app.tools.human_review import create_manual_review
from app.tools import human_review
from app.tools.refund import refund_apply


def _create_review_in_worker(_):
    fixtures._worker_barrier.wait(timeout=20)
    return create_manual_review("10010", "refund", "high", [], "申请退款").result


class ReviewResolutionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RefundConsistencyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        token = current_principal.set(Principal("operator", "admin"))
        self.addCleanup(current_principal.reset, token)

    def pending(self, order_id="10010", with_refund=True):
        refund = refund_apply(order_id, "我要退款").result if with_refund else None
        review = create_manual_review(order_id, "refund", "high", ["人工复核"], "我要退款",
                                      refund["refund_id"] if refund else None).result
        return review, refund

    def test_approve_resumes_pending_refund_and_late_old_event_does_not_reblock(self):
        review, refund = self.pending()
        old_message = consume_messages()[0]
        result = review_service.resolve_review(review["review_id"], "approve", "已核实订单和申请凭证")
        self.assertEqual(result["refund"]["status"], "queued")
        self.assertTrue(refund_service.process_refund_message(old_message)["business_executed"])
        refund_service.process_refund_tasks()
        self.assertEqual(db.get_refund_request_from_db(refund["refund_id"])["status"], "refund_processing")
        self.assertEqual(db.list_manual_reviews_from_db()[0]["resolution"]["operator_id"], "operator")

    def test_approve_before_refund_creation_creates_one_intent_and_event(self):
        review, _ = self.pending(with_refund=False)
        first = review_service.resolve_review(review["review_id"], "approve", "人工确认可受理")
        second = review_service.resolve_review(review["review_id"], "approve", "重复请求")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)
        self.assertEqual(second["review"]["related_id"], first["refund"]["refund_id"])

    def test_reject_stops_pending_refund_even_when_event_delivered(self):
        review, refund = self.pending()
        review_service.resolve_review(review["review_id"], "reject", "凭证不符")
        result = refund_service.process_refund_tasks()
        self.assertFalse(any(item["business_executed"] for item in result["results"]))
        self.assertEqual(db.get_refund_request_from_db(refund["refund_id"])["status"], "rejected")
        with self.assertRaises(ValueError):
            review_service.resolve_review(review["review_id"], "approve", "改成通过")

    def test_publish_failure_rolls_back_review_and_refund(self):
        review, refund = self.pending()
        with patch.object(review_service, "publish_message", side_effect=ConnectionError("publish failed")):
            with self.assertRaises(ConnectionError):
                review_service.resolve_review(review["review_id"], "approve", "已核实")
        self.assertEqual(db.list_manual_reviews_from_db()[0]["status"], "pending_review")
        self.assertEqual(db.get_refund_request_from_db(refund["refund_id"])["status"], "pending_manual_review")

    def test_unpaid_order_cannot_be_approved_for_refund(self):
        review, _ = self.pending("10005", with_refund=False)
        with self.assertRaisesRegex(ValueError, "支付"):
            review_service.resolve_review(review["review_id"], "approve", "请求退款")
        self.assertEqual(self.fixture.count("refund_requests"), 0)

    def test_customer_cannot_resolve_review(self):
        review, _ = self.pending()
        token = current_principal.set(Principal("u010", "customer"))
        try:
            with self.assertRaises(PermissionError):
                review_service.resolve_review(review["review_id"], "approve", "用户自行批准")
        finally:
            current_principal.reset(token)

    def test_concurrent_decisions_do_not_overwrite_audit(self):
        review, _ = self.pending(with_refund=False)
        def decide(_):
            token = current_principal.set(Principal("operator", "admin"))
            try:
                return review_service.resolve_review(review["review_id"], "approve", "核实通过")
            finally:
                current_principal.reset(token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, range(2)))
        self.assertEqual(sum(not r["idempotent_replay"] for r in results), 1)
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)

    def test_review_captures_runtime_context_through_tool_worker(self):
        from app.agent.agents.after_sales import AfterSalesAgent
        from app.agent.state import AgentState
        from app.core.schemas import RouteDecision, ToolResult
        state = AgentState("conversation-context", "请求人工核实", RouteDecision(order_id="10010", need_refund_request=True),
                           history=[{"role": "user", "content": "先核实资格"}], order=db.get_order_from_db("10010"),
                           tool_results=[ToolResult(tool_name="policy_search", success=True,
                                                    result=[{"chunk_id": "observed-chunk", "text": "已检索到的政策原文"}])])
        result = AfterSalesAgent().create_manual_review(state)
        self.assertTrue(result.success)
        saved = db.list_manual_reviews_from_db()[0]
        self.assertEqual(saved["context"]["conversation_id"], "conversation-context")
        self.assertEqual(saved["context"]["policy_evidence"][0][0]["chunk_id"], "observed-chunk")
        self.assertEqual(saved["context"]["history"], state.history)

    def test_repeated_refund_review_returns_same_pending_item(self):
        first, _ = self.pending(with_refund=False)
        second = create_manual_review("10010", "refund", "high", ["重复请求"], "再次请求退款").result
        self.assertEqual(second["review_id"], first["review_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.fixture.count("manual_reviews"), 1)
        self.assertEqual(db.list_manual_reviews_from_db()[0]["user_request"], "我要退款")

    def test_concurrent_first_refund_reviews_create_one_pending_item(self):
        start = threading.Barrier(4)
        def create(_):
            start.wait(timeout=5)
            return create_manual_review("10010", "refund", "high", ["人工复核"], "申请退款").result
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(4)))
        self.assertEqual(len({r["review_id"] for r in results}), 1)
        self.assertEqual(sum(not r.get("idempotent_replay", False) for r in results), 1)
        self.assertEqual(self.fixture.count("manual_reviews"), 1)
        self.assertEqual(self.fixture.count("refund_requests"), 0)

    def test_first_refund_review_is_unique_across_mysql_processes(self):
        if not db.using_mysql_backend():
            self.skipTest("Dedicated MySQL multi-process check")
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(4)
        with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=context,
                initializer=fixtures._initialize_mysql_worker, initargs=(os.environ["MYSQL_DSN"], barrier)) as pool:
            results = list(pool.map(_create_review_in_worker, range(4)))
        self.assertEqual(len({r["review_id"] for r in results}), 1)
        self.assertEqual(sum(not r["idempotent_replay"] for r in results), 1)
        self.assertEqual(self.fixture.count("manual_reviews"), 1)

    def test_review_after_approval_reports_existing_refund_without_new_review(self):
        review, _ = self.pending(with_refund=False)
        approved = review_service.resolve_review(review["review_id"], "approve", "已核实")
        result = create_manual_review("10010", "refund", "high", [], "再次申请退款").result
        self.assertEqual(result["status"], "not_required")
        self.assertEqual(result["existing_refund"]["refund_id"], approved["refund"]["refund_id"])
        self.assertNotIn("review_id", result)
        self.assertEqual(self.fixture.count("manual_reviews"), 1)
        self.assertEqual(self.fixture.count("refund_requests"), 1)

    def test_rejected_review_does_not_block_a_new_request(self):
        review, _ = self.pending(with_refund=False)
        review_service.resolve_review(review["review_id"], "reject", "本次材料不符")
        new_review = create_manual_review("10010", "refund", "high", [], "补齐材料后申请复核").result
        self.assertNotEqual(new_review["review_id"], review["review_id"])
        self.assertEqual(new_review["status"], "pending_review")
        self.assertEqual(self.fixture.count("manual_reviews"), 2)

    def test_review_creation_failure_does_not_leave_a_reservation(self):
        with patch.object(human_review, "save_manual_review_to_db", side_effect=RuntimeError("insert failed")):
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                self.pending(with_refund=False)
        self.assertEqual(self.fixture.count("manual_reviews"), 0)
        review, _ = self.pending(with_refund=False)
        self.assertTrue(review["review_id"])
        self.assertEqual(self.fixture.count("manual_reviews"), 1)

    def test_repeat_releases_order_lock_before_appending_material(self):
        if not db.using_mysql_backend():
            self.skipTest("MySQL row lock ordering; SQLite serializes writes")
        review, _ = self.pending(with_refund=False)
        held, release, appending = threading.Event(), threading.Event(), threading.Event()
        lock = review_service.lock_record
        append = review_service.append_review_supplement
        def mark_append(*args, **kwargs):
            appending.set()
            return append(*args, **kwargs)
        def probe_order():
            from app.storage.transactions import transaction
            with transaction():
                return lock("orders", "order_id", "10010")
        def gated_lock(table, key, value):
            row = lock(table, key, value)
            if table == "manual_reviews":
                held.set()
                if not release.wait(10):
                    raise TimeoutError("test review gate")
            return row
        def approve():
            token = current_principal.set(Principal("operator", "admin"))
            try:
                return review_service.resolve_review(review["review_id"], "approve", "核实通过")
            finally:
                current_principal.reset(token)
        with patch.object(review_service, "lock_record", side_effect=gated_lock), \
                patch.object(review_service, "append_review_supplement", side_effect=mark_append), \
                concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            approval = pool.submit(approve)
            try:
                self.assertTrue(held.wait(5))
                creation = pool.submit(create_manual_review, "10010", "refund", "high", [], "重复请求")
                self.assertTrue(appending.wait(5))
                self.assertTrue(pool.submit(probe_order).result(timeout=5))
            finally:
                release.set()
            self.assertTrue(approval.result(timeout=5)["success"])
            repeated = creation.result(timeout=5)
            self.assertTrue(repeated.result["supplement_recorded"])
            self.assertEqual(repeated.result["status"], "not_required")
        self.assertEqual(self.fixture.count("manual_reviews"), 1)
        self.assertEqual(self.fixture.count("refund_requests"), 1)

    def test_supplements_preserve_original_and_are_idempotent(self):
        review, _ = self.pending(with_refund=False)
        first = review_service.append_review_supplement(review["review_id"], "补充：包装已破损", "submission-1")
        second = review_service.append_review_supplement(review["review_id"], "补充：包装已破损", "submission-1")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["supplement_version"], 1)
        saved = review_service.review_details(review["review_id"])
        self.assertEqual(saved["user_request"], "我要退款")
        self.assertEqual(len(saved["supplements"]), 1)
        with self.assertRaisesRegex(ValueError, "提交编号"):
            review_service.append_review_supplement(review["review_id"], "不同内容", "submission-1")

    def test_new_material_prevents_decision_from_stale_page(self):
        review, _ = self.pending(with_refund=False)
        review_service.append_review_supplement(review["review_id"], "新的凭证说明", "submission-1")
        for version in [None, 0]:
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "材料已有更新"):
                review_service.resolve_review(review["review_id"], "approve", "旧页面决定", supplement_version=version)
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        approved = review_service.resolve_review(review["review_id"], "approve", "已查看补充说明", supplement_version=1)
        self.assertEqual(approved["review"]["resolution"]["supplement_version"], 1)

    def test_post_decision_supplement_does_not_rewrite_resolution(self):
        review, _ = self.pending(with_refund=False)
        approved = review_service.resolve_review(review["review_id"], "approve", "已核实")
        result = review_service.append_review_supplement(review["review_id"], "审核后补充说明", "submission-1")
        self.assertEqual(result["submission"]["review_status_at_submission"], "approved")
        saved = review_service.review_details(review["review_id"])
        self.assertEqual(saved["resolution"], approved["review"]["resolution"])
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(self.fixture.count("refund_requests"), 1)

    def test_concurrent_supplements_do_not_lose_material(self):
        review, _ = self.pending(with_refund=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: review_service.append_review_supplement(
                review["review_id"], f"补充说明 {i}", f"submission-{i}"), range(4)))
        self.assertEqual({r["supplement_version"] for r in results}, {1, 2, 3, 4})
        self.assertEqual(len(review_service.review_details(review["review_id"])["supplements"]), 4)

    def test_concurrent_retries_of_same_supplement_are_idempotent(self):
        review, _ = self.pending(with_refund=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: review_service.append_review_supplement(
                review["review_id"], "同一次补充提交", "submission-retry"), range(4)))
        self.assertEqual(sum(not r["idempotent_replay"] for r in results), 1)
        self.assertEqual({r["supplement_version"] for r in results}, {1})
        self.assertEqual(len(review_service.review_details(review["review_id"])["supplements"]), 1)

    def test_failed_supplement_write_can_retry_without_partial_version(self):
        review, _ = self.pending(with_refund=False)
        with patch.object(review_service, "execute", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                review_service.append_review_supplement(review["review_id"], "补充说明", "submission-retry")
        saved = review_service.review_details(review["review_id"])
        self.assertEqual(saved.get("supplement_version", 0), 0)
        result = review_service.append_review_supplement(review["review_id"], "补充说明", "submission-retry")
        self.assertEqual(result["supplement_version"], 1)


if __name__ == "__main__":
    unittest.main()
