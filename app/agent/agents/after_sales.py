from datetime import datetime

from app.agent.agents.risk import evaluate_risk
from app.agent.policies.ticket_policy import parse_date
from app.core.schemas import RouteDecision


class AfterSalesAgent:
    """售后 Agent：负责订单查询、退款申请、售后工单和人工审核流转。"""

    key = "after_sales_agent"
    name = "售后 Agent"
    responsibility = "订单查询、退款申请、售后工单、MQ 任务和业务流程执行"

    def should_handle(self, route: RouteDecision) -> bool:
        return any(
            [
                route.need_order,
                route.need_refund_request,
                route.need_ticket,
                route.need_handoff,
                route.handoff_required,
            ]
        )

    def planned_tools(self, route: RouteDecision) -> list[str]:
        tools = []

        if route.need_order and route.order_id:
            tools.append("order_lookup")

        if route.need_refund_request:
            tools.append("refund_apply")

        if route.need_ticket:
            tools.append("create_ticket")

        if route.manual_review_required:
            tools.append("create_manual_review")

        if route.need_handoff:
            tools.append("transfer_to_human")

        return tools


def is_quality_or_fault_request(user_request: str) -> bool:
    return any(
        keyword in user_request
        for keyword in ["质量问题", "坏了", "故障", "不能用", "无法使用", "破损", "少件"]
    )


def infer_refund_reason(user_request: str) -> str:
    if is_quality_or_fault_request(user_request):
        return "quality_issue"

    if "未收到" in user_request or "没收到" in user_request:
        return "not_received"

    if "不想要" in user_request or "不要了" in user_request or "七天无理由" in user_request:
        return "no_reason_return"

    if "重复扣款" in user_request or "支付异常" in user_request or "扣款" in user_request:
        return "payment_issue"

    return "refund_request"


def days_since_signed(order: dict) -> int | None:
    signed_at = parse_date(order.get("signed_date"))

    if not signed_at:
        return None

    return (datetime.now() - signed_at).days


def evaluate_refund_eligibility(
    order: dict,
    user_request: str,
    risk_assessment: dict | None = None,
) -> dict:
    """判断退款申请是否可以进入自动业务流。"""

    risk_assessment = risk_assessment or evaluate_risk(order, user_request)
    order_status = order.get("order_status") or ""
    category = order.get("category") or ""
    amount = float(order.get("amount") or 0)
    reason = infer_refund_reason(user_request)
    signed_days = days_since_signed(order)
    return_window_days = int(order.get("return_window_days") or 0)

    if order.get("payment_status") == "unpaid" or "待支付" in order_status:
        return {
            "eligible": False,
            "reason": "订单尚未支付，不会产生退款；用户可自行取消待支付订单。",
            "refund_reason": reason,
            "review_required": False,
        }

    if "退货审核中" in order_status or "退款" in order_status:
        return {
            "eligible": False,
            "reason": "订单已有售后或退款流程在处理中，不能重复创建退款申请。",
            "refund_reason": reason,
            "review_required": False,
        }

    if "定制" in category and not is_quality_or_fault_request(user_request):
        return {
            "eligible": False,
            "reason": "定制商品通常不支持七天无理由退款，质量问题除外。",
            "refund_reason": reason,
            "review_required": True,
        }

    if signed_days is not None and return_window_days > 0 and signed_days > return_window_days:
        if not is_quality_or_fault_request(user_request):
            return {
                "eligible": False,
                "reason": f"订单签收已超过 {return_window_days} 天无理由退货窗口，不能自动创建退款申请。",
                "refund_reason": reason,
                "review_required": True,
            }

    review_required = bool(risk_assessment.get("review_required")) or amount >= 1000

    if "已发货" in order_status and signed_days is None:
        review_required = True

    return {
        "eligible": True,
        "reason": "订单满足进入退款申请流程的基础条件。",
        "refund_reason": reason,
        "review_required": review_required,
    }
