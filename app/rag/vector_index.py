from app.rag.document_loader import DocumentChunk
from app.rag.embedding_client import get_embedding_provider, keyword_score


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(left_value * right_value for left_value, right_value in zip(left, right))


class InMemoryVectorIndex:
    """内存向量索引：保存 chunk 和对应 embedding，并负责相似度检索。"""

    def __init__(self, chunks: list[DocumentChunk]) -> None:
        self.embedding_provider = get_embedding_provider()
        self.items = [
            {
                "chunk": chunk,
                "embedding": self.embedding_provider.embed_text(
                    f"{chunk.source}\n{chunk.section}\n{chunk.text}"
                ),
            }
            for chunk in chunks
        ]

    # 检索流程：
    # 1. 把用户问题转成向量。
    # 2. 和每个 chunk 的向量计算余弦相似度。
    # 3. 再混合关键词分数，提升业务关键词命中的稳定性。
    def search(self, query: str, top_k: int = 3) -> list[dict]:
        query_embedding = self.embedding_provider.embed_text(query)
        results = []

        for item in self.items:
            chunk = item["chunk"]
            vector_score = cosine_similarity(query_embedding, item["embedding"])
            lexical_score = keyword_score(query, chunk.source, chunk.text)
            final_score = vector_score + lexical_score * 0.08

            if final_score <= 0:
                continue

            chunk_data = chunk.to_dict()
            chunk_data["score"] = round(final_score, 4)
            chunk_data["vector_score"] = round(vector_score, 4)
            chunk_data["keyword_score"] = lexical_score
            results.append(chunk_data)

        results.sort(key=lambda item: item["score"], reverse=True)

        return results[:top_k]
