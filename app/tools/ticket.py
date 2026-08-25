from app.core.schemas import ToolResult
from app.storage.database import save_ticket_to_db


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
