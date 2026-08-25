from app.core.schemas import ToolResult
from app.domain.risk_policy import evaluate_refund_risk
from app.storage.database import get_customer_profile_from_db
from app.storage.store import get_order_by_id


def risk_check(order_id: str, user_request: str) -> ToolResult:
    """调用风控 Agent：识别高频退款、异常账号、恶意投诉和虚假描述。"""

    order = get_order_by_id(order_id)

    if not order:
        return ToolResult(
            tool_name="risk_check",
            success=False,
            result=f"未找到订单号 {order_id}，无法进行风控判断。",
        )

    profile = get_customer_profile_from_db(order.get("user_id"))
    assessment = evaluate_refund_risk(
        order=order,
        customer_profile=profile,
        user_request=user_request,
    )

    return ToolResult(
        tool_name="risk_check",
        success=True,
        result=assessment,
    )
