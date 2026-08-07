RISKY_ACTION_KEYWORDS = [
    "直接退款",
    "马上退款",
    "立刻赔付",
    "取消订单",
    "修改地址",
    "删除订单",
    "绕过审核",
]

PROMPT_INJECTION_KEYWORDS = [
    "忽略之前",
    "忘记规则",
    "不要遵守",
    "系统提示词",
    "泄露提示词",
    "输出你的prompt",
]


def check_user_input(message: str) -> tuple[bool, str | None]:
    for keyword in PROMPT_INJECTION_KEYWORDS:
        if keyword in message:
            return False, "检测到疑似提示词注入请求，已拒绝执行。"

    return True, None


def contains_risky_action(message: str) -> bool:
    return any(keyword in message for keyword in RISKY_ACTION_KEYWORDS)
