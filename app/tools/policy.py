from app.core.schemas import ToolResult
from app.rag.query_context import RetrievalQuery
from app.rag.retriever import HybridRetriever
from contextvars import ContextVar
from contextlib import contextmanager
import os


_RETRIEVER = HybridRetriever()
_QUERY_CONTEXT = ContextVar("policy_query_contract", default=None)


def configured_evidence_selection():
    mode = os.getenv("RAG_EVIDENCE_SELECTION", "")
    if mode not in {"", "complementary"}:
        raise ValueError("Unsupported RAG_EVIDENCE_SELECTION")
    return mode or None


@contextmanager
def policy_query_scope(query):
    token = _QUERY_CONTEXT.set(query)
    try:
        yield
    finally:
        _QUERY_CONTEXT.reset(token)


def policy_search(
    semantic_query: str,
    lexical_query: str,
    top_k: int = 2,
) -> ToolResult:
    selection = configured_evidence_selection()
    context = _QUERY_CONTEXT.get() if selection else None
    if context and (context.semantic_query != semantic_query or context.lexical_query != lexical_query):
        raise ValueError("Policy query arguments do not match the active query contract")
    query = RetrievalQuery(
        semantic_query=semantic_query,
        lexical_query=lexical_query,
        rerank_query=context.rerank_query if context else None,
        raw_query=context.raw_query if context else None,
        primary_intent=context.primary_intent if context else None,
        action_type=context.action_type if context else None,
    )
    options = {"mode": "hybrid_rule", "candidate_k": 20, "evidence_selection": selection} if selection else {}
    results = _RETRIEVER.retrieve(query=query, top_k=top_k, **options)

    if not results:
        return ToolResult(
            tool_name="policy_search",
            success=False,
            result="未找到匹配的售后知识，请重新描述问题。",
        )

    return ToolResult(
        tool_name="policy_search",
        success=True,
        result=results,
    )
