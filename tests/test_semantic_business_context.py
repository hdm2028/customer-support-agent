import unittest
from unittest.mock import patch

from app.domain.refund_policy import is_quality_or_fault_request, infer_refund_reason, evaluate_refund_eligibility
from app.domain.risk_policy import evaluate_refund_risk
from app.agent.policies.fallback_policy import is_risky_operation
from app.agent.routing.decision import requests_review_bypass
from app.core.schemas import RouteDecision
from app.rag.query_builder import build_rag_query
from app.rag.evidence_selection import _guardrail_masks
from app.tools.policy import policy_query_scope, policy_search


class BusinessSemanticTests(unittest.TestCase):
    def test_explicit_denials_do_not_claim_quality_exception(self):
        for text in ("没有质量问题，只是不想要", "没有任何故障", "并非破损", "不是不能用"):
            with self.subTest(text=text):
                self.assertFalse(is_quality_or_fault_request(text))
        self.assertEqual(infer_refund_reason("没有质量问题，只是不想要"), "no_reason_return")
        result = evaluate_refund_eligibility({"payment_status": "paid", "category": "定制商品"},
                    "没有质量问题，只是不想要", {"review_required": False, "risk_level": "low"})
        self.assertFalse(result["eligible"])

    def test_positive_faults_contrast_and_double_negation_remain(self):
        for text in ("商品不能用", "没有破损，但是有质量问题", "不是没有质量问题", "物流没问题，商品坏了"):
            with self.subTest(text=text):
                self.assertTrue(is_quality_or_fault_request(text))

    def test_risk_and_route_use_same_denial_policy(self):
        text = "我不投诉，也不要求直接退款"
        self.assertEqual(evaluate_refund_risk({}, {}, text)["risk_score"], 0)
        self.assertFalse(is_risky_operation(text))
        self.assertFalse(requests_review_bypass("不要绕过审核"))
        self.assertTrue(requests_review_bypass("不用审核，直接退款"))
        self.assertGreater(evaluate_refund_risk({}, {}, "我要投诉，直接退款")["risk_score"], 0)

    def test_structured_risk_facts_are_not_suppressed(self):
        self.assertGreater(evaluate_refund_risk({"amount": 1500}, {"account_status": "abnormal"},
                                             "我不投诉，也不要求直接退款")["risk_score"], 0)

    def test_policy_tool_preserves_route_for_selector(self):
        query = build_rag_query("没收到快递，暂不申请退款", RouteDecision(
            intent="shipping_exception", action_type="query", topic="shipping_policy"), [])
        candidates = [{"source": "物流配送政策.md", "text": "物流签收情况需要核实。"},
                      {"source": "退款政策.md", "text": "退款政策。"}]
        with patch.dict("os.environ", {"RAG_EVIDENCE_SELECTION": "complementary"}), \
                patch("app.tools.policy._RETRIEVER.retrieve", return_value=candidates) as retrieve:
            with policy_query_scope(query):
                policy_search(query.semantic_query, query.lexical_query)
        received = retrieve.call_args.kwargs["query"]
        self.assertEqual(received.primary_intent, "shipping_exception")
        self.assertEqual(_guardrail_masks(received, candidates), ([True, False], [True, False]))


if __name__ == "__main__":
    unittest.main()
