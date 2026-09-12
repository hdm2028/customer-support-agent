from datetime import datetime

from app.domain.risk_policy import evaluate_refund_risk
from app.domain.request_signals import asserted_mentions
from app.storage.database import get_customer_profile_from_db


class RefundPaymentNotConfirmed(ValueError):
    """The order has no confirmed payment from which to create a refund."""


def require_confirmed_refund_payment(order: dict) -> None:
    if order.get("payment_status") == "unpaid" or any(
        status in (order.get("order_status") or "") for status in ("待支付", "待付款")
    ):
        raise RefundPaymentNotConfirmed("订单尚未支付，不会产生退款；用户可自行取消待支付订单。")
    if order.get("payment_status") != "paid":
        raise RefundPaymentNotConfirmed("订单尚未确认支付成功，暂不能创建或提交退款；请先核实支付结果。")


def existing_after_sales_reason(order: dict) -> str | None:
    status = order.get("order_status") or ""
    if "退货审核中" in status or "退款" in status:
        return "订单已有售后或退款流程在处理中，不能重复创建退款申请。"
    return None


def parse_business_date(value: str | None) -> datetime | None:
    """解析订单日期字段，兼容日期和分钟级时间。"""

    if not value:
        return None

    for date_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue

    return None


def is_quality_or_fault_request(user_request: str) -> bool:
    return bool(asserted_mentions(user_request, ["质量问题", "坏了", "故障", "不能用", "无法使用", "破损", "少件"]))


def infer_refund_reason(user_request: str) -> str:
    if is_quality_or_fault_request(user_request):
        return "quality_issue"

    if asserted_mentions(user_request, ["未收到", "没收到"]):
        return "not_received"

    if asserted_mentions(user_request, ["不想要", "不要了", "七天无理由"]):
        return "no_reason_return"

    if asserted_mentions(user_request, ["重复扣款", "支付异常", "扣款"]):
        return "payment_issue"

    return "refund_request"


def days_since_signed(order: dict) -> int | None:
    signed_at = parse_business_date(order.get("signed_date"))

    if not signed_at:
        return None

    return (datetime.now() - signed_at).days


def evaluate_refund_eligibility(
    order: dict,
    user_request: str,
    risk_assessment: dict | None = None,
) -> dict:
    """判断退款申请是否可以进入自动业务流。"""

    try:
        require_confirmed_refund_payment(order)
    except RefundPaymentNotConfirmed as error:
        return {"eligible": False, "reason": str(error),
                "refund_reason": infer_refund_reason(user_request), "review_required": False}

    if risk_assessment is None:
        profile = get_customer_profile_from_db(order.get("user_id"))
        risk_assessment = evaluate_refund_risk(order, profile, user_request)

    order_status = order.get("order_status") or ""
    category = order.get("category") or ""
    amount = float(order.get("amount") or 0)
    reason = infer_refund_reason(user_request)
    signed_days = days_since_signed(order)
    return_window_days = int(order.get("return_window_days") or 0)

    existing_reason = existing_after_sales_reason(order)
    if existing_reason:
        return {
            "eligible": False,
            "reason": existing_reason,
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
