from app.core.schemas import ToolResult
from app.rag.rag import search_documents
from app.storage.database import save_ticket_to_db
from app.storage.store import get_order_by_id


def order_lookup(order_id: str) -> ToolResult:
    order = get_order_by_id(order_id)

    if not order:
        return ToolResult(
            tool_name="order_lookup",
            success=False,
            result=f"未找到订单号 {order_id}，请核对订单号是否正确。",
        )

    return ToolResult(
        tool_name="order_lookup",
        success=True,
        result=order,
    )


def policy_search(query: str, top_k: int = 2) -> ToolResult:
    results = search_documents(query, top_k=top_k)

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


def create_ticket(
    order_id: str | None,
    issue_type: str,
    user_request: str,
    priority: str = "normal",
) -> ToolResult:
    ticket = {
        "status": "pending_human_review",
        "risk_notice": "该工具只生成工单草稿，不会执行真实退款、赔付、取消订单或修改数据库。",
        "order_id": order_id or "未知订单",
        "issue_type": issue_type,
        "priority": priority,
        "user_request": user_request,
        "next_step": "请人工客服核对订单、凭证和售后政策后再处理。",
    }
    saved_ticket = save_ticket_to_db(ticket)

    return ToolResult(
        tool_name="create_ticket",
        success=True,
        result=saved_ticket,
    )
