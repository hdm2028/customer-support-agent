import unittest

from app.agent.response.fallback import build_fallback_answer
from app.agent.entry.workflow import should_force_fallback
from app.core.schemas import RouteDecision, ToolResult
from app.tools.executor import safe_tool_call


class RefundReplyStateTests(unittest.TestCase):
    def answer(self, status, replay=False):
        return build_fallback_answer(RouteDecision(), [ToolResult(tool_name="refund_apply", success=True,
            result={"refund_id": "R-example", "status": status, "mq_message_id": "MQ-private", "idempotent_replay": replay})])

    def test_queued_is_acceptance_not_settlement(self):
        reply = self.answer("queued")
        self.assertIn("等待处理", reply)
        self.assertNotIn("MQ", reply)
        self.assertNotIn("已为您退款", reply)

    def test_replayed_processing_does_not_claim_new_creation(self):
        reply = self.answer("refund_processing", replay=True)
        self.assertIn("尚未确认处理完成", reply)
        self.assertNotIn("已创建", reply)

    def test_manual_review_is_not_presented_as_automatic_processing(self):
        self.assertIn("待人工审核", self.answer("pending_manual_review"))
        self.assertNotIn("等待处理", self.answer("pending_manual_review"))

    def test_failure_and_unknown_state_do_not_claim_success(self):
        self.assertIn("处理失败", self.answer("failed"))
        self.assertIn("需要进一步核实", self.answer("unrecognized"))

    def test_policy_prose_cannot_override_an_actual_refund_outcome(self):
        policy = ToolResult(tool_name="policy_search", success=True,
            result=[{"text": "进入 MQ 队列。某些商品不支持退款。", "citation": "测试政策"}])
        for success, data in ((True, {"refund_id": "R", "status": "queued"}),
                              (False, {"reason": "订单尚未支付，不会产生退款。"})):
            normalized = safe_tool_call("refund_apply", lambda: ToolResult(tool_name="refund_apply", success=success, result=data))
            results = [policy, normalized]
            self.assertTrue(should_force_fallback(RouteDecision(), results))
            reply = build_fallback_answer(RouteDecision(), results)
            self.assertNotIn("MQ", reply)
            self.assertNotIn("某些商品", reply)
            self.assertIn("等待处理" if success else "尚未支付", reply)

    def test_runtime_failure_is_not_reported_as_confirmed_business_rejection(self):
        def fail():
            raise RuntimeError("simulated DB failure after dispatch")
        failed = safe_tool_call("refund_apply", fail)
        self.assertEqual(failed.result["failure_origin"], "runtime_exception")
        reply = build_fallback_answer(RouteDecision(), [failed])
        self.assertIn("未能确认", reply)
        self.assertNotIn("暂未创建成功", reply)
        self.assertNotIn("simulated", reply)

    def test_channel_success_does_not_claim_customer_received_money(self):
        reply = self.answer("refund_succeeded")
        self.assertIn("支付渠道确认", reply)
        self.assertIn("到账记录为准", reply)


if __name__ == "__main__":
    unittest.main()
