import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from app.rag.query_context import RAGQueryContext, RetrievalQuery
from app.rag.ranking import (
    HYBRID_MODE,
    RANKING_MODES,
    RULE_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
    SEMANTIC_RERANK_MODE,
    EvidenceConstraint,
    apply_business_evidence_constraint,
    build_ablation_rankings,
    build_evidence_constraint,
    evaluate_evidence_constraint,
    rank_candidates,
)
from app.rag.semantic_reranker import (
    CrossEncoderSemanticReranker,
    SemanticRerankerError,
    build_reranker_text,
    build_semantic_reranker,
)
from scripts.eval.eval_rag import CANDIDATE_K, diagnose_failure, run_ablation


@dataclass(frozen=True)
class FakeIdentity:
    provider: str = "deterministic_test_double"

    def to_dict(self) -> dict:
        return {"provider": self.provider}


class FakeSemanticReranker:
    def __init__(self) -> None:
        self.calls = 0
        self.received_pools: list[list[str]] = []
        self.identity = FakeIdentity()

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        self.calls += 1
        self.received_pools.append([item["chunk_id"] for item in candidates])
        reranked = [
            {
                **candidate,
                "semantic_rerank_score": candidate["fake_semantic_score"],
            }
            for candidate in candidates
        ]
        reranked.sort(
            key=lambda item: (
                item["semantic_rerank_score"],
                -item["retrieval_rank"],
            ),
            reverse=True,
        )
        return [
            {**candidate, "semantic_rank": rank}
            for rank, candidate in enumerate(reranked, start=1)
        ]


class FailingSemanticReranker(FakeSemanticReranker):
    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        raise SemanticRerankerError("semantic provider unavailable")


def candidate(
    number: int,
    *,
    source: str = "退款政策.md",
    text: str = "退款规则",
    category: str = "refund_policy",
    semantic_score: float | None = None,
) -> dict:
    retrieval_score = round(1.0 - number / 100, 4)
    return {
        "chunk_id": f"chunk-{number:02d}",
        "source": source,
        "section": f"规则 {number}",
        "text": text,
        "metadata": {
            "knowledge_category": category,
            "business_domain": "after_sales",
            "source_type": "policy",
        },
        "score": retrieval_score,
        "hybrid_score": retrieval_score,
        "semantic_score": 0.1,
        "vector_score": 0.1,
        "keyword_score": 0,
        "fake_semantic_score": (
            semantic_score if semantic_score is not None else number / 100
        ),
    }


class SemanticRerankerContractTests(unittest.TestCase):
    def test_candidate_representation_contains_retrieval_context(self) -> None:
        item = candidate(1, text="chunk body")
        representation = build_reranker_text(item)

        self.assertIn("section: 规则 1", representation)
        self.assertIn("knowledge_category: refund_policy", representation)
        self.assertIn("business_domain: after_sales", representation)
        self.assertIn("content: chunk body", representation)

    def test_semantic_score_is_independent_and_preserves_retrieval_score(self) -> None:
        reranker = FakeSemanticReranker()
        query = RetrievalQuery("退款", "退款")
        original = candidate(1, semantic_score=0.99)
        ranked = rank_candidates(
            query,
            [original],
            mode=SEMANTIC_RERANK_MODE,
            top_k=1,
            semantic_reranker=reranker,
        )

        self.assertEqual(ranked[0]["retrieval_score"], original["score"])
        self.assertEqual(ranked[0]["semantic_rerank_score"], 0.99)
        self.assertEqual(ranked[0]["score"], original["score"])

    def test_provider_configuration_error_is_explicit(self) -> None:
        settings = SimpleNamespace(rag_semantic_reranker_provider="unknown")
        with self.assertRaisesRegex(
            SemanticRerankerError,
            "Unsupported semantic reranker provider",
        ):
            build_semantic_reranker(settings)

    def test_inference_error_is_not_silently_replaced(self) -> None:
        class BrokenModel:
            def predict(self, *args, **kwargs):
                raise RuntimeError("inference exploded")

        reranker = CrossEncoderSemanticReranker()
        reranker._model = BrokenModel()
        with self.assertRaisesRegex(
            SemanticRerankerError,
            "Local semantic reranker inference failed",
        ):
            reranker.rerank("退款", [candidate(1)])


class RerankAblationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = RetrievalQuery("退款为什么没到账", "退款 到账")
        self.candidates = [candidate(number) for number in range(1, 21)]

    def test_ablation_uses_same_top20_and_one_semantic_pass(self) -> None:
        reranker = FakeSemanticReranker()
        rankings = build_ablation_rankings(
            self.query,
            self.candidates,
            semantic_reranker=reranker,
            top_k=5,
        )
        expected_ids = {item["chunk_id"] for item in self.candidates}

        self.assertEqual(reranker.calls, 1)
        self.assertEqual(
            reranker.received_pools,
            [[item["chunk_id"] for item in self.candidates]],
        )
        self.assertEqual(set(rankings), set(RANKING_MODES))
        for mode in RANKING_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(len(rankings[mode]), CANDIDATE_K)
                self.assertEqual(
                    {item["chunk_id"] for item in rankings[mode]},
                    expected_ids,
                )

    def test_a_executes_no_reranker(self) -> None:
        with patch(
            "app.rag.ranking.rerank_documents",
            side_effect=AssertionError("rule reranker called"),
        ), patch(
            "app.rag.ranking.build_semantic_reranker",
            side_effect=AssertionError("semantic reranker called"),
        ):
            ranked = rank_candidates(
                self.query,
                self.candidates,
                mode=HYBRID_MODE,
                top_k=5,
            )

        self.assertEqual(ranked[0]["chunk_id"], "chunk-01")
        self.assertNotIn("rule_score", ranked[0])
        self.assertNotIn("semantic_rerank_score", ranked[0])

    def test_b_executes_only_legacy_rule_reranker(self) -> None:
        with patch(
            "app.rag.ranking.build_semantic_reranker",
            side_effect=AssertionError("semantic reranker called"),
        ):
            ranked = rank_candidates(
                self.query,
                self.candidates,
                mode=RULE_RERANK_MODE,
                top_k=5,
            )

        self.assertIn("rule_score", ranked[0])
        self.assertIn("rule_boost", ranked[0])
        self.assertIn("rule_reason", ranked[0])
        self.assertNotIn("semantic_rerank_score", ranked[0])

    def test_c_executes_semantic_reranker(self) -> None:
        reranker = FakeSemanticReranker()
        ranked = rank_candidates(
            self.query,
            self.candidates,
            mode=SEMANTIC_RERANK_MODE,
            top_k=5,
            semantic_reranker=reranker,
        )

        self.assertEqual(reranker.calls, 1)
        self.assertEqual(ranked[0]["chunk_id"], "chunk-20")
        self.assertEqual(ranked[0]["semantic_rank"], 1)

    def test_d_reuses_c_and_no_requirement_means_exact_equality(self) -> None:
        reranker = FakeSemanticReranker()
        rankings = build_ablation_rankings(
            self.query,
            self.candidates,
            semantic_reranker=reranker,
            top_k=5,
            evidence_constraint=EvidenceConstraint(),
        )

        self.assertEqual(reranker.calls, 1)
        self.assertEqual(
            rankings[SEMANTIC_RERANK_MODE],
            rankings[SEMANTIC_CONSTRAINT_MODE],
        )

    def test_semantic_provider_failure_propagates(self) -> None:
        with self.assertRaisesRegex(
            SemanticRerankerError,
            "semantic provider unavailable",
        ):
            rank_candidates(
                self.query,
                self.candidates,
                mode=SEMANTIC_RERANK_MODE,
                top_k=5,
                semantic_reranker=FailingSemanticReranker(),
            )


class EvidenceConstraintTests(unittest.TestCase):
    def test_required_categories_come_from_semantics_not_query_keywords(self) -> None:
        context = RAGQueryContext(
            raw_query="这句话没有业务关键词",
            primary_intent="return_refund",
            action_type="query",
            topic="refund_eligibility",
            related_topics=["shipping_exception", "product_failure"],
        )
        constraint = build_evidence_constraint(context)

        self.assertEqual(
            constraint.required_categories,
            ("refund", "shipping", "product_after_sales"),
        )
        no_semantics = build_evidence_constraint(
            RAGQueryContext(raw_query="退款 物流 商品损坏")
        )
        self.assertEqual(no_semantics.required_categories, ())

    def test_missing_evidence_is_inserted_from_semantic_pool_minimally(self) -> None:
        candidates = [
            {**candidate(1, semantic_score=0.99), "semantic_rank": 1},
            {**candidate(2, semantic_score=0.98), "semantic_rank": 2},
            {
                **candidate(
                    3,
                    source="物流配送政策.md",
                    category="logistics_policy",
                    semantic_score=0.80,
                ),
                "metadata": {
                    "knowledge_category": "logistics_policy",
                    "business_domain": "fulfillment",
                    "source_type": "policy",
                },
                "semantic_rank": 3,
            },
        ]
        constraint = EvidenceConstraint(
            required_categories=("refund", "shipping"),
            category_reasons=(("shipping", "related_topics=shipping_exception"),),
        )
        adjusted = apply_business_evidence_constraint(
            candidates,
            constraint,
            top_k=2,
        )

        self.assertEqual(adjusted[0]["chunk_id"], "chunk-01")
        self.assertEqual(adjusted[1]["chunk_id"], "chunk-03")
        self.assertTrue(adjusted[1]["constraint_adjusted"])
        self.assertIn("related_topics=shipping_exception", adjusted[1]["constraint_reason"])
        self.assertTrue(
            evaluate_evidence_constraint(adjusted[:2], constraint)[
                "constraint_satisfied"
            ]
        )

    def test_complete_evidence_does_not_change_semantic_ranking(self) -> None:
        candidates = [
            {**candidate(1), "semantic_rank": 1},
            {
                **candidate(2, source="物流配送政策.md", category="logistics_policy"),
                "metadata": {
                    "knowledge_category": "logistics_policy",
                    "business_domain": "fulfillment",
                    "source_type": "policy",
                },
                "semantic_rank": 2,
            },
            {**candidate(3), "semantic_rank": 3},
        ]
        constraint = EvidenceConstraint(required_categories=("refund", "shipping"))
        adjusted = apply_business_evidence_constraint(candidates, constraint, top_k=2)

        self.assertEqual(
            [item["chunk_id"] for item in adjusted],
            [item["chunk_id"] for item in candidates],
        )
        self.assertFalse(any(item.get("constraint_adjusted") for item in adjusted))

    def test_constraint_only_reorders_supplied_candidates(self) -> None:
        candidates = [{**candidate(number), "semantic_rank": number} for number in range(1, 6)]
        adjusted = apply_business_evidence_constraint(
            candidates,
            EvidenceConstraint(required_categories=("refund",)),
            top_k=3,
        )
        self.assertEqual(
            {item["chunk_id"] for item in adjusted},
            {item["chunk_id"] for item in candidates},
        )


class EvaluationAblationTests(unittest.TestCase):
    def test_evaluation_retrieves_once_and_outputs_all_modes(self) -> None:
        class FakeRetriever:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def retrieve_candidates(self, query, *, candidate_k):
                self.calls.append(candidate_k)
                return [candidate(number) for number in range(1, 21)]

        cases = [
            {
                "case_id": f"case-{number}",
                "query": "退款规则",
                "rag_context": {
                    "primary_intent": "return_refund",
                    "action_type": "query",
                    "topic": "refund_policy",
                    "related_topics": [],
                },
                "expected_sources": ["退款政策.md"],
                "expected_keywords": ["退款"],
            }
            for number in range(2)
        ]
        retriever = FakeRetriever()
        reranker = FakeSemanticReranker()

        with patch(
            "scripts.eval.eval_rag.validate_policy_evidence",
            return_value=(True, {"passed": True}),
        ):
            reports = run_ablation(
                cases,
                retriever,
                top_k=5,
                semantic_reranker=reranker,
            )

        self.assertEqual(retriever.calls, [CANDIDATE_K, CANDIDATE_K])
        self.assertEqual(reranker.calls, len(cases))
        self.assertEqual(set(reports), set(RANKING_MODES))
        for report in reports.values():
            self.assertEqual(report["total_cases"], len(cases))


class FailureDiagnosisTests(unittest.TestCase):
    @staticmethod
    def keywords(satisfied: bool) -> dict:
        return {
            "satisfied": satisfied,
            "missing_terms": [] if satisfied else ["missing"],
        }

    @staticmethod
    def constraint(satisfied: bool) -> dict:
        return {
            "constraint_satisfied": satisfied,
            "missing_categories": [] if satisfied else ["shipping"],
        }

    def diagnose(self, **overrides) -> str:
        values = {
            "pool_source_pass": True,
            "pool_keywords": self.keywords(True),
            "pool_constraint": self.constraint(True),
            "result_source_pass": True,
            "result_keywords": self.keywords(True),
            "result_constraint": self.constraint(True),
            "evidence_guardrail_pass": True,
        }
        values.update(overrides)
        return diagnose_failure(**values)[0]

    def test_failure_stages_are_distinct(self) -> None:
        self.assertEqual(
            self.diagnose(pool_source_pass=False),
            "retrieval_failure",
        )
        self.assertEqual(
            self.diagnose(result_keywords=self.keywords(False)),
            "ranking_failure",
        )
        self.assertEqual(
            self.diagnose(result_constraint=self.constraint(False)),
            "evidence_coverage_failure",
        )
        self.assertEqual(self.diagnose(), "passed")


if __name__ == "__main__":
    unittest.main()
