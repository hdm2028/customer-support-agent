from app.core.schemas import ToolResult
from app.storage.store import get_order_by_id
from app.storage.database import get_active_refund_request_by_order_id_from_db


def order_lookup(order_id: str) -> ToolResult:
    order = get_order_by_id(order_id)

    if not order:
        return ToolResult(
            tool_name="order_lookup",
            success=False,
            result=f"未找到订单号 {order_id}，请核对订单号是否正确。",
        )

    # Ownership has been checked by get_order_by_id before reading linked data.
    # Read failures must propagate; they are not evidence that no refund exists.
    refund = get_active_refund_request_by_order_id_from_db(order_id)
    summary = {key: refund.get(key) for key in ("refund_id", "order_id", "status")} if refund else None
    return ToolResult(
        tool_name="order_lookup",
        success=True,
        result={**order, "active_refund": summary},
    )
