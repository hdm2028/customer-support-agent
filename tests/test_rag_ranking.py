import unittest

from app.rag.query_context import RetrievalQuery
from app.rag.ranking import (
    HYBRID_MODE,
    RANKING_MODES,
    RULE_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
    SEMANTIC_RERANK_MODE,
    EvidenceConstraint,
    evaluate_evidence_constraint,
    rank_candidates,
)
from scripts.eval.eval_rag import classify_failure


def candidate(
    chunk_id: str,
    *,
    source: str,
    text: str,
    hybrid: float,
    semantic: float,
) -> dict:
    return {
        "chunk_id": chunk_id,
        "source": source,
        "section": "规则",
        "text": text,
        "score": hybrid,
        "hybrid_score": hybrid,
        "semantic_score": semantic,
        "vector_score": semantic,
        "keyword_score": 0,
    }


class RerankAblationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = RetrievalQuery("退款为什么没到账", "退款 到账")
        self.candidates = [
            candidate(
                "hybrid-first",
                source="普通规则.md",
                text="普通说明",
                hybrid=0.9,
                semantic=0.3,
            ),
            candidate(
                "semantic-first",
                source="退款政策.md",
                text="支付渠道到账说明",
                hybrid=0.8,
                semantic=0.95,
            ),
            candidate(
                "evidence",
                source="退款政策.md",
                text="退款失败时需要人工审核",
                hybrid=0.7,
                semantic=0.4,
            ),
        ]

    def test_all_modes_only_reorder_the_same_candidates(self) -> None:
        expected_ids = {item["chunk_id"] for item in self.candidates}
        constraint = EvidenceConstraint(
            required_sources=("退款政策.md",),
            required_terms=("人工审核",),
        )

        for mode in RANKING_MODES:
            with self.subTest(mode=mode):
                ranked = rank_candidates(
                    self.query,
                    self.candidates,
                    mode=mode,
                    top_k=2,
                    evidence_constraint=constraint,
                )
                self.assertEqual(
                    {item["chunk_id"] for item in ranked},
                    expected_ids,
                )

    def test_semantic_reranker_uses_relevance_only(self) -> None:
        ranked = rank_candidates(
            self.query,
            self.candidates,
            mode=SEMANTIC_RERANK_MODE,
            top_k=2,
        )

        self.assertEqual(ranked[0]["chunk_id"], "semantic-first")
        self.assertNotIn("rerank_bonus", ranked[0])
        self.assertEqual(ranked[0]["score"], ranked[0]["semantic_score"])

    def test_business_constraint_prioritizes_explicit_required_evidence(self) -> None:
        constraint = EvidenceConstraint(
            required_sources=("退款政策.md",),
            required_terms=("人工审核",),
        )
        ranked = rank_candidates(
            self.query,
            self.candidates,
            mode=SEMANTIC_CONSTRAINT_MODE,
            top_k=2,
            evidence_constraint=constraint,
        )
        report = evaluate_evidence_constraint(ranked[:2], constraint)

        self.assertEqual(ranked[0]["chunk_id"], "evidence")
        self.assertTrue(report["constraint_satisfied"])
        self.assertTrue(all(item.get("constraint_applied") for item in ranked))

    def test_modes_have_stable_public_names(self) -> None:
        self.assertEqual(
            RANKING_MODES,
            (
                HYBRID_MODE,
                RULE_RERANK_MODE,
                SEMANTIC_RERANK_MODE,
                SEMANTIC_CONSTRAINT_MODE,
            ),
        )


class FailureClassificationTests(unittest.TestCase):
    def test_retrieval_rerank_and_constraint_failures_are_distinct(self) -> None:
        satisfied = {"constraint_satisfied": True}
        missing = {"constraint_satisfied": False}

        self.assertEqual(
            classify_failure(
                mode=HYBRID_MODE,
                pool_rank=None,
                result_rank=None,
                pool_constraint=missing,
                result_constraint=missing,
                evidence_guardrail_pass=True,
            ),
            "retrieval_failure",
        )
        self.assertEqual(
            classify_failure(
                mode=SEMANTIC_RERANK_MODE,
                pool_rank=2,
                result_rank=None,
                pool_constraint=satisfied,
                result_constraint=missing,
                evidence_guardrail_pass=True,
            ),
            "rerank_failure",
        )
        self.assertEqual(
            classify_failure(
                mode=SEMANTIC_CONSTRAINT_MODE,
                pool_rank=2,
                result_rank=1,
                pool_constraint=satisfied,
                result_constraint=missing,
                evidence_guardrail_pass=True,
            ),
            "constraint_failure",
        )


if __name__ == "__main__":
    unittest.main()
