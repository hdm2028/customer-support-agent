import unittest
from unittest.mock import patch

from app.agent.agents.customer import CustomerAgent
from app.agent.state import AgentState
from app.core.schemas import RouteDecision
from app.rag.query_context import RetrievalQuery
from app.tools import policy


class PolicyEvidenceBridgeTests(unittest.TestCase):
    def test_opt_in_preserves_rerank_contract_through_tool_thread(self):
        query = RetrievalQuery("政策咨询", "政策 申请", "主题：退款政策\n关联主题：人工审核")
        state = AgentState("conversation", "政策咨询", RouteDecision(need_policy=True))
        with patch.dict("os.environ", {"RAG_EVIDENCE_SELECTION": "complementary"}), \
             patch("app.agent.agents.customer.build_rag_query", return_value=query), \
             patch.object(policy._RETRIEVER, "retrieve", return_value=[{"text": "政策", "source": "policy.md"}]) as retrieve, \
             patch("app.agent.agents.customer.apply_policy_evidence_guardrail", side_effect=lambda _, result, **kwargs: result) as guard:
            CustomerAgent().run(state)
        self.assertEqual(retrieve.call_args.kwargs["query"], query)
        self.assertEqual(retrieve.call_args.kwargs["top_k"], 5)
        self.assertEqual(retrieve.call_args.kwargs["candidate_k"], 20)
        self.assertIs(guard.call_args.kwargs["route"], state.route)

    def test_default_tool_keeps_existing_profile(self):
        with patch.dict("os.environ", {"RAG_EVIDENCE_SELECTION": ""}), \
             patch.object(policy._RETRIEVER, "retrieve", return_value=[]) as retrieve:
            policy.policy_search("政策咨询", "政策 申请")
        self.assertEqual(retrieve.call_args.kwargs, {"query": RetrievalQuery("政策咨询", "政策 申请"), "top_k": 2})

    def test_stale_query_context_is_rejected(self):
        with patch.dict("os.environ", {"RAG_EVIDENCE_SELECTION": "complementary"}), \
             policy.policy_query_scope(RetrievalQuery("另一个问题", "另一个问题")):
            with self.assertRaises(ValueError):
                policy.policy_search("政策咨询", "政策 申请")


if __name__ == "__main__":
    unittest.main()
