from __future__ import annotations

from app.agent.policies.fallback_policy import (
    should_handoff_to_human,
)
from app.agent.policies.guardrails import (
    contains_risky_action,
)
from app.agent.routing.semantic import (
    SemanticRoute,
)
from app.core.schemas import (
    RouteDecision,
)


POLICY_TOPICS = {
    "refund_policy",
    "refund_timing",
    "refund_eligibility",
    "return_policy",
    "cancel_policy",
    "address_change_policy",
    "warranty_policy",
    "shipping_policy",
    "invoice_policy",
    "membership_policy",
}


REFUND_EXECUTION_TOPICS = {
    "refund_apply",
    "return_apply",
}


# --------------------------------
# 明确的审核绕过表达
#
# 注意：
# 这不是业务意图 Router。
# 这里只负责检测安全风险。
# --------------------------------

REVIEW_BYPASS_PATTERNS = (
    "不用审核",
    "不要审核",
    "别审核",
    "不需要审核",
    "无需审核",
    "跳过审核",
    "绕过审核",
    "免审核",
    "别走审核",
    "不要走审核",
    "不走审核",
    "跳过审核流程",
    "绕过审核流程",
    "别走审核流程",
    "不要走审核流程",
)


def requests_review_bypass(
    user_message: str,
) -> bool:
    """
    检测用户是否明确要求绕过审核。

    这里只检测安全风险，
    不负责判断业务 intent。
    """

    text = (
        user_message
        .strip()
        .lower()
    )

    return any(
        pattern in text
        for pattern
        in REVIEW_BYPASS_PATTERNS
    )


def requires_order_id(
    semantic: SemanticRoute,
) -> bool:
    """
    当前业务是否需要具体订单。

    注意：
    “需要订单号”
    不等于
    “当前已经有订单号”。
    """

    if (
        semantic.action_type
        == "handoff"
    ):
        return False

    if (
        semantic.intent
        == "order_lookup"
    ):
        return True

    if (
        semantic.intent
        == "cancel_order"
    ):
        return True

    if (
        semantic.intent
        == "address_change"
    ):
        return True

    if (
        semantic.intent
        == "return_refund"
    ):
        # 真正执行退款
        if (
            semantic.action_type
            == "execute"
        ):
            return True

        # 针对具体商品/订单询问
        # 是否符合退款条件
        if (
            semantic.topic
            == "refund_eligibility"
        ):
            return True

        # refund_policy /
        # refund_timing
        # 属于通用政策问题
        return False

    if (
        semantic.intent
        == "shipping_exception"
    ):
        return (
            semantic.topic
            in {
                "shipping_status",
                "shipping_delay",
                "shipping_exception",
                "lost_package",
            }
        )

    if (
        semantic.intent
        == "warranty_repair"
    ):
        return (
            semantic.action_type
            == "execute"
        )

    if (
        semantic.intent
        == "payment_invoice"
    ):
        return (
            semantic.topic
            in {
                "payment_status",
                "payment_failed",
                "duplicate_charge",
                "invoice_apply",
                "invoice_change",
            }
        )

    if (
        semantic.intent
        == "complaint"
    ):
        return (
            semantic.action_type
            == "execute"
        )

    return False


def requires_policy(
    semantic: SemanticRoute,
) -> bool:

    if (
        semantic.topic
        in POLICY_TOPICS
    ):
        return True

    # 真正执行退款前，
    # 也需要先查退款政策
    if (
        semantic.intent
        == "return_refund"
        and semantic.action_type
        == "execute"
        and semantic.topic
        in REFUND_EXECUTION_TOPICS
    ):
        return True

    return False


def requires_refund_execution(
    semantic: SemanticRoute,
    order_id: str | None,
) -> bool:

    return bool(
        semantic.intent
        == "return_refund"
        and semantic.action_type
        == "execute"
        and semantic.topic
        == "refund_apply"
        and order_id is not None
    )


def requires_ticket(
    semantic: SemanticRoute,
    order_id: str | None,
    need_refund_request: bool,
    risky_action: bool,
) -> bool:

    if order_id is None:
        return False

    if need_refund_request:
        return True

    if risky_action:
        return True

    if (
        semantic.action_type
        != "execute"
    ):
        return False

    return (
        semantic.intent
        in {
            "address_change",
            "cancel_order",
            "shipping_exception",
            "warranty_repair",
            "complaint",
            "payment_invoice",
        }
    )


def build_route_decision(
    user_message: str,
    semantic: SemanticRoute,
    order_id: str | None,
) -> RouteDecision:

    # --------------------------------
    # 1. Semantic 层已经决定：
    #    query / execute / handoff
    # --------------------------------

    is_execution = (
        semantic.action_type
        == "execute"
    )

    is_explicit_handoff = (
        semantic.action_type
        == "handoff"
    )

    # --------------------------------
    # 2. 原有人工升级规则
    #
    # 只有 execute 请求才允许
    # 原始文本风险规则影响业务决策。
    #
    # 避免：
    # “不要直接给我退款”
    # 被识别成高风险退款执行。
    # --------------------------------

    legacy_handoff_required = False
    legacy_handoff_reason = None

    if is_execution:
        (
            legacy_handoff_required,
            legacy_handoff_reason,
        ) = should_handoff_to_human(
            user_message
        )

    # --------------------------------
    # 3. 审核绕过是明确安全风险
    #
    # 同样只对 execute 生效。
    # --------------------------------

    review_bypass = bool(
        is_execution
        and requests_review_bypass(
            user_message
        )
    )

    # --------------------------------
    # 4. 其他危险动作检测
    #
    # 只在 execute 时启用。
    # --------------------------------

    risky_action = bool(
        is_execution
        and contains_risky_action(
            user_message
        )
    )

    # --------------------------------
    # 5. Order / Policy
    # --------------------------------

    order_required = (
        requires_order_id(
            semantic
        )
    )

    need_clarification = bool(
        order_required
        and order_id is None
    )

    # 保留当前项目既有行为：
    # 消息中明确提供 order_id，
    # 就允许查询订单事实。
    need_order = (
        order_id is not None
    )

    need_policy = (
        requires_policy(
            semantic
        )
    )

    # --------------------------------
    # 6. Refund Execution
    # --------------------------------

    need_refund_request = (
        requires_refund_execution(
            semantic=semantic,
            order_id=order_id,
        )
    )

    # --------------------------------
    # 7. Handoff / Manual Review
    # --------------------------------

    handoff_required = bool(
        is_explicit_handoff
        or review_bypass
        or legacy_handoff_required
    )

    need_handoff = (
        handoff_required
    )

    # 显式要求人工客服，
    # 不一定意味着风险审核。
    #
    # 只有风险/审核绕过
    # 才进入 manual review。
    manual_review_required = bool(
        review_bypass
        or legacy_handoff_required
    )

    # --------------------------------
    # 8. Risk Check
    # --------------------------------

    need_risk_check = bool(
        order_id
        and (
            need_refund_request
            or semantic.intent
            == "complaint"
            or review_bypass
            or risky_action
            or legacy_handoff_required
        )
    )

    # --------------------------------
    # 9. Ticket
    # --------------------------------

    need_ticket = (
        requires_ticket(
            semantic=semantic,
            order_id=order_id,
            need_refund_request=(
                need_refund_request
            ),
            risky_action=(
                risky_action
                or review_bypass
            ),
        )
    )

    # --------------------------------
    # 10. Risk Level
    # --------------------------------

    high_risk = bool(
        review_bypass
        or legacy_handoff_required
    )

    risk_level = (
        "high"
        if high_risk
        else "low"
    )

    risk_flags: list[str] = []

    if review_bypass:
        risk_flags.append(
            "review_bypass"
        )

    if risky_action:
        risk_flags.append(
            "risky_action"
        )

    # --------------------------------
    # 11. Handoff Reason
    # --------------------------------

    handoff_reason = None

    if review_bypass:
        handoff_reason = (
            "用户要求绕过正常审核流程，"
            "需要人工审核。"
        )

    elif legacy_handoff_required:
        handoff_reason = (
            legacy_handoff_reason
        )

    elif is_explicit_handoff:
        handoff_reason = (
            "用户明确要求人工客服处理。"
        )

    # --------------------------------
    # 12. RouteDecision
    # --------------------------------

    return RouteDecision(
        intent=semantic.intent,
        action_type=semantic.action_type,
        topic=semantic.topic,
        related_topics=list(semantic.related_topics),
        confidence=semantic.confidence,
        routing_reason=semantic.reason,

        order_id=order_id,

        need_order=need_order,
        need_policy=need_policy,
        need_ticket=need_ticket,

        need_refund_request=(
            need_refund_request
        ),

        need_risk_check=(
            need_risk_check
        ),

        manual_review_required=(
            manual_review_required
        ),

        need_handoff=(
            need_handoff
        ),

        blocked_by_guardrail=False,
        guardrail_reason=None,

        need_clarification=(
            need_clarification
        ),

        clarification_question=(
            "请您提供订单号，我才能继续查询订单状态并判断售后方案。"
            if need_clarification
            else None
        ),

        handoff_required=(
            handoff_required
        ),

        handoff_reason=(
            handoff_reason
        ),

        risk_level=(
            risk_level
        ),

        risk_flags=(
            risk_flags
        ),
    )
