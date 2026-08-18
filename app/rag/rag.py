from pathlib import Path
import hashlib
import json

from app.core.config import get_settings
from app.rag.document_loader import DocumentChunk, build_chunks_from_dir
from app.rag.enterprise_knowledge import build_enterprise_catalog
from app.rag.hybrid_index import HybridRAGIndex
from app.storage.cache import get_json_cache, set_json_cache
from app.storage.store import KNOWLEDGE_DIR


_INDEX_CACHE = {
    "signature": None,
    "chunks": [],
    "index": None,
}


# 根据知识库目录下文件的名字、修改时间和大小生成签名。
# 作用：当知识库文件变化时，自动重建 chunk 和向量索引。
def knowledge_signature(directory: Path = KNOWLEDGE_DIR) -> tuple:
    if not directory.exists():
        return ()

    signature = []

    for file_path in sorted(directory.iterdir()):
        if not file_path.is_file():
            continue

        stat = file_path.stat()
        signature.append((file_path.name, stat.st_mtime_ns, stat.st_size))

    return tuple(signature)


# 把 knowledge 目录里的原始文档解析并切分成结构化 chunk。
# 后续如果接 PDF、OCR、网页，这里仍然是统一入口。
def build_knowledge_chunks() -> list[DocumentChunk]:
    return build_chunks_from_dir(KNOWLEDGE_DIR)


# 获取知识库索引。为了避免每次请求都重新切文档和算 embedding，这里做了内存缓存。
def get_knowledge_index() -> tuple[list[DocumentChunk], HybridRAGIndex]:
    signature = knowledge_signature()

    if _INDEX_CACHE["signature"] != signature:
        chunks = build_knowledge_chunks()
        _INDEX_CACHE["signature"] = signature
        _INDEX_CACHE["chunks"] = chunks
        _INDEX_CACHE["index"] = HybridRAGIndex(chunks)

    return _INDEX_CACHE["chunks"], _INDEX_CACHE["index"]


def rag_cache_key(query: str, top_k: int, signature: tuple) -> str:
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


# 对外暴露的检索函数：输入用户问题，返回最相关的知识片段。
def search_documents(query: str, top_k: int = 3) -> list[dict]:
    signature = knowledge_signature()
    cache_key = rag_cache_key(query, top_k, signature)
    cached = get_json_cache(cache_key)

    if cached is not None:
        return cached

    _, index = get_knowledge_index()

    if index is None:
        return []

    results = index.search(query, top_k=top_k)
    set_json_cache(cache_key, results)

    return results


# 给调试和接口使用：查看当前知识库到底被切成了哪些 chunk。
def list_chunks() -> list[dict]:
    chunks, _ = get_knowledge_index()
    return [chunk.to_dict() for chunk in chunks]


def get_knowledge_catalog() -> dict:
    signature = knowledge_signature()
    cache_key = "knowledge_catalog:" + hashlib.sha256(
        json.dumps(signature, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    cached = get_json_cache(cache_key)

    if cached is not None:
        return cached

    chunks, _ = get_knowledge_index()
    catalog = build_enterprise_catalog(chunks)
    set_json_cache(cache_key, catalog)

    return catalog
