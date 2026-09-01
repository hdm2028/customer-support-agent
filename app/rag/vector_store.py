from dataclasses import dataclass

from app.rag.models import DocumentChunk


def cosine_similarity(
    left: list[float],
    right: list[float],
) -> float:
    """计算两个已归一化向量之间的点积相似度。"""

    return sum(
        left_value * right_value
        for left_value, right_value in zip(left, right)
    )


@dataclass
class VectorRecord:
    """VectorStore 中保存的一条知识向量记录。"""

    chunk: DocumentChunk
    vector: list[float]
    embedding_text_hash: str
    embedding_identity: str


class InMemoryVectorStore:
    """当前项目使用的内存向量存储。

    目前知识库规模较小，使用内存实现即可。
    上层 RAG 只依赖该 Store 接口，未来可以替换为
    Qdrant、pgvector 等实现，而不需要重写检索流程。
    """

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}

    def upsert(
        self,
        chunk: DocumentChunk,
        vector: list[float],
        *,
        embedding_text_hash: str,
        embedding_identity: str,
    ) -> None:
        """新增或覆盖一个 chunk 对应的向量。"""

        self._records[chunk.chunk_id] = VectorRecord(
            chunk=chunk,
            vector=list(vector),
            embedding_text_hash=embedding_text_hash,
            embedding_identity=embedding_identity,
        )

    def upsert_many(
        self,
        records: list[VectorRecord],
    ) -> None:
        """批量新增或覆盖向量记录。"""

        for record in records:
            self._records[record.chunk.chunk_id] = record

    def delete(self, chunk_ids: str | list[str]) -> None:
        """根据 chunk_id 删除向量记录。"""

        targets = [chunk_ids] if isinstance(chunk_ids, str) else chunk_ids

        for chunk_id in targets:
            self._records.pop(chunk_id, None)

    def get(
        self,
        chunk_id: str,
    ) -> VectorRecord | None:
        """获取指定 chunk 的向量记录。"""

        return self._records.get(chunk_id)

    def get_by_embedding_text_hash(
        self,
        embedding_text_hash: str,
        *,
        embedding_identity: str,
    ) -> VectorRecord | None:
        """Find a reusable vector by its actual embedding input identity."""

        for record in self._records.values():
            if (
                record.embedding_text_hash == embedding_text_hash
                and record.embedding_identity == embedding_identity
            ):
                return record

        return None

    def search(
        self,
        query_vector: list[float],
        top_k: int,
    ) -> list[tuple[VectorRecord, float]]:
        """按照 cosine similarity 返回 TopK 向量结果。"""

        if top_k <= 0:
            return []

        results = [
            (
                record,
                cosine_similarity(
                    query_vector,
                    record.vector,
                ),
            )
            for record in self._records.values()
        ]

        results.sort(
            key=lambda item: item[1],
            reverse=True,
        )

        return results[:top_k]

    def clear(self) -> None:
        """清空所有向量记录。"""

        self._records.clear()

    def size(self) -> int:
        """返回当前向量记录数量。"""

        return len(self._records)

    def all_records(self) -> list[VectorRecord]:
        """返回当前所有向量记录。"""

        return list(self._records.values())
