from dataclasses import dataclass, field


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

    def __post_init__(self) -> None:
        if not self.semantic_query.strip():
            raise ValueError("semantic_query must not be empty")

        if not self.lexical_query.strip():
            raise ValueError("lexical_query must not be empty")
