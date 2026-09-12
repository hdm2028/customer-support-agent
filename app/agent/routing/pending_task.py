from app.agent.routing.parsing import extract_order_id
from app.core.schemas import RouteDecision
import re


def is_address_question(message: str) -> bool:
    return bool(re.search(r"[?？]|是否|能否|能不能|可不可以|怎么|如何|为什么|什么|吗[。！!\s]*$", message))


def cancels_pending_request(message: str) -> bool:
    cleaned = re.sub(r"[\s，,。！!；;]", "", message)
    return cleaned in {"算了", "算了取消申请", "取消申请", "取消刚才的申请", "取消刚才的操作",
                       "不申请了", "先不退了", "不用处理了", "不改了", "先不改了"}


def pending_message_kind(message: str, pending: dict | None) -> str:
    """Merge only slot values; new instructions must reach the router intact."""
    if not pending:
        return "new"
    if cancels_pending_request(message):
        return "cancel"
    missing = pending.get("missing_slots", [])
    explicit_order = re.search(r"订单(?:号)?\s*(?:是|为|[:：])?\s*(\d{4,})", message)
    previous_order = (pending.get("slots") or {}).get("order_id")
    if explicit_order and previous_order and explicit_order.group(1) != previous_order:
        return "replace"
    if "order_id" in missing and re.fullmatch(r"\s*(?:订单(?:号)?\s*(?:是|为|:|：)?\s*)?\d{4,}[。\s]*", message):
        return "slots"
    if "new_address" in missing:
        # A follow-up question is a new request, not a free-text address value.
        if is_address_question(message):
            return "replace"
        business_change = any(word in message for word in ("退款", "退货", "投诉", "取消", "不用", "不改", "不想", "先别"))
        if not business_change and extract_new_address(message, pending):
            return "slots"
    return "replace"


ADDRESS_CHANGE_KEYWORDS = [
    "改收货地址",
    "修改地址",
    "改地址",
    "修改收货地址",
    "换地址",
    "修改为",
]


def is_order_id_only_message(user_message: str) -> bool:
    return (
        extract_order_id(user_message) is not None
        and len(user_message.strip()) <= 12
    )


def is_address_change_request(user_message: str) -> bool:
    user_message = re.sub(r"(?:不用|不再|先别|别|不)(?:修改|更改|改)(?:收货)?地址", "", user_message)
    return any(
        keyword in user_message
        for keyword in ADDRESS_CHANGE_KEYWORDS
    )


def clean_slot_value(value: str) -> str:
    return value.strip().strip("。；;，,：: ")


def extract_new_address(
    user_message: str,
    pending_task: dict | None = None,
) -> str | None:
    if is_address_question(user_message):
        return None

    address_prefixes = [
        "新地址是",
        "新地址为",
        "收货地址是",
        "收货地址为",
        "地址是",
        "地址为",
        "改地址到",
        "地址改成",
        "改成",
        "修改为",
        "换成",
        "寄到",
        "发到",
    ]

    for prefix in address_prefixes:
        if prefix in user_message:
            _, value = user_message.split(prefix, 1)
            cleaned = clean_slot_value(value)

            if cleaned:
                return cleaned

    missing_slots = (
        pending_task.get("missing_slots", [])
        if pending_task
        else []
    )

    if (
        "new_address" in missing_slots
        and not is_order_id_only_message(user_message)
    ):
        cleaned = clean_slot_value(user_message)

        if len(cleaned) >= 6:
            return cleaned

    return None


def infer_required_slots(
    user_message: str,
    pending_task: dict | None = None,
) -> list[str]:
    if pending_task:
        return list(
            pending_task.get(
                "required_slots",
                pending_task.get("missing_slots", []),
            )
        )

    if is_address_change_request(user_message):
        return ["order_id", "new_address"]

    return []


def collect_slots(
    user_message: str,
    pending_task: dict | None = None,
) -> dict:
    slots = (
        dict(pending_task.get("slots", {}))
        if pending_task
        else {}
    )

    order_id = extract_order_id(user_message)
    new_address = extract_new_address(
        user_message,
        pending_task,
    )

    if pending_task and new_address and not re.search(r"订单(?:号)?\s*(?:是|为|[:：])?\s*\d{4,}", user_message):
        # House/unit numbers inside an address must not replace the bound order.
        order_id = None

    if order_id:
        slots["order_id"] = order_id

    if new_address:
        slots["new_address"] = new_address

    return slots


def build_effective_user_message(
    user_message: str,
    pending_task: dict | None,
    slots: dict,
) -> tuple[str, bool]:
    if not pending_task:
        return user_message, False

    original_request = pending_task.get(
        "user_request",
        user_message,
    )

    message_parts = [original_request]

    if slots.get("order_id"):
        message_parts.append(
            f"订单 {slots['order_id']}"
        )

    if slots.get("new_address"):
        message_parts.append(
            f"新收货地址：{slots['new_address']}"
        )

    return " ".join(message_parts), True


def prepare_pending_task_context(
    user_message: str,
    pending_task: dict | None,
) -> tuple[str, bool, dict, list[str]]:
    slots = collect_slots(
        user_message,
        pending_task,
    )

    required_slots = infer_required_slots(
        user_message,
        pending_task,
    )

    effective_user_message, used_pending_task = (
        build_effective_user_message(
            user_message=user_message,
            pending_task=pending_task,
            slots=slots,
        )
    )

    return (
        effective_user_message,
        used_pending_task,
        slots,
        required_slots,
    )


def get_missing_slots(
    required_slots: list[str],
    slots: dict,
) -> list[str]:
    return [
        slot
        for slot in required_slots
        if not slots.get(slot)
    ]


def build_clarification_question(
    missing_slots: list[str],
    slots: dict,
) -> str:
    if missing_slots == ["order_id"]:
        return (
            "请您提供订单号，我才能继续查询订单状态并判断售后方案。"
        )

    if missing_slots == ["new_address"]:
        order_text = (
            f"订单号 {slots['order_id']} 已收到，"
            if slots.get("order_id")
            else ""
        )

        return (
            f"{order_text}"
            "请继续提供新的收货地址，我才能为您创建地址修改工单。"
        )

    if (
        "order_id" in missing_slots
        and "new_address" in missing_slots
    ):
        return (
            "请您提供订单号和新的收货地址，"
            "我才能继续为您创建地址修改工单。"
        )

    return "请您补充必要信息后，我再继续处理。"


def can_retrieve_policy_while_clarifying(route: RouteDecision) -> bool:
    """Only explain eligibility policy while waiting for an order number."""
    return bool(
        route.need_clarification and route.intent == "return_refund"
        and route.action_type == "query" and route.topic == "refund_eligibility"
        and route.need_policy and not route.order_id
        and not any((route.blocked_by_guardrail, route.need_order, route.need_ticket,
                     route.need_refund_request, route.need_risk_check,
                     route.manual_review_required, route.need_handoff, route.handoff_required))
    )


def apply_slot_requirements(
    route: RouteDecision,
    required_slots: list[str],
    slots: dict,
) -> tuple[RouteDecision, list[str], list[str]]:
    if route.blocked_by_guardrail:
        return route, [], []

    # Pre-routing keywords are only hints. A consultation must never inherit
    # execution-only slots, including from a previously pending operation.
    final_required_slots = []
    if route.action_type == "execute":
        final_required_slots = [slot for slot in required_slots if slot not in {"order_id", "new_address"}]
        if route.intent == "address_change":
            final_required_slots.extend(["order_id", "new_address"])

    if (
        route.need_clarification
        and "order_id" not in final_required_slots
    ):
        final_required_slots.append("order_id")

    missing_slots = get_missing_slots(
        final_required_slots,
        slots,
    )

    if missing_slots:
        route.need_clarification = True
        route.clarification_question = (
            build_clarification_question(
                missing_slots,
                slots,
            )
        )

        # Preserve the missing-order task; consultation may retrieve policy,
        # but may not execute the user's potential future business operation.
        route.tool_plan = ["policy_search"] if can_retrieve_policy_while_clarifying(route) else []

    return route, missing_slots, final_required_slots


def should_store_pending_task(
    route: RouteDecision,
    missing_slots: list[str],
) -> bool:
    return (
        route.need_clarification
        and bool(missing_slots)
    )


def build_pending_task(
    user_message: str,
    route: RouteDecision,
    slots: dict,
    required_slots: list[str],
    missing_slots: list[str],
) -> dict:
    return {
        "user_request": user_message,
        "slots": slots,
        "required_slots": required_slots,
        "missing_slots": missing_slots,
        "handoff_required": route.handoff_required,
        "handoff_reason": route.handoff_reason,
        "risk_level": route.risk_level,
    }
