from datetime import datetime
import re

from app.core.schemas import RouteDecision


def parse_date(value: str | None) -> datetime | None:
    """把订单里的日期字符串解析成 datetime，解析失败时返回 None。"""

    if not value:
        return None

    for date_format in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue

    return None


def extract_shipping_time(shipping_status: str | None) -> datetime | None:
    """从物流状态文本中提取最近一次物流更新时间。"""

    if not shipping_status:
        return None

    match = re.search(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?", shipping_status)

    if not match:
        return None

    return parse_date(match.group(0))


def is_within_warranty(order: dict, now: datetime | None = None) -> bool:
    """根据签收日期和保修月数判断订单是否仍在保修期内。"""

    signed_at = parse_date(order.get("signed_date"))
    warranty_months = int(order.get("warranty_months") or 0)

    if not signed_at or warranty_months <= 0:
        return False

    now = now or datetime.now()
    warranty_days = warranty_months * 30

    return (now - signed_at).days <= warranty_days


def evaluate_ticket_creation(
    route: RouteDecision,
    order: dict | None,
    issue_type: str,
    user_message: str,
) -> dict:
    """创建工单前的业务资格判断。

    Router 只负责判断“用户可能需要工单”，这里负责判断“当前订单状态是否允许创建这种工单”。
    """

    if not route.need_ticket:
        return {
            "can_create": False,
            "reason": "当前路由不需要创建工单。",
            "priority": "normal",
        }

    if not order:
        return {
            "can_create": False,
            "reason": "订单不存在，不能创建售后工单。",
            "priority": "normal",
        }

    order_status = order.get("order_status") or ""
    shipping_status = order.get("shipping_status") or ""
    notes = order.get("notes") or ""

    if issue_type == "保修检测":
        if not order.get("signed_date"):
            return {
                "can_create": False,
                "reason": "订单尚未签收，暂时不能创建保修检测工单；请先等待签收或物流状态更新。",
                "priority": "normal",
            }

        if int(order.get("warranty_months") or 0) <= 0:
            return {
                "can_create": False,
                "reason": "该订单商品没有配置保修期，不能直接创建保修检测工单。",
                "priority": "normal",
            }

        if not is_within_warranty(order):
            return {
                "can_create": False,
                "reason": "订单已超过保修期，不能直接创建保修检测工单，需要转人工进一步确认。",
                "priority": "normal",
            }

        return {
            "can_create": True,
            "reason": "订单已签收且仍在保修期内，可以创建保修检测工单草稿。",
            "priority": "normal",
        }

    if issue_type == "物流异常":
        if order.get("signed_date") or "已签收" in shipping_status:
            return {
                "can_create": False,
                "reason": "订单已签收，当前不适合创建物流异常工单。",
                "priority": "normal",
            }

        if "未超过48" in notes or "未超过 48" in notes:
            return {
                "can_create": False,
                "reason": "物流刚更新且未超过 48 小时，建议用户继续观察，暂不创建物流异常工单。",
                "priority": "normal",
            }

        last_update = extract_shipping_time(shipping_status)
        if last_update and (datetime.now() - last_update).total_seconds() < 48 * 3600:
            return {
                "can_create": False,
                "reason": "最近一次物流更新未超过 48 小时，暂不创建物流异常工单。",
                "priority": "normal",
            }

        return {
            "can_create": True,
            "reason": "物流长时间未更新，可以创建物流异常工单草稿。",
            "priority": "normal",
        }

    if issue_type == "地址修改":
        if "待发货" not in order_status:
            return {
                "can_create": False,
                "reason": "订单已进入发货或签收流程，不能直接创建地址修改工单。",
                "priority": "normal",
            }

        return {
            "can_create": True,
            "reason": "待发货订单可以提交地址修改工单，由仓库人工确认。",
            "priority": "normal",
        }

    if issue_type == "支付异常":
        return {
            "can_create": True,
            "reason": "支付异常需要人工核实支付凭证和支付渠道回调。",
            "priority": "high",
        }

    if issue_type == "投诉升级":
        return {
            "can_create": True,
            "reason": "投诉场景需要创建升级工单交由人工客服处理。",
            "priority": "high",
        }

    if issue_type == "退货退款" or route.handoff_required:
        return {
            "can_create": True,
            "reason": "退款、退货或高风险请求需要创建待人工审核工单。",
            "priority": "high",
        }

    return {
        "can_create": True,
        "reason": f"{issue_type} 场景可以创建待人工审核工单。",
        "priority": "normal",
    }
