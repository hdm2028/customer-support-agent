import hashlib
import json

from app.core.config import get_settings
from app.rag.enterprise_knowledge import build_enterprise_catalog
from app.rag.index_manager import RAGIndexManager, get_rag_index_manager
from app.rag.query_context import RetrievalQuery
from app.rag.ranking import (
    SEMANTIC_CONSTRAINT_MODE,
    SEMANTIC_RERANK_MODE,
    EvidenceConstraint,
    rank_candidates,
)
from app.rag.semantic_reranker import SemanticReranker, build_semantic_reranker
from app.storage.cache import get_json_cache, set_json_cache


class HybridRetriever:
    """Online RAG entry point backed only by the active in-memory index."""

    def __init__(
        self,
        index_manager: RAGIndexManager | None = None,
        semantic_reranker: SemanticReranker | None = None,
    ) -> None:
        self.index_manager = index_manager or get_rag_index_manager()
        self.semantic_reranker = semantic_reranker

    def candidate_cache_key(
        self,
        query: RetrievalQuery,
        candidate_k: int,
        kb_version: str,
        embedding_identity: str,
    ) -> str:
        settings = get_settings()
        payload = {
            "semantic_query": query.semantic_query,
            "lexical_query": query.lexical_query,
            "candidate_k": candidate_k,
            "kb_version": kb_version,
            "embedding_identity": embedding_identity,
            "retrieval_mode": "hybrid_vector_bm25_keyword",
            "weights": {
                "semantic": settings.rag_semantic_weight,
                "bm25": settings.rag_bm25_weight,
                "keyword": settings.rag_keyword_weight,
            },
            "candidate_multiplier": settings.rag_candidate_multiplier,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return "rag_search:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def resolve_candidate_k(top_k: int) -> int:
        multiplier = max(get_settings().rag_candidate_multiplier, 1)
        return max(top_k * multiplier, top_k, 10)

    def retrieve_candidates(
        self,
        query: RetrievalQuery,
        *,
        candidate_k: int,
    ) -> list[dict]:
        index = self.index_manager.get_active_index()

        if index is None or candidate_k <= 0:
            return []

        cache_key = self.candidate_cache_key(
            query,
            candidate_k,
            index.kb_version,
            index.embedding_identity,
        )
        cached = get_json_cache(cache_key)

        if cached is not None:
            return cached

        results = index.search(query, candidate_k=candidate_k)
        set_json_cache(cache_key, results)
        return results

    def retrieve(
        self,
        query: RetrievalQuery,
        top_k: int = 3,
        *,
        mode: str | None = None,
        candidate_k: int | None = None,
        evidence_constraint: EvidenceConstraint | None = None,
    ) -> list[dict]:
        if top_k <= 0:
            return []

        resolved_candidate_k = (
            candidate_k
            if candidate_k is not None
            else self.resolve_candidate_k(top_k)
        )
        if resolved_candidate_k < top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")

        candidates = self.retrieve_candidates(
            query,
            candidate_k=resolved_candidate_k,
        )
        resolved_mode = mode or get_settings().rag_ranking_mode
        if (
            resolved_mode in {SEMANTIC_RERANK_MODE, SEMANTIC_CONSTRAINT_MODE}
            and self.semantic_reranker is None
        ):
            self.semantic_reranker = build_semantic_reranker()
        ranked = rank_candidates(
            query,
            candidates,
            mode=resolved_mode,
            top_k=top_k,
            evidence_constraint=evidence_constraint,
            semantic_reranker=self.semantic_reranker,
        )
        return ranked[:top_k]

    async def aretrieve(
        self,
        query: RetrievalQuery,
        top_k: int = 3,
        *,
        mode: str | None = None,
        candidate_k: int | None = None,
        evidence_constraint: EvidenceConstraint | None = None,
    ) -> list[dict]:
        return self.retrieve(
            query=query,
            top_k=top_k,
            mode=mode,
            candidate_k=candidate_k,
            evidence_constraint=evidence_constraint,
        )

    def list_chunks(self) -> list[dict]:
        index = self.index_manager.get_active_index()
        return [chunk.to_dict() for chunk in index.chunks] if index else []

    def catalog(self) -> dict:
        index = self.index_manager.get_active_index()

        if index is None:
            return build_enterprise_catalog([])

        cache_key = f"knowledge_catalog:v2:{index.kb_version}"
        cached = get_json_cache(cache_key)

        if cached is not None:
            return cached

        catalog = build_enterprise_catalog(index.chunks)
        catalog["kb_version"] = index.kb_version
        set_json_cache(cache_key, catalog)
        return catalog
