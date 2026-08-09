import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import BASE_DIR
from app.rag.document_loader import (
    SUPPORTED_IMAGE_SUFFIXES,
    SUPPORTED_PDF_SUFFIXES,
    SUPPORTED_TEXT_SUFFIXES,
    build_chunks_from_dir,
)
from app.rag.embedding_client import EmbeddingProvider
from app.rag.vector_index import InMemoryVectorIndex
from app.storage.store import KNOWLEDGE_DIR


CACHE_DIR = BASE_DIR / "data" / "cache"
MANIFEST_PATH = CACHE_DIR / "knowledge_manifest.json"
SUPPORTED_SUFFIXES = (
    SUPPORTED_TEXT_SUFFIXES
    | SUPPORTED_PDF_SUFFIXES
    | SUPPORTED_IMAGE_SUFFIXES
)


def now_text() -> str:
    """返回统一格式的时间字符串，用于 manifest 和报告。"""

    return datetime.now().isoformat(timespec="seconds")


def sha256_file(file_path: Path) -> str:
    """计算文件内容 hash，用于判断文档是否新增或修改。"""

    digest = hashlib.sha256()

    with file_path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def load_manifest() -> dict:
    """读取上一次 ingest 的 manifest；第一次运行时返回空结构。"""

    if not MANIFEST_PATH.exists():
        return {
            "version": 1,
            "documents": {},
        }

    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def scan_knowledge_files() -> dict[str, dict]:
    """扫描 knowledge 目录，记录每个支持文档的 hash、大小和修改时间。"""

    files = {}

    if not KNOWLEDGE_DIR.exists():
        return files

    for file_path in sorted(KNOWLEDGE_DIR.iterdir()):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue

        stat = file_path.stat()
        files[file_path.name] = {
            "hash": sha256_file(file_path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "suffix": file_path.suffix.lower(),
        }

    return files


def diff_documents(old_manifest: dict, current_files: dict[str, dict]) -> dict:
    """对比当前文件和旧 manifest，判断新增、修改、未变化和删除文档。"""

    old_documents = old_manifest.get("documents", {})
    old_names = set(old_documents)
    current_names = set(current_files)

    added = sorted(current_names - old_names)
    deleted = sorted(old_names - current_names)
    unchanged = []
    modified = []

    for name in sorted(current_names & old_names):
        old_hash = old_documents[name].get("hash")
        current_hash = current_files[name].get("hash")

        if old_hash == current_hash:
            unchanged.append(name)
        else:
            modified.append(name)

    return {
        "added": added,
        "modified": modified,
        "unchanged": unchanged,
        "deleted": deleted,
    }


def count_chunks_by_source(chunks: list) -> dict[str, int]:
    """统计每份文档被切成了多少 chunk。"""

    counts = {}

    for chunk in chunks:
        counts[chunk.source] = counts.get(chunk.source, 0) + 1

    return counts


def build_embedding_text(chunk) -> str:
    """保持和 InMemoryVectorIndex 一致的 embedding 输入文本。"""

    return f"{chunk.source}\n{chunk.section}\n{chunk.text}"


def estimate_embedding_cache(chunks: list) -> dict:
    """估算本次构建会复用或新增多少条 embedding cache。

    目前项目使用内存向量索引，真正的向量库不会落盘；但智谱 embedding 结果会缓存到
    data/cache/embedding_cache.json。这里提前检查 cache key，方便报告成本节省情况。
    """

    provider = EmbeddingProvider()
    settings = provider.settings
    provider_name = settings.rag_embedding_provider

    unique_texts = sorted({
        build_embedding_text(chunk)
        for chunk in chunks
    })

    if provider_name != "zhipu":
        return {
            "provider": provider_name,
            "model": "local_hash",
            "total_unique_texts": len(unique_texts),
            "reused": 0,
            "created": 0,
            "note": "当前使用本地 hash embedding，不需要远程 embedding cache。",
        }

    reused = 0

    for text in unique_texts:
        cached = provider.cache.get(
            provider="zhipu",
            model=settings.zhipu_embedding_model,
            dimensions=settings.embedding_dimensions,
            text=text,
        )

        if cached is not None:
            reused += 1

    return {
        "provider": provider_name,
        "model": settings.zhipu_embedding_model,
        "total_unique_texts": len(unique_texts),
        "reused": reused,
        "created": len(unique_texts) - reused,
        "note": "created 表示构建索引时预计需要新生成的 embedding 数量。",
    }


def build_manifest(
    current_files: dict[str, dict],
    diff: dict,
    chunk_counts: dict[str, int],
    embedding_cache: dict,
) -> dict:
    """生成新的 knowledge manifest。"""

    documents = {}

    for name, file_info in current_files.items():
        documents[name] = {
            **file_info,
            "chunk_count": chunk_counts.get(name, 0),
            "status": (
                "added"
                if name in diff["added"]
                else "modified"
                if name in diff["modified"]
                else "unchanged"
            ),
        }

    return {
        "version": 1,
        "updated_at": now_text(),
        "knowledge_dir": str(KNOWLEDGE_DIR),
        "summary": {
            "added_count": len(diff["added"]),
            "modified_count": len(diff["modified"]),
            "unchanged_count": len(diff["unchanged"]),
            "deleted_count": len(diff["deleted"]),
            "document_count": len(current_files),
            "chunk_count": sum(chunk_counts.values()),
        },
        "changed_files": diff,
        "embedding_cache": embedding_cache,
        "documents": documents,
    }


def save_manifest(manifest: dict) -> None:
    """把 manifest 写入 data/cache，方便后续对比文档变化。"""

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def print_file_list(title: str, names: list[str]) -> None:
    """打印一组文件名，列表为空时也给出明确提示。"""

    print(f"{title}: {len(names)}")

    for name in names:
        print(f"  - {name}")


def print_report(manifest: dict) -> None:
    """把本次 ingest 的核心结果输出到终端。"""

    summary = manifest["summary"]
    changed_files = manifest["changed_files"]
    embedding_cache = manifest["embedding_cache"]

    print("=" * 72)
    print("Knowledge Ingest Report")
    print("=" * 72)
    print(f"知识库目录: {manifest['knowledge_dir']}")
    print(f"文档总数: {summary['document_count']}")
    print(f"chunk 总数: {summary['chunk_count']}")
    print(f"新增文档: {summary['added_count']}")
    print(f"修改文档: {summary['modified_count']}")
    print(f"未变化文档: {summary['unchanged_count']}")
    print(f"删除文档: {summary['deleted_count']}")
    print("-" * 72)
    print_file_list("新增", changed_files["added"])
    print_file_list("修改", changed_files["modified"])
    print_file_list("删除", changed_files["deleted"])
    print("-" * 72)
    print(f"Embedding provider: {embedding_cache['provider']}")
    print(f"Embedding model: {embedding_cache['model']}")
    print(f"唯一 embedding 文本数: {embedding_cache['total_unique_texts']}")
    print(f"预计复用 embedding: {embedding_cache['reused']}")
    print(f"预计新增 embedding: {embedding_cache['created']}")
    print(f"说明: {embedding_cache['note']}")
    print("-" * 72)
    print(f"manifest: {MANIFEST_PATH}")
    print("=" * 72)


def main() -> None:
    """构建知识库 chunk、预热 embedding cache，并生成增量 ingest manifest。"""

    old_manifest = load_manifest()
    current_files = scan_knowledge_files()
    diff = diff_documents(old_manifest, current_files)

    chunks = build_chunks_from_dir(
        KNOWLEDGE_DIR,
        max_chars=700,
        overlap=120,
    )
    chunk_counts = count_chunks_by_source(chunks)
    embedding_cache = estimate_embedding_cache(chunks)

    # 构建一次内存索引，触发缺失 embedding 的生成和缓存写入。
    InMemoryVectorIndex(chunks)

    manifest = build_manifest(
        current_files=current_files,
        diff=diff,
        chunk_counts=chunk_counts,
        embedding_cache=embedding_cache,
    )
    save_manifest(manifest)
    print_report(manifest)


if __name__ == "__main__":
    main()
