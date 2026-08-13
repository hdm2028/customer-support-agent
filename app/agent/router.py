import re

from app.agent.fallback_policy import (
    should_ask_order_id,
    should_handoff_to_human,
)
from app.agent.guardrails import check_user_input, contains_risky_action
from app.core.schemas import RouteDecision


POLICY_KEYWORDS = [
    "退货",
    "退款",
    "不想要",
    "不要了",
    "换货",
    "取消",
    "修改",
    "地址",
    "保修",
    "维修",
    "检测",
    "坏了",
    "故障",
    "质量问题",
    "换新",
    "售后",
    "物流",
    "发货",
    "签收",
    "会员",
    "支付",
    "扣款",
    "发票",
    "库存",
    "缺货",
    "补货",
    "预售",
    "投诉",
    "七天无理由",
]

PRODUCT_KEYWORDS = [
    "推荐",
    "商品",
    "买",
    "下单",
    "链接",
    "商品卡片",
    "店里",
    "同款",
    "类似商品",
    "价格",
]

GOODS_LINK_KEYWORDS = [
    "发链接",
    "商品卡片",
    "链接",
    "发我",
    "推荐",
]

QUICK_REPLY_INTENT_KEYWORDS = {
    "payment_invoice": ["怎么开", "如何开", "抬头", "税号", "邮箱"],
    "missing_order_id": ["订单号"],
    "handoff": ["人工", "真人", "转人工", "客服"],
    "product_recommendation": ["推荐", "商品卡片", "发链接"],
}

HUMAN_HANDOFF_KEYWORDS = [
    "转人工",
    "人工客服",
    "真人客服",
    "不要机器人",
    "升级处理",
    "客服主管",
]

TICKET_KEYWORDS = [
    "取消订单",
    "修改地址",
    "改地址",
    "改收货地址",
    "收货地址",
    "投诉",
    "退款",
    "赔付",
    "工单",
    "人工",
    "坏了",
    "故障",
    "质量问题",
    "换新",
    "售后",
    "检测",
    "维修",
    "物流异常",
    "不更新",
    "没更新",
    "延迟",
    "丢件",
    "支付失败",
    "支付异常",
    "扣款",
    "重复扣款",
    "改派",
]


INTENT_RULES = [
    {
        "intent": "address_change",
        "label": "修改收货地址",
        "keywords": ["改收货地址", "修改地址", "改地址", "收货地址"],
    },
    {
        "intent": "cancel_order",
        "label": "取消订单",
        "keywords": ["取消订单", "取消", "不想买"],
    },
    {
        "intent": "return_refund",
        "label": "退货退款",
        "keywords": ["退货", "退款", "七天无理由", "不想要", "不要了"],
    },
    {
        "intent": "shipping_exception",
        "label": "物流异常",
        "keywords": ["物流", "快递", "发货", "没更新", "不更新", "延迟", "丢件"],
    },
    {
        "intent": "warranty_repair",
        "label": "保修维修",
        "keywords": ["保修", "维修", "检测", "坏了", "故障", "质量问题", "换新"],
    },
    {
        "intent": "payment_invoice",
        "label": "支付与发票",
        "keywords": ["支付", "扣款", "银行卡", "发票", "税号", "抬头"],
    },
    {
        "intent": "stock_restock",
        "label": "库存补发",
        "keywords": ["缺货", "补发", "补货", "预售"],
    },
    {
        "intent": "complaint",
        "label": "投诉升级",
        "keywords": ["投诉", "没人处理", "人工", "客服"],
    },
    {
        "intent": "membership",
        "label": "会员权益",
        "keywords": ["会员", "黑金", "权益"],
    },
    {
        "intent": "product_recommendation",
        "label": "商品推荐",
        "keywords": ["推荐", "商品", "店里", "买", "类似商品", "商品卡片"],
    },
]


def extract_order_id(user_message: str) -> str | None:
    """从用户输入中提取订单号。这里先用 4 位以上数字模拟真实订单号。"""

    # 中文客服输入里经常出现“订单10004”这种数字和中文贴在一起的写法。
    # 不能用 \b 单词边界，否则中文字符和数字相邻时可能无法识别。
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", user_message)

    if match:
        return match.group(0)

    return None


def infer_route_intent(user_message: str, order_id: str | None) -> tuple[str, float, str]:
    """规则优先识别售后意图，并返回可解释的置信度。"""

    best_rule = None
    best_hits = []

    for rule in INTENT_RULES:
        hits = [
            keyword for keyword in rule["keywords"]
            if keyword in user_message
        ]

        if len(hits) > len(best_hits):
            best_rule = rule
            best_hits = hits

    if best_rule:
        confidence = min(0.95, 0.55 + len(best_hits) * 0.12 + (0.08 if order_id else 0))
        reason = f"命中{best_rule['label']}意图关键词：{', '.join(best_hits)}。"
        return best_rule["intent"], round(confidence, 2), reason

    if order_id:
        return "order_lookup", 0.62, "识别到订单号，按订单查询意图处理。"

    return "general_support", 0.35, "未命中明确售后意图，按通用客服咨询处理。"


def build_tool_plan(route: RouteDecision) -> list[str]:
    """把路由决策转换成可展示的受控工具计划。"""

    if route.blocked_by_guardrail or route.need_clarification:
        return []

    if route.handoff_required and not route.order_id:
        return []

    plan = []

    if route.need_order and route.order_id:
        plan.append("order_lookup")

    if route.need_policy:
        plan.append("policy_search")

    if route.need_product_search:
        plan.append("get_shop_products")

    if route.need_goods_link:
        plan.append("send_goods_link")

    if route.need_quick_reply:
        plan.append("get_quick_reply")

    if route.need_handoff:
        plan.append("transfer_to_human")

    if route.need_ticket:
        plan.append("create_ticket")

    return plan


def detect_quick_reply_intent(user_message: str) -> str | None:
    """判断是否适合使用客服工作台快捷回复。"""

    if any(keyword in user_message for keyword in HUMAN_HANDOFF_KEYWORDS):
        return None

    for intent, keywords in QUICK_REPLY_INTENT_KEYWORDS.items():
        if intent in {"handoff", "product_recommendation"}:
            continue

        if any(keyword in user_message for keyword in keywords):
            return intent

    return None


def route_tools(user_message: str) -> RouteDecision:
    """根据用户问题判断本轮需要调用哪些工具。"""

    passed, reason = check_user_input(user_message)
    if not passed:
        return RouteDecision(
            intent="unsafe_request",
            confidence=1.0,
            routing_reason=reason,
            tool_plan=[],
            blocked_by_guardrail=True,
            guardrail_reason=reason,
        )

    order_id = extract_order_id(user_message)
    intent, confidence, routing_reason = infer_route_intent(user_message, order_id)
    need_clarification = should_ask_order_id(user_message, order_id)
    handoff_required, handoff_reason = should_handoff_to_human(user_message)

    need_order = order_id is not None
    need_policy = any(keyword in user_message for keyword in POLICY_KEYWORDS)
    need_product_search = any(keyword in user_message for keyword in PRODUCT_KEYWORDS)
    need_goods_link = need_product_search and any(keyword in user_message for keyword in GOODS_LINK_KEYWORDS)
    quick_reply_intent = detect_quick_reply_intent(user_message)
    need_quick_reply = quick_reply_intent is not None and not need_clarification
    need_handoff = (
        any(keyword in user_message for keyword in HUMAN_HANDOFF_KEYWORDS)
        or handoff_required
        or ("缺货" in user_message and not order_id)
    )
    has_ticket_intent = any(keyword in user_message for keyword in TICKET_KEYWORDS)

    # 工单通常需要订单号承载，避免“会员权益解释”等纯咨询被误建工单。
    # 高风险动作即使没有订单号，也要进入人工审核链路或被安全策略拦截。
    need_ticket = (need_order and has_ticket_intent) or contains_risky_action(user_message)

    route = RouteDecision(
        intent=intent,
        confidence=confidence,
        routing_reason=routing_reason,
        order_id=order_id,
        need_order=need_order,
        need_policy=need_policy,
        need_ticket=need_ticket,
        need_product_search=need_product_search,
        need_goods_link=need_goods_link,
        need_quick_reply=need_quick_reply,
        need_handoff=need_handoff,
        quick_reply_intent=quick_reply_intent,
        product_query=user_message if need_product_search else None,
        need_clarification=need_clarification,
        clarification_question="请您提供订单号，我才能继续查询订单状态并判断售后方案。"
        if need_clarification
        else None,
        handoff_required=handoff_required,
        handoff_reason=handoff_reason,
        risk_level="high" if handoff_required else "low",
    )
    route.tool_plan = build_tool_plan(route)

    return route


def infer_issue_type(user_message: str) -> str:
    """根据用户问题粗略推断工单类型。"""

    if "投诉" in user_message:
        return "投诉升级"

    if "改收货地址" in user_message or "修改地址" in user_message or "改地址" in user_message:
        return "地址修改"

    if "退款" in user_message or "退货" in user_message or "不想要" in user_message or "不要了" in user_message:
        return "退货退款"

    if "物流" in user_message:
        return "物流异常"

    if (
        "维修" in user_message
        or "检测" in user_message
        or "保修" in user_message
        or "坏了" in user_message
        or "故障" in user_message
        or "质量问题" in user_message
        or "换新" in user_message
    ):
        return "保修检测"

    return "售后咨询"
