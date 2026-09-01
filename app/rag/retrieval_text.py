from app.rag.models import DocumentChunk, content_hash_text


def build_retrieval_text(chunk: DocumentChunk) -> str:
    """Build the canonical text used by document embedding and BM25."""

    metadata = chunk.metadata or {}
    return "\n".join(
        [
            chunk.source,
            chunk.section,
            metadata.get("knowledge_category", ""),
            metadata.get("business_domain", ""),
            metadata.get("source_type", ""),
            chunk.text,
        ]
    )


def retrieval_text_hash(chunk: DocumentChunk) -> str:
    return content_hash_text(build_retrieval_text(chunk))
