from app.core.schemas import ToolResult
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
