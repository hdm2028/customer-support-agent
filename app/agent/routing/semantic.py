from typing import Literal

from pydantic import BaseModel, Field


SemanticIntent = Literal[
    "address_change",
    "cancel_order",
    "return_refund",
    "shipping_exception",
    "warranty_repair",
    "payment_invoice",
    "complaint",
    "membership",
    "order_lookup",
    "general_support",
]


ActionType = Literal[
    "query",
    "execute",
    "handoff",
    "unknown",
]


class SemanticRoute(BaseModel):
    intent: SemanticIntent = "general_support"

    action_type: ActionType = "unknown"

    topic: str | None = None

    related_topics: list[str] = Field(default_factory=list)

    confidence: float = 0.0

    reason: str | None = None

    source: Literal[
        "rule",
        "llm",
        "fallback",
    ] = "fallback"
