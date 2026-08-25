from app.core.schemas import ToolResult
from app.storage.database import save_manual_review_to_db
from app.storage.store import get_order_by_id


def create_manual_review(
    order_id: str | None,
    review_type: str,
    risk_level: str,
    risk_flags: list[str],
    user_request: str,
    related_id: str | None = None,
) -> ToolResult:
    """创建人工审核单，用于大额退款、异常账号、投诉升级等高风险动作。"""

    order = get_order_by_id(order_id) if order_id else None
    review = save_manual_review_to_db(
        {
            "order_id": order_id,
            "user_id": order.get("user_id") if order else None,
            "review_type": review_type,
            "risk_level": risk_level,
            "risk_flags": risk_flags,
            "user_request": user_request,
            "related_id": related_id,
            "next_step": "由人工客服复核订单、用户凭证、风控原因和政策依据后处理。",
        }
    )

    return ToolResult(
        tool_name="create_manual_review",
        success=True,
        result=review,
    )


def transfer_to_human(reason: str, user_request: str, priority: str = "normal") -> ToolResult:
    """生成转人工交接单，用于人工审核或人工客服接管。"""

    return ToolResult(
        tool_name="transfer_to_human",
        success=True,
        result={
            "action": "transfer_to_human",
            "status": "pending_human_takeover",
            "reason": reason,
            "priority": priority,
            "user_request": user_request,
            "handoff_summary": f"用户诉求：{user_request}；转人工原因：{reason}。",
            "next_step": "请人工客服查看订单、聊天记录和工具结果后继续处理。",
        },
    )
