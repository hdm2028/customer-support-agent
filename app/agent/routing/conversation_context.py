from app.agent.policies.fallback_policy import requires_order_id
from app.agent.routing.router import extract_order_id


ISSUE_CONTEXT_KEYWORDS = [
    "不想要",
    "不要了",
    "退款",
    "退货",
    "换货",
    "保修",
    "维修",
    "坏了",
    "故障",
    "物流",
    "发货",
    "没更新",
    "不更新",
    "投诉",
    "发票",
    "取消",
    "改地址",
    "修改地址",
]


def is_short_followup(message: str) -> bool:
    """判断用户当前输入是否像一个依赖上下文的短追问。"""

    cleaned = message.strip()

    return len(cleaned) <= 20 and any(
        keyword in cleaned
        for keyword in ISSUE_CONTEXT_KEYWORDS
    )


def should_inherit_order_id(message: str) -> bool:
    """判断当前问题没有订单号时，是否应该尝试沿用历史里的最近订单号。"""

    if extract_order_id(message):
        return False

    return requires_order_id(message) or is_short_followup(message)


def find_recent_order_id(history: list[dict]) -> str | None:
    """从最近的多轮聊天历史里查找最后一次出现的订单号。"""

    for message in reversed(history):
        order_id = extract_order_id(message.get("content", ""))

        if order_id:
            return order_id

    return None


def find_recent_issue_message(history: list[dict]) -> str | None:
    """找出最近一条像售后诉求的用户消息，用于用户下一轮只补订单号的场景。"""

    for message in reversed(history):
        if message.get("role") != "user":
            continue

        content = message.get("content", "").strip()

        if extract_order_id(content):
            continue

        if requires_order_id(content) or is_short_followup(content):
            return content

    return None


def apply_conversation_context(
    user_message: str,
    history: list[dict],
    used_pending_task: bool,
) -> tuple[str, bool, dict]:
    """把普通多轮聊天历史合并进当前请求。

    pending task 处理的是“系统明确知道还缺什么”的场景；
    conversation context 处理的是“用户自然省略了前文信息”的场景。
    """

    if used_pending_task:
        return user_message, False, {}

    current_order_id = extract_order_id(user_message)

    # 场景一：用户先说“耳机坏了/想退款”，下一轮只补“10001”。
    if current_order_id and user_message.strip() == current_order_id:
        recent_issue = find_recent_issue_message(history)

        if recent_issue:
            effective_message = f"{recent_issue} 订单 {current_order_id}"
            return effective_message, True, {
                "reason": "order_id_only_followup",
                "inherited_issue": recent_issue,
                "order_id": current_order_id,
            }

    # 场景二：用户上一轮已经给过订单号，下一轮只说“退款/投诉/还没到”。
    if should_inherit_order_id(user_message):
        recent_order_id = find_recent_order_id(history)

        if recent_order_id:
            effective_message = f"订单 {recent_order_id} {user_message}"
            return effective_message, True, {
                "reason": "inherit_recent_order_id",
                "order_id": recent_order_id,
            }

    return user_message, False, {}
