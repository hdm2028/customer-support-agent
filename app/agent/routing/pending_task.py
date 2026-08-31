from app.agent.routing.router import build_tool_plan, extract_order_id
from app.core.schemas import RouteDecision


ADDRESS_CHANGE_KEYWORDS = [
    "改收货地址",
    "修改地址",
    "改地址",
    "修改收货地址",
    "换地址",
    "修改为",
]


def is_order_id_only_message(user_message: str) -> bool:
    return extract_order_id(user_message) is not None and len(user_message.strip()) <= 12


def is_address_change_request(user_message: str) -> bool:
    return any(keyword in user_message for keyword in ADDRESS_CHANGE_KEYWORDS)


def clean_slot_value(value: str) -> str:
    return value.strip().strip("。；;，,：: ")


def extract_new_address(
    user_message: str,
    pending_task: dict | None = None,
) -> str | None:
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

    missing_slots = pending_task.get("missing_slots", []) if pending_task else []

    if "new_address" in missing_slots and not is_order_id_only_message(user_message):
        cleaned = clean_slot_value(user_message)

        if len(cleaned) >= 6:
            return cleaned

    return None


def infer_required_slots(user_message: str, pending_task: dict | None = None) -> list[str]:
    if pending_task:
        return list(pending_task.get("required_slots", pending_task.get("missing_slots", [])))

    if is_address_change_request(user_message):
        return ["order_id", "new_address"]

    return []


def collect_slots(user_message: str, pending_task: dict | None = None) -> dict:
    slots = dict(pending_task.get("slots", {})) if pending_task else {}
    order_id = extract_order_id(user_message)
    new_address = extract_new_address(user_message, pending_task)

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

    original_request = pending_task.get("user_request", user_message)
    message_parts = [original_request]

    if slots.get("order_id"):
        message_parts.append(f"订单 {slots['order_id']}")

    if slots.get("new_address"):
        message_parts.append(f"新收货地址：{slots['new_address']}")

    return " ".join(message_parts), True


def prepare_pending_task_context(
    user_message: str,
    pending_task: dict | None,
) -> tuple[str, bool, dict, list[str]]:
    slots = collect_slots(user_message, pending_task)
    required_slots = infer_required_slots(user_message, pending_task)
    effective_user_message, used_pending_task = build_effective_user_message(
        user_message=user_message,
        pending_task=pending_task,
        slots=slots,
    )

    return effective_user_message, used_pending_task, slots, required_slots


def get_missing_slots(required_slots: list[str], slots: dict) -> list[str]:
    return [
        slot for slot in required_slots
        if not slots.get(slot)
    ]


def build_clarification_question(missing_slots: list[str], slots: dict) -> str:
    if missing_slots == ["order_id"]:
        return "请您提供订单号，我才能继续查询订单状态并判断售后方案。"

    if missing_slots == ["new_address"]:
        order_text = f"订单号 {slots['order_id']} 已收到，" if slots.get("order_id") else ""
        return f"{order_text}请继续提供新的收货地址，我才能为您创建地址修改工单。"

    if "order_id" in missing_slots and "new_address" in missing_slots:
        return "请您提供订单号和新的收货地址，我才能继续为您创建地址修改工单。"

    return "请您补充必要信息后，我再继续处理。"


def apply_slot_requirements(
    route: RouteDecision,
    required_slots: list[str],
    slots: dict,
) -> tuple[RouteDecision, list[str]]:
    if route.blocked_by_guardrail:
        return route, []

    final_required_slots = list(required_slots)

    if route.need_clarification and "order_id" not in final_required_slots:
        final_required_slots.append("order_id")

    missing_slots = get_missing_slots(final_required_slots, slots)

    if missing_slots:
        route.need_clarification = True
        route.clarification_question = build_clarification_question(missing_slots, slots)
        route.tool_plan = build_tool_plan(route)

    return route, missing_slots


def should_store_pending_task(route: RouteDecision, missing_slots: list[str]) -> bool:
    return route.need_clarification and bool(missing_slots)


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
