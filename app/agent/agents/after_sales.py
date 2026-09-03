from app.agent.policies.ticket_policy import evaluate_ticket_creation
from app.agent.routing.router import get_issue_type
from app.agent.state import AgentResult, AgentState
from app.agent.tools.tool_results import get_tool_result
from app.core.schemas import RouteDecision, ToolResult
from app.domain.refund_policy import (
    days_since_signed,
    evaluate_refund_eligibility,
    infer_refund_reason,
    is_quality_or_fault_request,
)
from app.tools.executor import execute_agent_tool, safe_tool_call


class AfterSalesAgent:
    """售后 Agent：负责订单查询、退款申请、售后工单和人工审核流转。"""

    key = "after_sales_agent"
    name = "售后 Agent"
    responsibility = "订单查询、退款申请、售后工单、MQ 任务和业务流程执行"

    def should_handle(self, route: RouteDecision) -> bool:
        return any(
            [
                route.need_order,
                route.need_refund_request,
                route.need_ticket,
                route.need_handoff,
                route.handoff_required,
                route.manual_review_required,
            ]
        )

    def planned_tools(self, route: RouteDecision) -> list[str]:
        tools = []

        if route.need_order and route.order_id:
            tools.append("order_lookup")

        if route.need_refund_request:
            tools.append("refund_apply")

        if route.need_ticket:
            tools.append("create_ticket")

        if route.manual_review_required:
            tools.append("create_manual_review")

        if route.need_handoff:
            tools.append("transfer_to_human")

        return tools

    def run(self, state: AgentState) -> AgentResult:
        route = state.route

        if route.need_order and route.order_id and state.order is None:
            return self.lookup_order(state)

        if route.need_risk_check and state.risk is None:
            return AgentResult(agent=self.key, success=True, next_hint="risk_agent")

        if self.should_create_manual_review(state):
            return self.create_manual_review(state)

        if self.manual_review_blocks_auto_refund(state):
            if route.need_handoff and state.handoff is None:
                return self.transfer_to_human(state)

            return AgentResult(agent=self.key, success=True)

        if route.need_refund_request and state.refund is None:
            return self.apply_refund(state)

        if route.need_ticket and state.ticket is None:
            refund_result = get_tool_result(state.tool_results, "refund_apply")
            if not route.need_refund_request or (refund_result and refund_result.success):
                return self.create_ticket(state)

        if route.need_handoff and state.handoff is None:
            return self.transfer_to_human(state)

        return AgentResult(agent=self.key, success=True)

    def lookup_order(self, state: AgentState) -> AgentResult:
        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="order_lookup",
            arguments={"order_id": state.order_id},
            trace=state.trace,
            fallback_action="ask_user_to_retry_or_handoff",
        )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"order": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )

    def apply_refund(self, state: AgentState) -> AgentResult:
        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="refund_apply",
            arguments={
                "order_id": state.order_id,
                "user_request": state.user_message,
                "risk_assessment": state.risk,
            },
            trace=state.trace,
            fallback_action="create_manual_review_or_explain_policy",
        )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"refund": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )

    def create_ticket(self, state: AgentState) -> AgentResult:
        order = state.order
        issue_type = get_issue_type(state.route.intent)
        decision_result = safe_tool_call(
            "ticket_decision",
            lambda: evaluate_ticket_creation(
                route=state.route,
                order=order,
                issue_type=issue_type,
                user_message=state.user_message,
            ),
            fallback_action="handoff_to_human",
        )

        if not decision_result.success:
            return AgentResult(
                agent=self.key,
                success=False,
                tool_results=[decision_result],
                error=str(decision_result.result),
            )

        decision = decision_result.result
        if not decision["can_create"]:
            return AgentResult(
                agent=self.key,
                success=False,
                tool_results=[ToolResult(tool_name="ticket_decision", success=False, result=decision)],
                error=decision.get("reason"),
            )

        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="create_ticket",
            arguments={
                "order_id": state.order_id,
                "issue_type": issue_type,
                "user_request": state.user_message,
                "priority": decision["priority"],
            },
            trace=state.trace,
            fallback_action="retry_or_handoff_to_human",
        )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"ticket": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )

    def should_create_manual_review(self, state: AgentState) -> bool:
        if state.manual_review is not None or not state.order_id:
            return False

        if state.route.need_risk_check and state.risk is None:
            return False

        risk = state.risk or {}
        refund_result = get_tool_result(state.tool_results, "refund_apply")
        refund = refund_result.result if refund_result and isinstance(refund_result.result, dict) else {}

        return bool(
            state.route.manual_review_required
            or risk.get("review_required")
            or refund.get("status") == "pending_manual_review"
            or (
                refund_result
                and not refund_result.success
                and isinstance(refund_result.result, dict)
                and refund_result.result.get("review_required")
            )
        )

    def manual_review_blocks_auto_refund(self, state: AgentState) -> bool:
        if (
            state.manual_review is None
            or state.refund is not None
            or not state.route.need_refund_request
        ):
            return False

        risk = state.risk or {}

        refund_result = get_tool_result(
            state.tool_results,
            "refund_apply",
        )

        refund_requires_review = bool(
            refund_result
            and isinstance(refund_result.result, dict)
            and (
                refund_result.result.get("review_required")
                or refund_result.result.get("status") == "pending_manual_review"
            )
        )

        return bool(
            state.route.manual_review_required
            or risk.get("review_required")
            or refund_requires_review
        )

    def create_manual_review(self, state: AgentState) -> AgentResult:
        risk = state.risk or {}
        refund = state.refund or {}
        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="create_manual_review",
            arguments={
                "order_id": state.order_id,
                "review_type": "refund" if state.route.need_refund_request else "risk_control",
                "risk_level": risk.get("risk_level", state.route.risk_level),
                "risk_flags": risk.get("risk_flags", state.route.risk_flags),
                "user_request": state.user_message,
                "related_id": refund.get("refund_id"),
            },
            trace=state.trace,
            fallback_action="manual_queue",
        )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"manual_review": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )

    def transfer_to_human(self, state: AgentState) -> AgentResult:
        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="transfer_to_human",
            arguments={
                "reason": state.route.handoff_reason or "用户要求人工客服或该场景需要人工接管。",
                "user_request": state.user_message,
                "priority": "high" if state.route.risk_level == "high" else "normal",
            },
            trace=state.trace,
            fallback_action="manual_queue",
        )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"handoff": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )
