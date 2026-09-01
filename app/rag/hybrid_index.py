import math
from collections import Counter

from app.core.config import get_settings
from app.rag.embedding_client import (
    EmbeddingProvider,
    get_embedding_provider,
    keyword_score,
    tokenize,
)
from app.rag.models import DocumentChunk
from app.rag.query_context import RetrievalQuery
from app.rag.retrieval_text import build_retrieval_text
from app.rag.vector_store import InMemoryVectorStore


class BM25Index:
    def __init__(self, documents: list[str]) -> None:
        self.documents = documents
        self.tokenized_documents = [tokenize(document) for document in documents]
        self.term_frequencies = [
            Counter(tokens)
            for tokens in self.tokenized_documents
        ]
        self.document_frequencies = self._build_document_frequencies()
        self.avg_document_length = self._average_document_length()

    def _build_document_frequencies(self) -> Counter:
        frequencies = Counter()

        for tokens in self.tokenized_documents:
            frequencies.update(set(tokens))

        return frequencies

    def _average_document_length(self) -> float:
        if not self.tokenized_documents:
            return 0.0

        return sum(len(tokens) for tokens in self.tokenized_documents) / len(self.tokenized_documents)

    def score(self, query: str, document_index: int) -> float:
        query_tokens = tokenize(query)

        if not query_tokens:
            return 0.0

        document_tokens = self.tokenized_documents[document_index]
        document_length = len(document_tokens)

        if document_length == 0:
            return 0.0

        term_frequency = self.term_frequencies[document_index]
        score = 0.0
        total_documents = len(self.documents)
        k1 = 1.5
        b = 0.75

        for token in query_tokens:
            frequency = term_frequency.get(token, 0)

            if frequency == 0:
                continue

            document_frequency = self.document_frequencies.get(token, 0)
            idf = math.log(1 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5))
            denominator = frequency + k1 * (
                1 - b + b * document_length / max(self.avg_document_length, 1.0)
            )
            score += idf * (frequency * (k1 + 1)) / denominator

        return score


def normalize_score(value: float, max_value: float) -> float:
    if max_value <= 0:
        return 0.0

    return max(0.0, value) / max_value


class HybridRAGIndex:
    def __init__(
        self,
        chunks: list[DocumentChunk],
        vector_store: InMemoryVectorStore,
        *,
        kb_version: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = get_settings()
        self.embedding_provider = embedding_provider or get_embedding_provider()
        self.vector_store = vector_store
        self.kb_version = kb_version
        self.embedding_identity = (
            self.embedding_provider.document_embedding_identity()
        )
        self.chunks = list(chunks)
        self.items = []

        for chunk in self.chunks:
            self.items.append(
                {
                    "chunk": chunk,
                    "retrieval_text": build_retrieval_text(chunk),
                }
            )

        self.bm25_index = BM25Index(
            [
                item["retrieval_text"]
                for item in self.items
            ]
        )

    def normalized_weights(
        self,
    ) -> tuple[float, float, float]:
        semantic_weight = max(
            self.settings.rag_semantic_weight,
            0.0,
        )
        bm25_weight = max(
            self.settings.rag_bm25_weight,
            0.0,
        )
        keyword_weight = max(
            self.settings.rag_keyword_weight,
            0.0,
        )

        total = (
            semantic_weight
            + bm25_weight
            + keyword_weight
        )

        if total <= 0:
            return 0.62, 0.28, 0.10

        return (
            semantic_weight / total,
            bm25_weight / total,
            keyword_weight / total,
        )

    def search(
        self,
        query: RetrievalQuery,
        candidate_k: int,
    ) -> list[dict]:
        if candidate_k <= 0:
            return []

        query_embedding = self.embedding_provider.embed_query(
            query.semantic_query
        )

        vector_results = self.vector_store.search(
            query_vector=query_embedding,
            top_k=self.vector_store.size(),
        )

        vector_scores = {
            record.chunk.chunk_id: score
            for record, score in vector_results
        }

        raw_results = []

        for index, item in enumerate(self.items):
            chunk = item["chunk"]

            semantic_score = vector_scores.get(
                chunk.chunk_id,
                0.0,
            )

            bm25_score = self.bm25_index.score(
                query.lexical_query,
                index,
            )

            lexical_score = keyword_score(
                query.lexical_query,
                chunk.source,
                chunk.text,
            )

            raw_results.append(
                {
                    "chunk": chunk,
                    "semantic_score": semantic_score,
                    "bm25_score": bm25_score,
                    "keyword_score": lexical_score,
                }
            )

        max_semantic = max(
            (
                max(result["semantic_score"], 0.0)
                for result in raw_results
            ),
            default=0.0,
        )

        max_bm25 = max(
            (
                result["bm25_score"]
                for result in raw_results
            ),
            default=0.0,
        )

        max_keyword = max(
            (
                float(result["keyword_score"])
                for result in raw_results
            ),
            default=0.0,
        )

        semantic_weight, bm25_weight, keyword_weight = (
            self.normalized_weights()
        )

        results = []

        for result in raw_results:
            semantic_norm = normalize_score(
                result["semantic_score"],
                max_semantic,
            )

            bm25_norm = normalize_score(
                result["bm25_score"],
                max_bm25,
            )

            keyword_norm = normalize_score(
                float(result["keyword_score"]),
                max_keyword,
            )

            hybrid_score = (
                semantic_norm * semantic_weight
                + bm25_norm * bm25_weight
                + keyword_norm * keyword_weight
            )

            if hybrid_score <= 0:
                continue

            chunk_data = result["chunk"].to_dict()

            chunk_data["retrieval_mode"] = (
                "hybrid_vector_bm25_keyword"
            )
            chunk_data["score"] = round(
                hybrid_score,
                4,
            )
            chunk_data["hybrid_score"] = round(
                hybrid_score,
                4,
            )
            chunk_data["vector_score"] = round(
                result["semantic_score"],
                4,
            )
            chunk_data["semantic_score"] = round(
                result["semantic_score"],
                4,
            )
            chunk_data["semantic_norm_score"] = round(
                semantic_norm,
                4,
            )
            chunk_data["bm25_score"] = round(
                result["bm25_score"],
                4,
            )
            chunk_data["bm25_norm_score"] = round(
                bm25_norm,
                4,
            )
            chunk_data["keyword_norm_score"] = round(
                keyword_norm,
                4,
            )

            chunk_data["retrieval_weights"] = {
                "semantic": round(
                    semantic_weight,
                    4,
                ),
                "bm25": round(
                    bm25_weight,
                    4,
                ),
                "keyword": round(
                    keyword_weight,
                    4,
                ),
            }

            results.append(chunk_data)

        results.sort(
            key=lambda item: item["hybrid_score"],
            reverse=True,
        )

        return results[:candidate_k]
