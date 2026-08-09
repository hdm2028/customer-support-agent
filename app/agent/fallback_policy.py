ORDER_RELATED_KEYWORDS = [
    "订单",
    "物流",
    "发货",
    "签收",
    "退款",
    "退货",
    "换货",
    "保修",
    "维修",
    "检测",
    "发票",
    "取消",
    "地址",
    "投诉",
    "坏了",
    "故障",
    "质量问题",
    "换新",
    "售后",
    "无法使用",
    "不能用",
]

ORDER_ID_REQUIRED_KEYWORDS = [
    "我的订单",
    "订单",
    "物流",
    "发货",
    "签收",
    "退款",
    "退货",
    "赔付",
    "换货",
    "换新",
    "保修",
    "维修",
    "检测",
    "坏了",
    "故障",
    "质量问题",
    "售后",
    "无法使用",
    "不能用",
    "退货仓库",
    "支付异常",
    "扣款",
    "发票",
    "投诉",
    "缺货",
    "补货",
    "改收货地址",
    "改地址",
    "修改地址",
]

RISKY_OPERATION_KEYWORDS = [
    "直接退款",
    "马上退款",
    "立即退款",
    "直接赔付",
    "马上赔付",
    "取消订单",
    "修改地址",
    "改地址",
    "改收货地址",
    "发优惠券",
    "补偿",
]

RISKY_ACTION_KEYWORDS = [
    "退款",
    "赔付",
    "取消订单",
    "修改地址",
    "改地址",
    "改收货地址",
]

RISKY_BYPASS_KEYWORDS = [
    "不要审核",
    "不用审核",
    "跳过审核",
    "绕过审核",
    "跳过检测",
    "不用检测",
    "直接",
]


def is_order_related(message: str) -> bool:
    """判断问题是否和具体订单有关。"""

    return any(keyword in message for keyword in ORDER_RELATED_KEYWORDS)


def requires_order_id(message: str) -> bool:
    """判断当前问题是否必须依赖具体订单号才能继续处理。"""

    return any(keyword in message for keyword in ORDER_ID_REQUIRED_KEYWORDS)


def is_risky_operation(message: str) -> bool:
    """判断用户是否请求高风险业务操作。"""

    if any(keyword in message for keyword in RISKY_OPERATION_KEYWORDS):
        return True

    has_risky_action = any(keyword in message for keyword in RISKY_ACTION_KEYWORDS)
    has_bypass_intent = any(keyword in message for keyword in RISKY_BYPASS_KEYWORDS)

    return has_risky_action and has_bypass_intent


def should_ask_order_id(message: str, order_id: str | None) -> bool:
    """如果问题必须查具体订单，但没有订单号，就应该先追问订单号。"""

    return requires_order_id(message) and order_id is None


def should_handoff_to_human(message: str) -> tuple[bool, str | None]:
    """判断是否需要转人工。"""

    if is_risky_operation(message):
        return True, "该请求涉及退款、赔付、取消订单、修改地址等高风险操作，需要人工客服审核。"

    return False, None
