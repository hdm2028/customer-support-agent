from app.storage.database import get_customer_profile_from_db


HIGH_RISK_AMOUNT = 1000
MEDIUM_RISK_AMOUNT = 500

COMPLAINT_KEYWORDS = ["投诉", "曝光", "差评", "起诉", "报警", "12315"]
BYPASS_KEYWORDS = ["不要审核", "不用审核", "绕过审核", "跳过审核", "直接退款", "马上退款"]
FALSE_DESCRIPTION_PATTERNS = [
    ("未收到", "已签收"),
    ("没收到", "已签收"),
    ("没签收", "已签收"),
]


class RiskControlAgent:
    """风控 Agent：检测高频退款、异常账号、恶意投诉和虚假描述。"""

    key = "risk_agent"
    name = "风控 Agent"
    responsibility = "售后风险评分、人工审核判断、异常账号与高危话术检测"

    def should_handle(self, route) -> bool:
        return route.need_risk_check or route.handoff_required or route.manual_review_required


def evaluate_risk(order: dict, user_request: str, profile: dict | None = None) -> dict:
    """输出可解释风险评分，分数越高越需要人工审核。"""

    profile = profile or get_customer_profile_from_db(order.get("user_id")) or {}
    flags = []
    score = 0
    amount = float(order.get("amount") or 0)

    if profile.get("account_status") == "abnormal":
        score += 40
        flags.append("账号异常")

    if int(profile.get("refund_count_30d") or 0) >= 3:
        score += 30
        flags.append("30天内高频退款")

    if int(profile.get("complaint_count_30d") or 0) >= 2:
        score += 20
        flags.append("近期投诉频繁")

    if amount >= HIGH_RISK_AMOUNT:
        score += 35
        flags.append("大额退款")
    elif amount >= MEDIUM_RISK_AMOUNT:
        score += 15
        flags.append("中额退款")

    if any(keyword in user_request for keyword in COMPLAINT_KEYWORDS):
        score += 20
        flags.append("投诉升级话术")

    if any(keyword in user_request for keyword in BYPASS_KEYWORDS):
        score += 35
        flags.append("要求绕过审核")

    shipping_status = order.get("shipping_status") or ""
    for user_keyword, order_keyword in FALSE_DESCRIPTION_PATTERNS:
        if user_keyword in user_request and order_keyword in shipping_status:
            score += 35
            flags.append("描述与物流状态冲突")
            break

    risk_level = "low"
    if score >= 70:
        risk_level = "high"
    elif score >= 40:
        risk_level = "medium"

    review_required = risk_level != "low"

    return {
        "risk_level": risk_level,
        "risk_score": min(score, 100),
        "risk_flags": flags,
        "review_required": review_required,
        "review_reason": "、".join(flags) if flags else "未命中明显风险规则",
        "profile": {
            "user_id": profile.get("user_id"),
            "account_status": profile.get("account_status", "unknown"),
            "refund_count_30d": profile.get("refund_count_30d", 0),
            "complaint_count_30d": profile.get("complaint_count_30d", 0),
        },
    }
