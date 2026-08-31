import re

from app.agent.policies.fallback_policy import (
    should_ask_order_id,
    should_handoff_to_human,
)
from app.agent.policies.guardrails import check_user_input, contains_risky_action
from app.core.schemas import RouteDecision


POLICY_KEYWORDS = [
    "退货",
    "退款",
    "退钱",
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
    "快递",
    "未收到",
    "没收到",
    "发货",
    "签收",
    "会员",
    "支付",
    "扣款",
    "发票",
    "投诉",
    "曝光",
    "差评",
    "起诉",
    "12315",
    "七天无理由",
    "质量",
    "黑屏",
]

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
    "修改为",
    "改收货地址",
    "收货地址",
    "投诉",
    "曝光",
    "差评",
    "起诉",
    "12315",
    "退款",
    "退钱",
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
    "快递",
    "未收到",
    "没收到",
    "没有更新",
    "不更新",
    "没更新",
    "三天没动",
    "超过48",
    "停住",
    "延迟",
    "丢件",
    "支付异常",
    "扣款",
    "重复扣款",
    "改派",
]

REFUND_APPLY_KEYWORDS = [
    "申请退款",
    "我要退款",
    "帮我退款",
    "给我退款",
    "退款",
    "退钱",
    "退款申请",
    "退货退款",
    "不想要了",
    "不要了",
]

REFUND_QUESTION_KEYWORDS = [
    "可以",
    "能不能",
    "能否",
    "为什么",
    "多久到账",
    "还不到账",
    "政策",
    "吗",
    "怎么办",
]


INTENT_RULES = [
    {
        "intent": "address_change",
        "label": "修改收货地址",
        "keywords": ["改收货地址", "修改地址", "改地址", "修改为", "收货地址"],
    },
    {
        "intent": "cancel_order",
        "label": "取消订单",
        "keywords": ["取消订单", "取消", "不想买"],
    },
    {
        "intent": "return_refund",
        "label": "退货退款",
        "keywords": ["退货", "退款", "退钱", "七天无理由", "不想要", "不要了"],
    },
    {
        "intent": "shipping_exception",
        "label": "物流异常",
        "keywords": ["物流", "快递", "发货", "没更新", "没有更新", "不更新", "三天没动", "超过48", "停住", "延迟", "丢件", "未收到", "没收到"],
    },
    {
        "intent": "warranty_repair",
        "label": "保修维修",
        "keywords": ["保修", "维修", "检测", "坏了", "故障", "质量问题", "质量", "黑屏", "换新"],
    },
    {
        "intent": "payment_invoice",
        "label": "支付与发票",
        "keywords": ["支付", "扣款", "银行卡", "发票", "税号", "抬头"],
    },
    {
        "intent": "complaint",
        "label": "投诉升级",
        "keywords": ["投诉", "没人处理", "人工", "客服", "曝光", "差评", "起诉", "12315"],
    },
    {
        "intent": "membership",
        "label": "会员权益",
        "keywords": ["会员", "黑金", "权益"],
    },
]


def extract_order_id(user_message: str) -> str | None:
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", user_message)

    if match:
        return match.group(0)

    return None


def infer_route_intent(user_message: str, order_id: str | None) -> tuple[str, float, str]:
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
    if route.blocked_by_guardrail or route.need_clarification:
        return []

    if route.handoff_required and not route.order_id:
        return []

    plan = []

    if route.need_order and route.order_id:
        plan.append("order_lookup")

    if route.need_policy:
        plan.append("policy_search")

    if route.need_risk_check:
        plan.append("risk_check")

    if route.need_refund_request:
        plan.append("refund_apply")

    if route.manual_review_required:
        plan.append("create_manual_review")

    if route.need_handoff:
        plan.append("transfer_to_human")

    if route.need_ticket:
        plan.append("create_ticket")

    return plan


def is_refund_application(user_message: str, intent: str) -> bool:
    if intent != "return_refund":
        return False

    has_apply_keyword = any(keyword in user_message for keyword in REFUND_APPLY_KEYWORDS)

    if not has_apply_keyword:
        return False

    if any(keyword in user_message for keyword in REFUND_QUESTION_KEYWORDS):
        return False

    return True


def route_tools(user_message: str) -> RouteDecision:
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
    need_handoff = (
        any(keyword in user_message for keyword in HUMAN_HANDOFF_KEYWORDS)
        or handoff_required
    )
    has_ticket_intent = any(keyword in user_message for keyword in TICKET_KEYWORDS)
    need_refund_request = is_refund_application(user_message, intent) and order_id is not None
    need_risk_check = bool(
        order_id
        and (
            need_refund_request
            or intent == "complaint"
            or handoff_required
            or contains_risky_action(user_message)
        )
    )

    need_ticket = (
        (need_order and has_ticket_intent)
        or need_refund_request
        or contains_risky_action(user_message)
    )
    manual_review_required = handoff_required

    route = RouteDecision(
        intent=intent,
        confidence=confidence,
        routing_reason=routing_reason,
        order_id=order_id,
        need_order=need_order,
        need_policy=need_policy,
        need_ticket=need_ticket,
        need_refund_request=need_refund_request,
        need_risk_check=need_risk_check,
        manual_review_required=manual_review_required,
        need_handoff=need_handoff,
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
    if "投诉" in user_message:
        return "投诉升级"

    if "改收货地址" in user_message or "修改地址" in user_message or "改地址" in user_message or "修改为" in user_message:
        return "地址修改"

    if (
        "退款" in user_message
        or "退钱" in user_message
        or "退货" in user_message
        or "不想要" in user_message
        or "不要了" in user_message
    ):
        return "退货退款"

    if "支付异常" in user_message or "重复扣款" in user_message or "扣款" in user_message:
        return "支付异常"

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
