from app.core.schemas import ToolResult
from app.rag.rag import search_documents
from app.storage.database import save_ticket_to_db
from app.storage.store import get_order_by_id
from app.workbench.service import find_quick_reply, get_product, search_products


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


def get_shop_products(
    query: str = "",
    platform: str | None = None,
    in_stock_only: bool = False,
    limit: int = 5,
) -> ToolResult:
    """查询店铺商品列表，模拟客服工作台里的商品推荐能力。"""

    products = search_products(
        query=query,
        platform=platform,
        in_stock_only=in_stock_only,
        limit=limit,
    )

    if not products:
        return ToolResult(
            tool_name="get_shop_products",
            success=False,
            result={
                "message": "未找到匹配商品。",
                "query": query,
                "platform": platform,
            },
        )

    return ToolResult(
        tool_name="get_shop_products",
        success=True,
        result=products,
    )


def send_goods_link(product_id: str, platform: str | None = None) -> ToolResult:
    """生成商品卡片发送结果；不会真的调用电商平台。"""

    product = get_product(product_id)

    if not product:
        return ToolResult(
            tool_name="send_goods_link",
            success=False,
            result=f"未找到商品 {product_id}，无法发送商品卡片。",
        )

    return ToolResult(
        tool_name="send_goods_link",
        success=True,
        result={
            "action": "send_goods_card",
            "platform": platform or "demo",
            "product_id": product["product_id"],
            "title": product["title"],
            "price": product["price"],
            "stock": product["stock"],
            "card_text": f"已生成商品卡片：{product['title']}，价格 {product['price']} 元。",
            "risk_notice": "演示工具只生成商品卡片结果，不会真实发送平台消息。",
        },
    )


def get_quick_reply(intent: str, platform: str | None = None) -> ToolResult:
    """按意图获取客服快捷回复模板。"""

    reply = find_quick_reply(intent=intent, platform=platform)

    if not reply:
        return ToolResult(
            tool_name="get_quick_reply",
            success=False,
            result={
                "message": "未找到匹配快捷回复。",
                "intent": intent,
                "platform": platform,
            },
        )

    return ToolResult(
        tool_name="get_quick_reply",
        success=True,
        result=reply,
    )


def transfer_to_human(reason: str, user_request: str, priority: str = "normal") -> ToolResult:
    """生成转人工交接单，模拟多平台客服工作台的人工接管。"""

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
