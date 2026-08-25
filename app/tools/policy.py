from app.core.schemas import ToolResult
from app.rag.retriever import HybridRetriever


_RETRIEVER = HybridRetriever()


def policy_search(query: str, top_k: int = 2) -> ToolResult:
    results = _RETRIEVER.retrieve(query=query, top_k=top_k)

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
