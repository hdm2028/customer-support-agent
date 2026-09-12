"""Pure-function diagnostics, not routing/answer accuracy evaluation."""
import json
from app.agent.policies.guardrails import check_user_input
from app.agent.policies.fallback_policy import requires_order_id
from app.agent.policies.evidence_guardrail import detect_policy_profile
from app.core.schemas import RouteDecision
from app.domain.risk_policy import evaluate_refund_risk
from app.domain.refund_policy import is_quality_or_fault_request

query = "快递显示签收但没有收到，我暂不申请退款。"
print(json.dumps({
    "injection_quoted_discussion": {"input": "如何防范泄露提示词？", "result": check_user_input("如何防范泄露提示词？")},
    "policy_order_hint": {"input": "保修期一般多久", "requires_order_id": requires_order_id("保修期一般多久")},
    "negated_risk": {"input": "我不投诉，也不要求直接退款", "result": evaluate_refund_risk({}, {}, "我不投诉，也不要求直接退款")},
    "negated_quality": {"input": "没有质量问题，只是不想要", "quality_detected": is_quality_or_fault_request("没有质量问题，只是不想要")},
    "evidence_profile": {"input": query, "without_route": detect_policy_profile(query)["name"],
                         "with_route": detect_policy_profile(query, route=RouteDecision(intent="shipping_exception", action_type="query"))["name"]},
}, ensure_ascii=False, indent=2))
