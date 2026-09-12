from __future__ import annotations

from app.agent.policies.guardrails import (
    check_user_input,
)
from app.agent.routing.decision import (
    build_route_decision,
)
from app.agent.routing.llm_router import (
    infer_semantic_route,
)
from app.agent.routing.parsing import (
    extract_order_id,
)
from app.agent.routing.semantic import (
    SemanticRoute,
)
from app.core.schemas import RouteDecision
from app.agent.routing.refund_withdrawal import WITHDRAWAL_TOPIC, mentions_refund_withdrawal
from app.agent.routing.pending_task import can_retrieve_policy_while_clarifying


def build_tool_plan(
    route: RouteDecision,
) -> list[str]:
    if route.blocked_by_guardrail:
        return []

    if route.need_clarification:
        return ["policy_search"] if can_retrieve_policy_while_clarifying(route) else []

    if (
        route.handoff_required
        and not route.order_id
    ):
        return []

    plan: list[str] = []

    if (
        route.need_order
        and route.order_id
    ):
        plan.append(
            "order_lookup"
        )

    if route.need_policy:
        plan.append(
            "policy_search"
        )

    if route.need_risk_check:
        plan.append(
            "risk_check"
        )

    if route.need_refund_request:
        plan.append(
            "refund_apply"
        )

    if route.manual_review_required:
        plan.append(
            "create_manual_review"
        )

    if route.need_handoff:
        plan.append(
            "transfer_to_human"
        )

    if route.need_ticket:
        plan.append(
            "create_ticket"
        )

    return plan


def route_tools_v2(
    user_message: str,
    semantic: SemanticRoute | None = None,
) -> RouteDecision:
    passed, reason = check_user_input(
        user_message
    )

    if not passed:
        return RouteDecision(
            intent="unsafe_request",
            confidence=1.0,
            routing_reason=reason,
            tool_plan=[],
            blocked_by_guardrail=True,
            guardrail_reason=reason,
        )

    if mentions_refund_withdrawal(user_message):
        return RouteDecision(
            intent="return_refund", action_type="query", topic=WITHDRAWAL_TOPIC,
            confidence=1.0, routing_reason="退款撤销需选定已有申请，通过撤销接口检查当前状态",
        )

    order_id = extract_order_id(
        user_message
    )

    if semantic is None:
        semantic = infer_semantic_route(
            user_message
        )

    route = build_route_decision(
        user_message=user_message,
        semantic=semantic,
        order_id=order_id,
    )

    route.tool_plan = build_tool_plan(
        route
    )

    return route
