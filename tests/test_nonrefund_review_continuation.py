import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.core.security import Principal, current_principal
from app.services.review_service import resolve_review
from app.services import review_continuation, refund_service
from app.storage import database as db
from app.tools.human_review import create_manual_review
from app.agent.entry.agent_core import run_customer_support_agent, stream_customer_support_agent
from app.agent.routing.semantic import SemanticRoute
from app.observability import tracing


class NonRefundReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        trace_path = patch.object(tracing, "TRACE_PATH", Path(self.fixture.tmp.name) / "trace.jsonl")
        trace_path.start()
        self.addCleanup(trace_path.stop)
        token = current_principal.set(Principal("operator", "admin"))
        self.addCleanup(current_principal.reset, token)

    def review(self, action, order_id="10009"):
        return create_manual_review(order_id, action, "high", ["需核实"], "用户请求人工继续处理").result

    def user_request(self, action, message, *, user_id="u009", query=False, streamed=False):
        semantic = SemanticRoute(intent=action, action_type="query" if query else "execute",
                                 topic=("cancel_policy" if action == "cancel_order" else "address_change_policy") if query
                                 else ("cancel_apply" if action == "cancel_order" else "address_change_apply"), source="llm")
        token = current_principal.set(Principal(user_id, "customer"))
        try:
            with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=semantic):
                if not streamed:
                    return run_customer_support_agent(message, use_llm=False)
                async def collect():
                    events = [event async for event in stream_customer_support_agent(message, use_llm=False)]
                    result = next(e["content"] for e in events if e["type"] == "done")
                    self.assertEqual([e["content"] for e in events if e["type"] == "message"], [result["reply"]])
                    return result
                return asyncio.run(collect())
        finally:
            current_principal.reset(token)

    def requested_review(self, result, action):
        tools = result["tool_results"]
        names = [item["tool_name"] for item in tools]
        self.assertIn("risk_check", names)
        self.assertIn("create_manual_review", names)
        self.assertLess(names.index("risk_check"), names.index("create_manual_review"))
        review = next(item["result"] for item in tools if item["tool_name"] == "create_manual_review")
        self.assertEqual(review["review_type"], action)
        self.assertEqual(review["status"], "pending_review")
        self.assertEqual(review["context"]["route"]["intent"], action)
        self.assertIn(review["review_id"], result["reply"])
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        return review

    def test_semantic_cancel_alias_reaches_review_and_unpaid_approval(self):
        before = db.get_order_from_db("10005")
        result = self.user_request("cancel_order", "订单10005这单我不买了，帮我撤掉。", user_id="u005")
        review = self.requested_review(result, "cancel_order")
        self.assertEqual(review["user_id"], "u005")
        self.assertEqual(db.get_order_from_db("10005"), before)
        resolved = resolve_review(review["review_id"], "approve", "已核实用户要求停止未付款订单")
        self.assertEqual(resolved["review"]["continuation"]["action"], "cancel_order")
        self.assertEqual(db.get_order_from_db("10005")["order_status"], "已取消")
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_semantic_paid_cancel_alias_approval_creates_one_refund_event(self):
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        result = self.user_request("cancel_order", "帮我撤销10009这单。", streamed=True)
        review = self.requested_review(result, "cancel_order")
        self.assertEqual(db.get_order_from_db("10009")["order_status"], "待发货")
        resolve_review(review["review_id"], "approve", "已向用户核实撤单并确认未发货")
        replay = resolve_review(review["review_id"], "approve", "同一次确认重试")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)
        self.assertEqual(db.list_refund_requests_from_db()[0]["status"], "queued")

    def test_semantic_address_alias_reaches_review_then_updates_confirmed_address(self):
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        before = db.get_order_from_db("10009")
        address = "测试市向阳路18号"
        result = self.user_request("address_change", "订单10009收件地点换成" + address, streamed=True)
        review = self.requested_review(result, "address_change")
        self.assertEqual(db.get_order_from_db("10009"), before)
        self.assertIn(address, review["user_request"])
        resolve_review(review["review_id"], "approve", "已与用户核实新地址和未发货状态", new_address=address)
        self.assertEqual(db.get_order_from_db("10009")["shipping_address"], address)
        self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_semantic_change_review_rejection_and_late_shipping_do_not_change_order(self):
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        review = self.requested_review(self.user_request("cancel_order", "订单10009我想撤销这单。"), "cancel_order")
        before = db.get_order_from_db("10009")
        resolve_review(review["review_id"], "reject", "用户确认保留原订单")
        self.assertEqual(db.get_order_from_db("10009"), before)
        second = self.requested_review(self.user_request("address_change", "订单10009以后寄到测试市新路19号"), "address_change")
        db.update_order_in_db("10009", {"shipping_status": "已发货", "order_status": "已发货"})
        with self.assertRaises(ValueError):
            resolve_review(second["review_id"], "approve", "页面打开后已发货", new_address="测试市新路19号")
        self.assertEqual(db.get_order_from_db("10009").get("shipping_address"), before.get("shipping_address"))
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_semantic_change_cannot_open_review_for_another_users_order(self):
        result = self.user_request("cancel_order", "帮我撤销10005这单。")
        self.assertEqual([item["tool_name"] for item in result["tool_results"]], ["order_lookup"])
        self.assertFalse(result["tool_results"][0]["success"])
        self.assertEqual(self.fixture.count("manual_reviews"), 0)
        self.assertEqual(self.fixture.count("tickets"), 0)

    def test_unpaid_cancellation_completes_without_refund(self):
        review = self.review("cancel_order", "10005")
        result = resolve_review(review["review_id"], "approve", "用户确认取消，订单未支付未发货")
        self.assertEqual(result["review"]["continuation"]["action"], "cancel_order")
        self.assertEqual(db.get_order_from_db("10005")["order_status"], "已取消")
        self.assertEqual(self.fixture.count("refund_requests"), 0)

    def test_paid_cancellation_queues_one_refund_and_does_not_claim_settlement(self):
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        review = self.review("cancel_order")
        first = resolve_review(review["review_id"], "approve", "已确认停止发货")
        self.assertTrue(resolve_review(review["review_id"], "approve", "重复提交")["idempotent_replay"])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)
        self.assertIn("等待处理", first["review"]["next_step"])
        refund_service.process_refund_tasks()
        self.assertEqual(db.list_refund_requests_from_db()[0]["status"], "refund_processing")
        self.assertTrue(db.get_order_from_db("10009")["fulfillment_cancelled"])

    def test_paid_cancellation_event_failure_rolls_back_review_order_and_refund(self):
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        review = self.review("cancel_order")
        with patch.object(review_continuation, "publish_message", side_effect=ConnectionError("injected")):
            with self.assertRaises(ConnectionError):
                resolve_review(review["review_id"], "approve", "确认取消")
        self.assertEqual(db.get_order_from_db("10009")["order_status"], "待发货")
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(db.list_manual_reviews_from_db()[0]["status"], "pending_review")

    def test_address_requires_confirmed_value_and_preserves_first_completed_change(self):
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        review = self.review("address_change")
        with self.assertRaisesRegex(ValueError, "新收货地址"):
            resolve_review(review["review_id"], "approve", "核实可修改")
        result = resolve_review(review["review_id"], "approve", "用户确认地址", new_address="测试省测试市新收货地址 18 号")
        self.assertEqual(db.get_order_from_db("10009")["shipping_address"], "测试省测试市新收货地址 18 号")
        resolve_review(review["review_id"], "approve", "重复", new_address="测试省测试市其他地址 19 号")
        self.assertEqual(db.get_order_from_db("10009")["shipping_address"], result["review"]["continuation"]["new_address"])

    def test_picking_or_shipped_order_blocks_cancel_and_address_change(self):
        for order_id in ("10009", "10002"):
            for action in ("cancel_order", "address_change"):
                review = self.review(action, order_id)
                with self.assertRaises(ValueError):
                    resolve_review(review["review_id"], "approve", "尝试处理", new_address="测试省测试市新地址 18 号" if action == "address_change" else None)
        self.assertEqual(self.fixture.count("refund_requests"), 0)

    def test_generic_risk_review_creates_auditable_followup_once(self):
        review = self.review("risk_control")
        first = resolve_review(review["review_id"], "approve", "已核实，转专人跟进")
        resolve_review(review["review_id"], "approve", "重复")
        self.assertEqual(self.fixture.count("tickets"), 1)
        ticket = db.list_tickets_from_db()[0]
        self.assertEqual(ticket["review_id"], review["review_id"])
        self.assertEqual(first["review"]["continuation"]["ticket_id"], ticket["ticket_id"])


if __name__ == "__main__":
    unittest.main()
