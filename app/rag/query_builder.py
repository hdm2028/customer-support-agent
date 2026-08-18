from app.agent.tools.tool_results import get_order_lookup_result
from app.core.schemas import RouteDecision, ToolResult


def infer_policy_intent(user_message: str) -> str:
    """把用户问题归纳成更适合 RAG 检索的业务意图词。"""

    if "改收货地址" in user_message or "修改地址" in user_message or "改地址" in user_message or "修改为" in user_message:
        return "修改收货地址 地址修改 出库前 仓库确认"

    if "取消" in user_message:
        return "取消订单 待发货 出库前"

    if "退款" in user_message or "退钱" in user_message or "退货" in user_message or "不想要" in user_message or "不要了" in user_message:
        return "退货退款 退款申请 七天无理由 质检 审核 人工审核 MQ"

    if (
        "物流" in user_message
        or "快递" in user_message
        or "没更新" in user_message
        or "没有更新" in user_message
        or "不更新" in user_message
        or "三天没动" in user_message
        or "超过48" in user_message
        or "停住" in user_message
        or "未收到" in user_message
        or "没收到" in user_message
    ):
        return "物流查询 物流异常 48 小时 工单"

    if "投诉" in user_message or "没人处理" in user_message or "起诉" in user_message or "差评" in user_message or "曝光" in user_message:
        return "投诉升级 升级工单 人工客服 记录用户诉求"

    if "保修" in user_message or "维修" in user_message or "检测" in user_message or "坏了" in user_message or "黑屏" in user_message or "质量" in user_message:
        return "保修范围 保修处理方式 检测工单"

    if "发票" in user_message:
        return "电子发票 发票抬头 税号 邮箱"

    if "缺货" in user_message or "补发" in user_message or "补货" in user_message:
        return "缺货订单处理 补发 继续等待 补货提醒"

    if "会员" in user_message:
        return "会员权益 售后权益限制 质量检测"

    return ""


def build_rag_query(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
) -> str:
    """把用户问题、订单状态和业务意图合成更精准的 RAG 检索 query。"""

    query_parts = [user_message]
    intent_text = infer_policy_intent(user_message)

    if intent_text:
        query_parts.append(f"用户意图：{intent_text}")

    if route.handoff_required:
        query_parts.append("风险边界：高风险操作 需要人工审核 不能直接执行")

    order_result = get_order_lookup_result(tool_results)

    if order_result and order_result.success:
        order = order_result.result
        is_shipping_query = any(
            keyword in user_message
            for keyword in ["物流", "快递", "发货", "没更新", "没有更新", "不更新", "三天没动", "超过48", "停住", "延迟", "丢件", "未收到", "没收到"]
        )
        query_parts.extend(
            [
                f"订单状态：{order.get('order_status')}",
                f"商品名称：{order.get('product_name')}",
                f"商品类目：{order.get('category')}",
            ]
        )

        if is_shipping_query:
            query_parts.append(f"物流状态：{order.get('shipping_status')}")

        if any(keyword in user_message for keyword in ["退款", "退钱", "退货", "不想要", "不要了", "保修", "维修", "坏了", "黑屏"]):
            query_parts.append(f"签收日期：{order.get('signed_date')}")

    return "\n".join(part for part in query_parts if part)
