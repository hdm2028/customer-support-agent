import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.routing.llm_router import _validate_semantic_result
from app.core.schemas import RouteDecision, ToolResult
from app.rag.hybrid_index import HybridRAGIndex
from app.rag.models import DocumentChunk
from app.rag.query_builder import build_rag_query, build_rag_query_context
from app.rag.query_context import RetrievalQuery
from app.rag.retrieval_text import retrieval_text_hash
from app.rag.vector_store import InMemoryVectorStore
from app.tools import policy as policy_module
from app.tools.registry import get_required_arguments


def make_route(
    intent: str,
    topic: str,
    *,
    action_type: str = "query",
    related_topics: list[str] | None = None,
    handoff_required: bool = False,
) -> RouteDecision:
    return RouteDecision(
        intent=intent,
        action_type=action_type,
        topic=topic,
        related_topics=related_topics or [],
        need_policy=True,
        handoff_required=handoff_required,
    )


class RAGQueryBuilderTests(unittest.TestCase):
    def test_refund_query_uses_upstream_semantics(self) -> None:
        query = build_rag_query(
            "我想申请退款",
            make_route("return_refund", "refund_apply", action_type="execute"),
            [],
        )

        self.assertEqual(query.semantic_query, "我想申请退款")
        self.assertIn("退款申请", query.lexical_query)
        self.assertNotIn("MQ", query.semantic_query)
        self.assertNotIn("人工审核", query.lexical_query)
        self.assertEqual(
            query.rerank_query,
            "用户问题：我想申请退款\n"
            "主要意图：退款\n"
            "业务主题：退款申请",
        )
        self.assertNotIn("return_refund", query.rerank_query)
        self.assertNotIn("refund_apply", query.rerank_query)

    def test_shipping_query_expands_from_topic(self) -> None:
        query = build_rag_query(
            "快递三天没有更新",
            make_route("shipping_exception", "shipping_delay"),
            [],
        )

        self.assertEqual(query.semantic_query, "快递三天没有更新")
        self.assertIn("物流异常", query.lexical_query)
        self.assertIn("物流延迟", query.lexical_query)

    def test_related_topics_preserve_refund_shipping_and_damage(self) -> None:
        raw_query = "快递运输途中把商品弄坏了，我可以申请退款吗？"
        route = make_route(
            "return_refund",
            "refund_eligibility",
            related_topics=[
                "shipping_exception",
                "product_failure",
                "shipping_exception",
            ],
        )
        query = build_rag_query(raw_query, route, [])

        self.assertEqual(query.semantic_query, raw_query)
        for term in ("退款资格", "物流异常", "商品破损"):
            self.assertIn(term, query.lexical_query)
        self.assertIn("关联主题：物流异常；商品故障", query.rerank_query)
        self.assertEqual(query.rerank_query.count("物流异常"), 1)

    def test_order_facts_are_added_without_second_lookup(self) -> None:
        route = make_route("return_refund", "refund_eligibility")
        order_result = ToolResult(
            tool_name="order_lookup",
            success=True,
            result={
                "order_status": "已发货",
                "shipping_status": "运输中",
                "product_name": "蓝牙耳机",
                "category": "电子产品",
                "signed_date": None,
            },
        )
        query = build_rag_query(
            "我的订单已经发货了，现在还能退款吗？",
            route,
            [order_result],
        )

        self.assertIn("订单状态：已发货", query.semantic_query)
        self.assertIn("物流状态：运输中", query.semantic_query)
        self.assertNotIn("签收日期：None", query.semantic_query)
        self.assertIn("订单状态：已发货", query.rerank_query)
        self.assertIn("物流状态：运输中", query.rerank_query)
        self.assertIn("商品名称：蓝牙耳机", query.rerank_query)
        self.assertIn("商品类目：电子产品", query.rerank_query)
        self.assertNotIn("None", query.rerank_query)

    def test_faq_without_order_result(self) -> None:
        route = make_route("membership", "membership_policy")
        context = build_rag_query_context("会员有哪些权益？", route, [])
        query = build_rag_query("会员有哪些权益？", route, [])

        self.assertIsNone(context.order_status)
        self.assertEqual(query.semantic_query, "会员有哪些权益？")
        self.assertIn("会员政策", query.lexical_query)
        self.assertNotIn("人工处理", query.rerank_query)

    def test_handoff_semantics_are_preserved_without_mutating_route(self) -> None:
        route = make_route(
            "complaint",
            "escalation",
            action_type="handoff",
            handoff_required=True,
        )
        query = build_rag_query("我要转人工处理投诉", route, [])

        self.assertTrue(route.handoff_required)
        self.assertIn("处理约束：需要人工处理", query.semantic_query)
        self.assertIn("人工处理", query.lexical_query)
        self.assertIn("处理约束：需要人工处理", query.rerank_query)

    def test_semantic_router_validates_related_topics(self) -> None:
        semantic = _validate_semantic_result(
            {
                "intent": "return_refund",
                "action_type": "query",
                "topic": "refund_eligibility",
                "related_topics": [
                    "shipping_exception",
                    "product_failure",
                    "shipping_exception",
                    "refund_eligibility",
                ],
                "confidence": 0.9,
                "reason": "退款资格同时涉及运输损坏",
            }
        )

        self.assertEqual(
            semantic.related_topics,
            ["shipping_exception", "product_failure"],
        )


class HybridQueryContractTests(unittest.TestCase):
    def test_each_retrieval_stage_receives_the_correct_query(self) -> None:
        observed: dict[str, str] = {}

        class FakeEmbeddingProvider:
            def embed_query(self, text: str) -> list[float]:
                observed["embedding"] = text
                return [1.0, 0.0]

        class FakeBM25Index:
            def score(self, query: str, document_index: int) -> float:
                observed["bm25"] = query
                return 1.0

        chunk = DocumentChunk(
            chunk_id="test-chunk",
            document_id="doc-test",
            source="退款政策.md",
            text="退款与物流异常处理规则",
            file_type="md",
            page=None,
            section="退款条件",
            start_char=0,
            end_char=12,
            content_hash="chunk-hash",
            chunker_version="section-char-v1",
            metadata={},
        )
        index = HybridRAGIndex.__new__(HybridRAGIndex)
        index.settings = SimpleNamespace(
            rag_semantic_weight=0.62,
            rag_bm25_weight=0.28,
            rag_keyword_weight=0.10,
            rag_candidate_multiplier=2,
        )
        index.embedding_provider = FakeEmbeddingProvider()
        index.bm25_index = FakeBM25Index()
        index.vector_store = InMemoryVectorStore()
        index.vector_store.upsert(
            chunk,
            [1.0, 0.0],
            embedding_text_hash=retrieval_text_hash(chunk),
            embedding_identity="fake-v1",
        )
        index.items = [
            {
                "chunk": chunk,
                "retrieval_text": chunk.text,
            }
        ]

        def fake_keyword_score(query: str, source: str, text: str) -> int:
            observed["keyword"] = query
            return 1

        with patch(
            "app.rag.hybrid_index.keyword_score",
            side_effect=fake_keyword_score,
        ):
            results = index.search(
                RetrievalQuery(
                    semantic_query="自然语言退款问题",
                    lexical_query="退款 物流异常 商品破损",
                ),
                candidate_k=1,
            )

        self.assertEqual(observed["embedding"], "自然语言退款问题")
        self.assertEqual(observed["bm25"], "退款 物流异常 商品破损")
        self.assertEqual(observed["keyword"], "退款 物流异常 商品破损")
        self.assertEqual(len(results), 1)
        self.assertNotIn("rerank_score", results[0])

    def test_policy_tool_and_schema_use_the_dual_query_contract(self) -> None:
        with patch.object(
            policy_module._RETRIEVER,
            "retrieve",
            return_value=[{"citation": "退款政策.md", "text": "退款规则"}],
        ) as retrieve:
            result = policy_module.policy_search(
                semantic_query="自然语言退款问题",
                lexical_query="退款 退款政策",
            )

        query = retrieve.call_args.kwargs["query"]
        self.assertTrue(result.success)
        self.assertEqual(query.semantic_query, "自然语言退款问题")
        self.assertEqual(query.lexical_query, "退款 退款政策")
        self.assertEqual(
            get_required_arguments("policy_search"),
            ["semantic_query", "lexical_query"],
        )


if __name__ == "__main__":
    unittest.main()
