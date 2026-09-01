import json

from app.rag.index_manager import RAGIndexManager


def main() -> None:
    manager = RAGIndexManager()
    result = manager.refresh()
    index = result.active_index
    report = {
        "success": True,
        "kb_version": result.kb_version,
        "document_diff": result.diff.to_dict(),
        "chunk_count": result.chunk_count,
        "vector_count": index.vector_store.size(),
        "embedded_count": result.embedded_count,
        "reused_count": result.reused_count,
        "embedding_identity": manager.embedding_provider.document_embedding_identity(),
        "manifest_path": str(manager.ingestion.manifest_path),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
