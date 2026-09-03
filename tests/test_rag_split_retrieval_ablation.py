import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.rag.models import DocumentChunk
from app.rag.query_context import RetrievalQuery
from scripts.eval.eval_rag_split_retrieval_ablation import (
    CHUNK_STRATEGY,
    EXPANDED_LEXICAL_TOP_K,
    EXPANDED_SPLIT_FUSION_MODE,
    EXPANDED_SPLIT_SEMANTIC_MODE,
    EXPANDED_VECTOR_TOP_K,
    SPLIT_FUSION_MODE,
    SPLIT_SEMANTIC_MODE,
    build_new_primary_evidence_analysis,
    build_shared_semantic_rankings,
    build_split_union_candidates,
)
from app.rag.ingestion.chunker import CHUNK_STRATEGIES
from app.rag.ranking import HYBRID_MODE


def make_chunk(chunk_id: str) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        source=f"{chunk_id}.md",
        text=chunk_id,
        file_type="markdown",
        page=None,
        section=chunk_id,
        start_char=0,
        end_char=1,
        content_hash=f"hash-{chunk_id}",
        chunker_version="test",
    )


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.queries = []

    def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [1.0]


class FakeVectorStore:
    def __init__(self, chunks: list[DocumentChunk]) -> None:
        self.chunks = chunks

    def size(self) -> int:
        return len(self.chunks)

    def search(self, query_vector, top_k):
        scores = [0.9, 0.8, 0.7, 0.6]
        return [
            (SimpleNamespace(chunk=chunk), score)
            for chunk, score in zip(self.chunks, scores)
        ][:top_k]


class FakeBM25Index:
    def score(self, query: str, document_index: int) -> float:
        return [0.0, 4.0, 3.0, 0.0][document_index]


class FakeIndex:
    def __init__(self, chunks: list[DocumentChunk]) -> None:
        self.embedding_provider = FakeEmbeddingProvider()
        self.vector_store = FakeVectorStore(chunks)
        self.bm25_index = FakeBM25Index()
        self.items = [
            {"chunk": chunk, "retrieval_text": chunk.text}
            for chunk in chunks
        ]

    @staticmethod
    def normalized_weights() -> tuple[float, float, float]:
        return 0.6, 0.3, 0.1


class FakeSemanticReranker:
    def __init__(self) -> None:
        self.calls = 0
        self.inputs = []

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        self.calls += 1
        self.inputs.append((query, [item["chunk_id"] for item in candidates]))
        scores = {"a": 0.1, "b": 0.4, "c": 0.3, "d": 0.9, "e": 0.2}
        return [
            {
                **candidate,
                "semantic_rerank_score": scores[candidate["chunk_id"]],
                "semantic_reranker": {"provider": "fake"},
            }
            for candidate in candidates
        ]


def ranking_candidate(chunk_id: str, score: float) -> dict:
    return {
        "chunk_id": chunk_id,
        "source": f"{chunk_id}.md",
        "section": chunk_id,
        "text": chunk_id,
        "metadata": {},
        "score": score,
        "retrieval_score": score,
    }


class SplitRetrievalAblationTests(unittest.TestCase):
    def test_experiment_keeps_fixed512_with_64_token_overlap(self) -> None:
        self.assertEqual(CHUNK_STRATEGY, "fixed_512")
        self.assertEqual(CHUNK_STRATEGIES[CHUNK_STRATEGY].max_chars, 512)
        self.assertEqual(CHUNK_STRATEGIES[CHUNK_STRATEGY].overlap, 64)
        self.assertEqual(EXPANDED_VECTOR_TOP_K, 15)
        self.assertEqual(EXPANDED_LEXICAL_TOP_K, 15)

    @patch(
        "scripts.eval.eval_rag_split_retrieval_ablation.keyword_score",
        return_value=0.0,
    )
    def test_vector_and_lexical_top10_are_unioned_and_deduplicated(
        self,
        _keyword_score,
    ) -> None:
        chunks = [make_chunk(name) for name in ("a", "b", "c", "d")]
        index = FakeIndex(chunks)
        query = RetrievalQuery(
            semantic_query="semantic input",
            lexical_query="lexical input",
        )

        candidates, diagnostics = build_split_union_candidates(
            index,
            query,
            vector_top_k=2,
            lexical_top_k=2,
        )

        self.assertEqual(index.embedding_provider.queries, ["semantic input"])
        self.assertEqual(diagnostics["vector_chunk_ids"], ["a", "b"])
        self.assertEqual(diagnostics["lexical_chunk_ids"], ["b", "c"])
        self.assertEqual(diagnostics["overlap_count"], 1)
        self.assertEqual(diagnostics["candidate_count"], 3)
        self.assertEqual(
            {candidate["chunk_id"] for candidate in candidates},
            {"a", "b", "c"},
        )
        b_candidate = next(item for item in candidates if item["chunk_id"] == "b")
        self.assertEqual(b_candidate["union_branches"], ["vector", "lexical"])

    def test_a_c_e_f_g_share_one_semantic_call_per_case(self) -> None:
        reranker = FakeSemanticReranker()
        query = RetrievalQuery("semantic input", "lexical input")
        hybrid = [
            ranking_candidate("a", 0.9),
            ranking_candidate("b", 0.8),
            ranking_candidate("c", 0.7),
        ]
        union = [
            ranking_candidate("b", 0.8),
            ranking_candidate("c", 0.7),
            ranking_candidate("d", 0.6),
        ]
        expanded_union = [
            ranking_candidate("a", 0.9),
            ranking_candidate("c", 0.7),
            ranking_candidate("d", 0.6),
            ranking_candidate("e", 0.5),
        ]

        rankings = build_shared_semantic_rankings(
            query,
            hybrid,
            union,
            semantic_reranker=reranker,
            expanded_union_candidates=expanded_union,
        )

        self.assertEqual(reranker.calls, 1)
        self.assertEqual(
            reranker.inputs,
            [("semantic input", ["a", "b", "c", "d", "e"])],
        )
        self.assertEqual(rankings[HYBRID_MODE][0]["chunk_id"], "a")
        self.assertEqual(rankings[SPLIT_SEMANTIC_MODE][0]["chunk_id"], "d")
        self.assertEqual(
            {item["chunk_id"] for item in rankings[SPLIT_FUSION_MODE]},
            {"b", "c", "d"},
        )
        self.assertEqual(
            rankings[EXPANDED_SPLIT_SEMANTIC_MODE][0]["chunk_id"],
            "d",
        )
        self.assertEqual(
            {item["chunk_id"] for item in rankings[EXPANDED_SPLIT_FUSION_MODE]},
            {"a", "c", "d", "e"},
        )

    def test_new_primary_evidence_counts_only_union_recoveries(self) -> None:
        mode_reports = {
            HYBRID_MODE: {
                "results": [
                    {
                        "case_id": "new",
                        "candidate_expected_rank": "N/A",
                        "expected_rank": "N/A",
                    },
                    {
                        "case_id": "existing",
                        "candidate_expected_rank": 1,
                        "expected_rank": 1,
                    },
                ]
            },
            SPLIT_SEMANTIC_MODE: {
                "results": [
                    {
                        "case_id": "new",
                        "candidate_expected_rank": 2,
                        "expected_rank": 3,
                    },
                    {
                        "case_id": "existing",
                        "candidate_expected_rank": 1,
                        "expected_rank": "N/A",
                    },
                ]
            },
            SPLIT_FUSION_MODE: {
                "results": [
                    {
                        "case_id": "new",
                        "candidate_expected_rank": 2,
                        "expected_rank": "N/A",
                    },
                    {
                        "case_id": "existing",
                        "candidate_expected_rank": 1,
                        "expected_rank": 2,
                    },
                ]
            },
        }

        analysis = build_new_primary_evidence_analysis(mode_reports)

        self.assertEqual(
            analysis["primary_absent_hybrid_top20_entered_union_count"],
            1,
        )
        self.assertEqual(analysis["entered_f_top5_count"], 1)
        self.assertEqual(analysis["entered_g_top5_count"], 0)


if __name__ == "__main__":
    unittest.main()
