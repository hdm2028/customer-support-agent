from collections import Counter, defaultdict

from app.rag.models import DocumentChunk
from app.rag.ranking import RANKING_MODES


def build_enterprise_catalog(chunks: list[DocumentChunk]) -> dict:
    sources = {}
    category_counts = Counter()
    domain_counts = Counter()
    strategy_counts = Counter()

    for chunk in chunks:
        metadata = chunk.metadata or {}
        category = metadata.get("knowledge_category", "general_policy")
        domain = metadata.get("business_domain", "customer_service")
        strategy = metadata.get("chunk_strategy", "default")
        source_type = metadata.get("source_type", "knowledge")

        category_counts[category] += 1
        domain_counts[domain] += 1
        strategy_counts[strategy] += 1

        if chunk.source not in sources:
            sources[chunk.source] = {
                "source": chunk.source,
                "file_type": chunk.file_type,
                "knowledge_category": category,
                "business_domain": domain,
                "source_type": source_type,
                "chunk_count": 0,
                "chunk_strategies": defaultdict(int),
                "sections": set(),
            }

        source = sources[chunk.source]
        source["chunk_count"] += 1
        source["chunk_strategies"][strategy] += 1
        source["sections"].add(chunk.section)

    documents = []
    for source in sources.values():
        documents.append(
            {
                **source,
                "chunk_strategies": dict(source["chunk_strategies"]),
                "sections": sorted(source["sections"]),
            }
        )

    documents.sort(key=lambda item: item["source"])

    return {
        "architecture": {
            "retrieval": [
                "semantic_vector_recall",
                "bm25_text_recall",
                "business_keyword_recall",
            ],
            "ranking_modes": list(RANKING_MODES),
            "cache_layers": [
                "rag_search_cache",
                "embedding_cache",
                "agent_state_cache",
                "conversation_cache",
            ],
        },
        "summary": {
            "document_count": len(documents),
            "chunk_count": len(chunks),
            "category_counts": dict(category_counts),
            "domain_counts": dict(domain_counts),
            "chunk_strategy_counts": dict(strategy_counts),
        },
        "documents": documents,
    }
