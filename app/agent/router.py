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


def extract_order_id(user_message: str) -> str | None:
    """从用户输入中提取订单号。这里先用 4 位以上数字模拟真实订单号。"""

    # 中文客服输入里经常出现“订单10004”这种数字和中文贴在一起的写法。
    # 不能用 \b 单词边界，否则中文字符和数字相邻时可能无法识别。
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", user_message)

    if match:
        return match.group(0)

    return None


def route_tools(user_message: str) -> RouteDecision:
    """根据用户问题判断本轮需要调用哪些工具。"""

    passed, reason = check_user_input(user_message)
    if not passed:
        return RouteDecision(
            blocked_by_guardrail=True,
            guardrail_reason=reason,
        )

    order_id = extract_order_id(user_message)
    need_clarification = should_ask_order_id(user_message, order_id)
    handoff_required, handoff_reason = should_handoff_to_human(user_message)

    need_order = order_id is not None
    need_policy = any(keyword in user_message for keyword in POLICY_KEYWORDS)
    has_ticket_intent = any(keyword in user_message for keyword in TICKET_KEYWORDS)

    # 工单通常需要订单号承载，避免“会员权益解释”等纯咨询被误建工单。
    # 高风险动作即使没有订单号，也要进入人工审核链路或被安全策略拦截。
    need_ticket = (need_order and has_ticket_intent) or contains_risky_action(user_message)

    return RouteDecision(
        order_id=order_id,
        need_order=need_order,
        need_policy=need_policy,
        need_ticket=need_ticket,
        need_clarification=need_clarification,
        clarification_question="请您提供订单号，我才能继续查询订单状态并判断售后方案。"
        if need_clarification
        else None,
        handoff_required=handoff_required,
        handoff_reason=handoff_reason,
        risk_level="high" if handoff_required else "low",
    )


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
