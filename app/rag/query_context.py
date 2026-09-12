from dataclasses import dataclass, field
import os


@dataclass
class RAGQueryContext:
    raw_query: str
    primary_intent: str | None = None
    action_type: str | None = None
    topic: str | None = None
    related_topics: list[str] = field(default_factory=list)
    order_status: str | None = None
    shipping_status: str | None = None
    product_name: str | None = None
    product_category: str | None = None
    signed_date: str | None = None
    handoff_required: bool = False


@dataclass(frozen=True)
class RetrievalQuery:
    semantic_query: str
    lexical_query: str
    rerank_query: str | None = None
    raw_query: str | None = None
    primary_intent: str | None = None
    action_type: str | None = None

    def __post_init__(self) -> None:
        if not self.semantic_query.strip():
            raise ValueError("semantic_query must not be empty")

        if not self.lexical_query.strip():
            raise ValueError("lexical_query must not be empty")


def resolve_embedding_query(query: RetrievalQuery) -> str:
    """Opt-in reuse of upstream context for query embedding only."""
    mode = os.getenv("RAG_SEMANTIC_CONTEXT", "")
    if mode not in {"", "route"}:
        raise ValueError("Unsupported RAG_SEMANTIC_CONTEXT")
    if mode == "route":
        return query.rerank_query or query.semantic_query
    return query.semantic_query
