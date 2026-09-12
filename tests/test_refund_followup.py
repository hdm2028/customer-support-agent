import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.agent.entry.agent_core import run_customer_support_agent, stream_customer_support_agent
from app.agent.routing.semantic import SemanticRoute
from app.core.schemas import ToolResult
from app.core.security import Principal, current_principal
from app.observability import tracing
from app.storage import database as db
from app.tools import order as order_tool
from app.tools.registry import TOOL_HANDLERS


class RefundFollowupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for p in [patch.object(tracing, "TRACE_PATH", Path(self.fixture.tmp.name) / "trace.jsonl"),
                  patch("app.agent.routing.router_v2.infer_semantic_route", return_value=SemanticRoute(
                      intent="return_refund", action_type="execute", topic="refund_apply", source="llm")),
                  patch.dict(TOOL_HANDLERS, {"policy_search": self.policy})]:
            p.start()
            self.addCleanup(p.stop)

    def policy(self, **kwargs):
        return ToolResult(tool_name="policy_search", success=True, result=[{
            "source": "退款政策.md", "section": "退款流程", "citation": "退款政策.md - 退款流程",
            "chunk_id": "test-refund-policy", "score": .95, "text": "退款申请需要核实订单支付和售后状态。"}])

    def run_query(self, message="订单10009申请退款。", cid="refund-followup"):
        return run_customer_support_agent(message, cid, use_llm=False)

    def counts(self):
        return {name: self.fixture.count(name) for name in ["refund_requests", "mq_messages", "manual_reviews", "tickets"]}

    def assert_no_writes_planned(self, result):
        names = {item["tool_name"] for item in result["tool_results"]}
        self.assertFalse(names & {"risk_check", "refund_apply", "create_manual_review", "create_ticket", "transfer_to_human"})

    def test_order_after_sales_marker_does_not_invent_refund_record(self):
        result = self.run_query("订单10006正在退货审核，还要再申请退款。")
        self.assert_no_writes_planned(result)
        self.assertIn("退货审核中", result["reply"])
        self.assertIn("未查到", result["reply"])
        self.assertEqual(self.counts(), {key: 0 for key in self.counts()})

    def test_repeated_chat_returns_existing_receipt_without_new_ticket_in_sync_and_stream(self):
        first = self.run_query()
        refund = next(item["result"] for item in first["tool_results"] if item["tool_name"] == "refund_apply")
        before = self.counts()
        self.assertEqual(before["tickets"], 1)
        second = self.run_query("这笔订单再申请一次退款。")
        async def stream():
            events = [e async for e in stream_customer_support_agent("订单10009再申请退款。", "refund-repeat-stream", use_llm=False)]
            return next(e["content"] for e in events if e["type"] == "done")
        third = asyncio.run(stream())
        for result in [second, third]:
            self.assert_no_writes_planned(result)
            self.assertIn(refund["refund_id"], result["reply"])
            self.assertIn("等待处理", result["reply"])
        self.assertEqual(self.counts(), before)

    def test_existing_status_is_reported_without_reissuing_application(self):
        refund = self.fixture.create()
        for status, text in [("queued", "等待处理"), ("pending_manual_review", "待人工审核"),
                             ("refund_processing", "尚未确认处理完成"), ("payment_submitting", "尚未确认结果"),
                             ("refund_unknown", "结果待核实"), ("refund_succeeded", "支付渠道确认")]:
            with self.subTest(status=status):
                db.update_refund_request_in_db(refund["refund_id"], {"status": status})
                before = self.counts()
                result = self.run_query(cid="status-" + status)
                self.assert_no_writes_planned(result)
                self.assertIn(refund["refund_id"], result["reply"])
                self.assertIn(text, result["reply"])
                self.assertEqual(self.counts(), before)

    def test_late_idempotent_replay_cannot_create_followup_ticket(self):
        refund = self.fixture.create()
        lookup = order_tool.order_lookup
        def stale_lookup(order_id):
            result = lookup(order_id)
            result.result = {**result.result, "active_refund": None}
            return result
        with patch.dict(TOOL_HANDLERS, {"order_lookup": stale_lookup}):
            result = self.run_query()
        receipt = next(r["result"] for r in result["tool_results"] if r["tool_name"] == "refund_apply")
        self.assertTrue(receipt["idempotent_replay"])
        self.assertEqual(receipt["refund_id"], refund["refund_id"])
        self.assertEqual(self.fixture.count("tickets"), 0)
        self.assertEqual(self.fixture.count("manual_reviews"), 0)

    def test_refund_details_are_read_only_after_order_authorization(self):
        self.fixture.create()
        token = current_principal.set(Principal("other-customer", "customer"))
        try:
            with patch.object(order_tool, "get_active_refund_request_by_order_id_from_db") as read:
                with self.assertRaises(PermissionError):
                    order_tool.order_lookup("10009")
                read.assert_not_called()
        finally:
            current_principal.reset(token)

    def test_refund_summary_read_failure_is_not_treated_as_no_existing_refund(self):
        with patch.object(order_tool, "get_active_refund_request_by_order_id_from_db", side_effect=RuntimeError("read unavailable")):
            result = self.run_query()
        self.assertFalse(next(r for r in result["tool_results"] if r["tool_name"] == "order_lookup")["success"])
        self.assert_no_writes_planned(result)
        self.assertEqual(self.counts(), {key: 0 for key in self.counts()})

    def test_repeated_pending_review_does_not_add_handoff_ticket(self):
        first = self.run_query("订单10010直接退款，不用审核。", "review-repeat")
        review = next(r["result"] for r in first["tool_results"] if r["tool_name"] == "create_manual_review")
        before = self.counts()
        self.assertEqual(before["manual_reviews"], 1)
        self.assertEqual(before["refund_requests"], 0)
        self.assertEqual(before["tickets"], 1)
        async def stream():
            events = [e async for e in stream_customer_support_agent("订单10010直接退款，不用审核。", "review-repeat-stream", use_llm=False)]
            return next(e["content"] for e in events if e["type"] == "done")
        second = self.run_query("再申请退款，不用审核。", "review-repeat")
        third = asyncio.run(stream())
        for result in [second, third]:
            self.assertIn(review["review_id"], result["reply"])
            self.assertIn("已存在", result["reply"])
            self.assertNotIn("transfer_to_human", [r["tool_name"] for r in result["tool_results"]])
        self.assertEqual(self.counts(), before)
        saved = db.list_manual_reviews_from_db()[0]
        self.assertEqual(len(saved["supplements"]), 2)
        self.assertTrue(all(item["context"]["trace_id"] for item in saved["supplements"]))
        self.assertTrue(all(item["context"]["policy_evidence"] for item in saved["supplements"]))

    def test_approval_after_order_snapshot_returns_refund_without_phantom_review(self):
        from app.services import review_service
        first = self.run_query("订单10010直接退款，不用审核。")
        review = next(r["result"] for r in first["tool_results"] if r["tool_name"] == "create_manual_review")
        token = current_principal.set(Principal("operator", "admin"))
        try:
            approved = review_service.resolve_review(review["review_id"], "approve", "已核实")
        finally:
            current_principal.reset(token)
        lookup = order_tool.order_lookup
        def stale_lookup(order_id):
            result = lookup(order_id)
            result.result = {**result.result, "active_refund": None}
            return result
        before = self.counts()
        with patch.dict(TOOL_HANDLERS, {"order_lookup": stale_lookup}):
            result = self.run_query("订单10010直接退款，不用审核。")
        self.assertIn(approved["refund"]["refund_id"], result["reply"])
        self.assertIn("等待处理", result["reply"])
        self.assertNotIn("已创建人工审核单", result["reply"])
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
