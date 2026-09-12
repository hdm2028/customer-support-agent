import unittest
from unittest.mock import patch

from app.agent.agents.customer import CustomerAgent
from app.agent.policies.evidence_guardrail import (
    MIN_RAG_SCORE,
    apply_policy_evidence_guardrail,
    detect_policy_profile,
    validate_policy_evidence,
)
from app.agent.state import AgentState
from app.core.schemas import RouteDecision, ToolResult


def evidence(source="物流配送政策.md", text="物流签收争议需要核实签收人员及代收情况。", score=0.9):
    return ToolResult(tool_name="policy_search", success=True, result=[{
        "chunk_id": "policy-fragment", "source": source, "text": text, "score": score,
    }])


def shipping_route(**overrides):
    return RouteDecision(**{
        "intent": "shipping_exception", "action_type": "query", "topic": "shipping_policy",
        "need_policy": True, **overrides,
    })


class PolicyRouteAlignmentTests(unittest.TestCase):
    def test_customer_uses_resolved_intent_when_query_negates_refund(self):
        state = AgentState("conversation", "快递显示签收但没有收到，我暂不申请退款。", shipping_route())
        with patch("app.agent.agents.customer.execute_agent_tool", return_value=evidence()) as call:
            result = CustomerAgent().run(state)
        self.assertTrue(result.success)
        self.assertEqual(call.call_args.kwargs["tool_name"], "policy_search")
        self.assertEqual(result.state_updates["policy"][0]["chunk_id"], "policy-fragment")
        self.assertEqual(result.state_updates["policy"][0]["evidence_guardrail"]["profile"], "shipping_exception")

    def test_negated_or_quoted_refund_does_not_override_resolved_shipping(self):
        for query in ("只查物流，不退货。", "先前咨询过退款，这次只问签收记录。", "说明上写着退款，请解释这次物流延误。"):
            with self.subTest(query=query):
                passed, report = validate_policy_evidence(query, evidence(), route=shipping_route())
                self.assertTrue(passed)
                self.assertEqual(report["profile"], "shipping_exception")

    def test_primary_refund_in_mixed_request_still_requires_refund_evidence(self):
        route = RouteDecision(intent="return_refund", action_type="execute", topic="refund_apply",
                              related_topics=["shipping_policy"], need_refund_request=True)
        query = "快递没有收到，我要申请退款。"
        failed = apply_policy_evidence_guardrail(query, evidence(), route=route)
        self.assertFalse(failed.success)
        self.assertEqual(failed.result["guardrail_report"]["profile"], "return_refund")
        self.assertEqual(failed.result["guardrail_report"]["reason"], "evidence_source_mismatch")
        passed, _ = validate_policy_evidence(query, evidence("退款政策.md", "退款需核实支付及订单状态。"), route=route)
        self.assertTrue(passed)

    def test_route_selection_applies_to_other_existing_business_profiles(self):
        profiles = [
            ("address_change", "订单取消与修改政策.md", "出库前可以申请修改收货地址。"),
            ("warranty_repair", "保修政策.md", "保修需要核实购买时间及故障原因。"),
            ("payment_invoice", "支付与发票政策.md", "电子发票需要确认发票抬头和税号。"),
            ("membership", "会员权益政策.md", "会员权益不能绕过质量检测。"),
            ("complaint", "售后FAQ.md", "投诉可以提交升级工单。"),
        ]
        for intent, source, text in profiles:
            with self.subTest(intent=intent):
                route = RouteDecision(intent=intent, action_type="query")
                passed, report = validate_policy_evidence("这次不问退款，只咨询当前业务。", evidence(source, text), route=route)
                self.assertTrue(passed)
                self.assertEqual(report["profile"], intent)

    def test_absent_unknown_or_unmapped_route_retains_keyword_fallback(self):
        routes = [None, RouteDecision(), shipping_route(action_type="unknown"),
                  shipping_route(action_type="invalid"), RouteDecision(intent="unmapped", action_type="query")]
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(detect_policy_profile("物流问题，不申请退款。", route=route)["name"], "return_refund")
                passed, report = validate_policy_evidence("物流问题，不申请退款。", evidence(), route=route)
                self.assertFalse(passed)
                self.assertEqual(report["reason"], "evidence_source_mismatch")

    def test_related_topic_does_not_replace_primary_policy(self):
        route = shipping_route(related_topics=["refund_policy"])
        self.assertEqual(detect_policy_profile("查询物流，提到了退款。", route=route)["name"], "shipping_exception")
        passed, report = validate_policy_evidence("查询物流，提到了退款。", evidence("退款政策.md", "退款规则。"), route=route)
        self.assertFalse(passed)
        self.assertEqual(report["reason"], "evidence_source_mismatch")

    def test_score_source_content_and_empty_checks_remain_enforced(self):
        cases = [
            (evidence(score=MIN_RAG_SCORE - 0.01), "low_rag_confidence"),
            (evidence("未授权来源.md"), "evidence_source_mismatch"),
            (evidence("售后FAQ.md", text="其他内容。"), "evidence_keyword_miss"),
            (ToolResult(tool_name="policy_search", success=True, result=[]), "empty_policy_results"),
            (ToolResult(tool_name="policy_search", success=False, result={"error": "unavailable"}), "policy_search_failed"),
        ]
        for result, reason in cases:
            with self.subTest(reason=reason):
                passed, report = validate_policy_evidence("物流，不申请退款。", result, route=shipping_route())
                self.assertFalse(passed)
                self.assertEqual(report["reason"], reason)


if __name__ == "__main__":
    unittest.main()
