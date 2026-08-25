import math
from collections import Counter

from app.core.config import get_settings
from app.rag.document_loader import DocumentChunk
from app.rag.embedding_client import get_embedding_provider, keyword_score, tokenize
from app.rag.reranker import rerank_documents
from app.rag.vector_store import cosine_similarity


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
    def __init__(self, chunks: list[DocumentChunk]) -> None:
        self.settings = get_settings()
        self.embedding_provider = get_embedding_provider()
        self.items = []

        for chunk in chunks:
            retrieval_text = self.build_retrieval_text(chunk)
            self.items.append(
                {
                    "chunk": chunk,
                    "retrieval_text": retrieval_text,
                    "embedding": self.embedding_provider.embed_text(retrieval_text),
                }
            )

        self.bm25_index = BM25Index([
            item["retrieval_text"]
            for item in self.items
        ])

    def build_retrieval_text(self, chunk: DocumentChunk) -> str:
        metadata = chunk.metadata or {}
        category = metadata.get("knowledge_category", "")
        domain = metadata.get("business_domain", "")
        source_type = metadata.get("source_type", "")

        return "\n".join([
            chunk.source,
            chunk.section,
            category,
            domain,
            source_type,
            chunk.text,
        ])

    def normalized_weights(self) -> tuple[float, float, float]:
        semantic_weight = max(self.settings.rag_semantic_weight, 0.0)
        bm25_weight = max(self.settings.rag_bm25_weight, 0.0)
        keyword_weight = max(self.settings.rag_keyword_weight, 0.0)
        total = semantic_weight + bm25_weight + keyword_weight

        if total <= 0:
            return 0.62, 0.28, 0.10

        return (
            semantic_weight / total,
            bm25_weight / total,
            keyword_weight / total,
        )

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        if not query.strip():
            return []

        query_embedding = self.embedding_provider.embed_text(query)
        raw_results = []

        for index, item in enumerate(self.items):
            chunk = item["chunk"]
            semantic_score = cosine_similarity(query_embedding, item["embedding"])
            bm25_score = self.bm25_index.score(query, index)
            lexical_score = keyword_score(query, chunk.source, chunk.text)
            raw_results.append(
                {
                    "chunk": chunk,
                    "semantic_score": semantic_score,
                    "bm25_score": bm25_score,
                    "keyword_score": lexical_score,
                }
            )

        max_semantic = max((max(result["semantic_score"], 0.0) for result in raw_results), default=0.0)
        max_bm25 = max((result["bm25_score"] for result in raw_results), default=0.0)
        max_keyword = max((float(result["keyword_score"]) for result in raw_results), default=0.0)
        semantic_weight, bm25_weight, keyword_weight = self.normalized_weights()
        results = []

        for result in raw_results:
            semantic_norm = normalize_score(result["semantic_score"], max_semantic)
            bm25_norm = normalize_score(result["bm25_score"], max_bm25)
            keyword_norm = normalize_score(float(result["keyword_score"]), max_keyword)
            hybrid_score = (
                semantic_norm * semantic_weight
                + bm25_norm * bm25_weight
                + keyword_norm * keyword_weight
            )

            if hybrid_score <= 0:
                continue

            chunk_data = result["chunk"].to_dict()
            chunk_data["retrieval_mode"] = "hybrid_vector_bm25_keyword"
            chunk_data["score"] = round(hybrid_score, 4)
            chunk_data["hybrid_score"] = round(hybrid_score, 4)
            chunk_data["vector_score"] = round(result["semantic_score"], 4)
            chunk_data["semantic_score"] = round(result["semantic_score"], 4)
            chunk_data["semantic_norm_score"] = round(semantic_norm, 4)
            chunk_data["bm25_score"] = round(result["bm25_score"], 4)
            chunk_data["bm25_norm_score"] = round(bm25_norm, 4)
            chunk_data["keyword_norm_score"] = round(keyword_norm, 4)
            chunk_data["retrieval_weights"] = {
                "semantic": round(semantic_weight, 4),
                "bm25": round(bm25_weight, 4),
                "keyword": round(keyword_weight, 4),
            }
            results.append(chunk_data)

        results.sort(key=lambda item: item["hybrid_score"], reverse=True)
        candidate_k = max(
            top_k * max(self.settings.rag_candidate_multiplier, 1),
            top_k,
            10,
        )
        candidates = results[:candidate_k]
        reranked_results = rerank_documents(query, candidates)

        return reranked_results[:top_k]
