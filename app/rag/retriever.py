from pathlib import Path
import hashlib
import json

from app.core.config import get_settings
from app.rag.document_loader import DocumentChunk, build_chunks_from_dir
from app.rag.enterprise_knowledge import build_enterprise_catalog
from app.rag.hybrid_index import HybridRAGIndex
from app.storage.cache import get_json_cache, set_json_cache
from app.storage.store import KNOWLEDGE_DIR


class HybridRetriever:
    """企业知识库统一入口：Vector Recall + BM25 + Keyword Fusion + Rerank。"""

    def __init__(self, knowledge_dir: Path = KNOWLEDGE_DIR) -> None:
        self.knowledge_dir = knowledge_dir
        self._signature = None
        self._chunks: list[DocumentChunk] = []
        self._index: HybridRAGIndex | None = None

    def knowledge_signature(self) -> tuple:
        if not self.knowledge_dir.exists():
            return ()

        signature = []

        for file_path in sorted(self.knowledge_dir.iterdir()):
            if not file_path.is_file():
                continue

            stat = file_path.stat()
            signature.append((file_path.name, stat.st_mtime_ns, stat.st_size))

        return tuple(signature)

    def build_chunks(self) -> list[DocumentChunk]:
        return build_chunks_from_dir(self.knowledge_dir)

    def get_index(self) -> tuple[list[DocumentChunk], HybridRAGIndex | None]:
        signature = self.knowledge_signature()

        if self._signature != signature:
            chunks = self.build_chunks()
            self._signature = signature
            self._chunks = chunks
            self._index = HybridRAGIndex(chunks) if chunks else None

        return self._chunks, self._index

    def cache_key(self, query: str, top_k: int, signature: tuple) -> str:
        settings = get_settings()
        payload = {
            "query": query,
            "top_k": top_k,
            "signature": signature,
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

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        signature = self.knowledge_signature()
        cache_key = self.cache_key(query, top_k, signature)
        cached = get_json_cache(cache_key)

        if cached is not None:
            return cached

        _, index = self.get_index()

        if index is None:
            return []

        results = index.search(query, top_k=top_k)
        set_json_cache(cache_key, results)

        return results

    async def aretrieve(self, query: str, top_k: int = 3) -> list[dict]:
        return self.retrieve(query=query, top_k=top_k)

    def list_chunks(self) -> list[dict]:
        chunks, _ = self.get_index()
        return [chunk.to_dict() for chunk in chunks]

    def catalog(self) -> dict:
        signature = self.knowledge_signature()
        cache_key = "knowledge_catalog:" + hashlib.sha256(
            json.dumps(signature, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        cached = get_json_cache(cache_key)

        if cached is not None:
            return cached

        chunks, _ = self.get_index()
        catalog = build_enterprise_catalog(chunks)
        set_json_cache(cache_key, catalog)

        return catalog
