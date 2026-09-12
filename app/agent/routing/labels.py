"""Business labels shared by agent decisions and ticket creation."""

ISSUE_TYPE_LABELS = {
    "address_change": "地址修改",
    "cancel_order": "取消订单",
    "return_refund": "退货退款",
    "shipping_exception": "物流异常",
    "warranty_repair": "保修检测",
    "payment_invoice": "支付异常",
    "complaint": "投诉升级",
    "membership": "会员权益",
    "order_lookup": "订单查询",
    "general_support": "售后咨询",
}


def get_issue_type(intent: str) -> str:
    return ISSUE_TYPE_LABELS.get(intent, "售后咨询")
