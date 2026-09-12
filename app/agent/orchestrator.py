from collections.abc import AsyncGenerator

from app.agent.agents import AfterSalesAgent, CustomerAgent, RiskAgent
from app.agent.routing.router_v2 import build_tool_plan, route_tools_v2
from app.agent.routing.pending_task import can_retrieve_policy_while_clarifying
from app.agent.state import AgentResult, AgentState
from app.agent.tools.tool_results import get_tool_result
from app.agent.tools.tool_validation import validate_tool_chain, validate_tool_plan
from app.core.schemas import RouteDecision, ToolResult
from app.domain.refund_policy import RefundPaymentNotConfirmed, require_confirmed_refund_payment, existing_after_sales_reason
from app.observability.tracing import add_trace_event, timed_step
from app.tools.executor import add_tool_failure_trace


class AgentOrchestrator:
    """统一编排入口：路由请求、调度 Agent、更新共享状态、汇总工具结果。"""

    def __init__(self) -> None:
        self.agents_by_key = {
            "customer_agent": CustomerAgent(),
            "after_sales_agent": AfterSalesAgent(),
            "risk_agent": RiskAgent(),
        }

    @property
    def agents(self) -> list:
        return list(self.agents_by_key.values())

    def build_agent_plan(self, route: RouteDecision) -> list[str]:
        agents = [
            agent.name
            for agent in self.agents
            if agent.should_handle(route)
        ]

        return agents or [self.agents_by_key["customer_agent"].name]

    def describe_plan(self, route: RouteDecision) -> dict:
        return {
            "entry": "FastAPI",
            "workflow_engine": "LangGraph",
            "orchestrator": "Agent Orchestrator",
            "agents": route.agent_plan,
            "tool_plan": route.tool_plan,
            "backends": {
                "customer_agent": ["Hybrid RAG", "Vector DB", "BM25", "Rerank"],
                "after_sales_agent": ["Tool Calling", "MySQL/SQLite", "MQ"],
                "risk_agent": ["Risk Policy", "Human Review"],
                "state": ["Redis Conversation Cache", "Redis Lock", "Idempotency"],
            },
        }

    def route(self, user_message: str) -> RouteDecision:
        route = route_tools_v2(user_message)
        route.agent_plan = self.build_agent_plan(route)
        return route

    def should_skip_agents(self, route: RouteDecision) -> bool:
        return (
            route.blocked_by_guardrail
            or (route.need_clarification and not can_retrieve_policy_while_clarifying(route))
            or (route.handoff_required and not route.order_id and not route.need_handoff)
        )

    def validate_route_plan(self, route: RouteDecision, trace: dict | None) -> ToolResult | None:
        plan_valid, plan_errors = validate_tool_plan(route)

        if trace:
            add_trace_event(
                trace,
                event_type="tool_plan_validation",
                data={
                    "passed": plan_valid,
                    "errors": plan_errors,
                },
            )

        if plan_valid:
            return None

        return ToolResult(
            tool_name="tool_plan_validation",
            success=False,
            result={
                "error_type": "InvalidToolPlan",
                "error_message": "工具调用计划不合法，已停止执行。",
                "errors": plan_errors,
                "fallback_action": "ask_user_or_handoff_to_human",
            },
        )

    def apply_risk_result_to_route(self, route: RouteDecision, risk_result: ToolResult) -> None:
        if not risk_result.success or not isinstance(risk_result.result, dict):
            return

        route.risk_level = risk_result.result.get("risk_level", route.risk_level)
        route.risk_flags = risk_result.result.get("risk_flags", [])
        route.manual_review_required = bool(
            route.manual_review_required
            or risk_result.result.get("review_required")
        )

        if route.manual_review_required and "create_manual_review" not in route.tool_plan:
            route.tool_plan.append("create_manual_review")

    def needs_manual_review(self, state: AgentState) -> bool:
        if state.manual_review is not None or not state.order_id:
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
            and refund_result.result.get("review_required")
        )

        return bool(
            state.route.manual_review_required
            or risk.get("review_required")
            or refund_requires_review
        )
    def dispatch_agent(self, agent_key: str, state: AgentState) -> AgentResult:
        agent = self.agents_by_key[agent_key]
        state.current_agent = agent_key

        if state.trace:
            add_trace_event(
                state.trace,
                event_type="agent_dispatch",
                data={
                    "agent_key": agent_key,
                    "agent": agent.name,
                    "state": state.to_summary(),
                },
            )

        def work() -> AgentResult:
            return agent.run(state)

        step_index = len(state.agent_steps) + 1
        result = timed_step(
            state.trace,
            f"agent.{step_index}.{agent_key}",
            work,
            {"agent_key": agent_key, "agent": agent.name},
        ) if state.trace else work()

        state.apply_agent_result(result)
        self.handle_agent_result(state, result)

        if state.trace:
            add_trace_event(
                state.trace,
                event_type="agent_result",
                data={
                    "agent_key": agent_key,
                    "success": result.success,
                    "tool_names": [item.tool_name for item in result.tool_results],
                    "next_hint": result.next_hint,
                    "state": state.to_summary(),
                },
            )

        return result

    def handle_agent_result(self, state: AgentState, result: AgentResult) -> None:
        for tool_result in result.tool_results:
            add_tool_failure_trace(state.trace, tool_result)

            if tool_result.tool_name == "risk_check":
                self.apply_risk_result_to_route(state.route, tool_result)

            if tool_result.tool_name == "order_lookup" and not tool_result.success:
                self.block_execution(state, "order_lookup_failed")
                return

            if tool_result.tool_name == "order_lookup" and tool_result.success:
                self.apply_refund_preconditions(state)

            if (tool_result.tool_name == "refund_apply" and tool_result.success
                    and isinstance(tool_result.result, dict) and tool_result.result.get("idempotent_replay")):
                self.record_refund_precondition(state, {
                    "kind": "existing_refund", "reason": "已有退款申请，本轮不重复创建。",
                    "existing_refund": {key: tool_result.result.get(key) for key in ("refund_id", "order_id", "status")},
                    "origin": "refund_apply_replay",
                })

            if (tool_result.tool_name == "create_manual_review" and tool_result.success
                    and isinstance(tool_result.result, dict) and tool_result.result.get("idempotent_replay")):
                review = tool_result.result
                if review.get("existing_refund"):
                    self.record_refund_precondition(state, {"kind": "existing_refund", "origin": "review_creation_check",
                        "supplement_recorded": review.get("supplement_recorded", False),
                        "existing_refund": review["existing_refund"], "reason": "已有退款申请，无需再创建审核。"})
                elif review.get("review_type") == "refund":
                    self.record_refund_precondition(state, {"kind": "existing_refund_review", "origin": "review_creation_check",
                        "supplement_recorded": review.get("supplement_recorded", False),
                        "existing_review": {key: review.get(key) for key in ("review_id", "status")},
                        "reason": "已有待处理退款审核，本轮不重复创建。"})

            if tool_result.tool_name == "policy_search" and not tool_result.success:
                self.block_execution(state, "policy_search_failed")
                return

            if tool_result.tool_name == "risk_check" and not tool_result.success:
                self.block_execution(state, "risk_check_failed")
                return

            if (
                tool_result.tool_name == "refund_apply"
                and not tool_result.success
                and not (
                    isinstance(tool_result.result, dict)
                    and tool_result.result.get("review_required")
                )
            ):
                self.block_execution(state, "refund_apply_failed")
                return

            if (
                not tool_result.success
                and isinstance(tool_result.result, dict)
                and tool_result.result.get("error_type") == "ToolPermissionDenied"
            ):
                self.block_execution(state, "tool_permission_denied")
                return

    def apply_refund_preconditions(self, state: AgentState) -> None:
        if not state.route.need_refund_request or not isinstance(state.order, dict):
            return
        existing = state.order.get("active_refund")
        if existing:
            self.record_refund_precondition(state, {"kind": "existing_refund", "existing_refund": existing,
                                                   "reason": "已有退款申请，本轮不重复创建。"})
            return
        try:
            require_confirmed_refund_payment(state.order)
        except RefundPaymentNotConfirmed as error:
            self.record_refund_precondition(state, {"kind": "payment_not_confirmed", "reason": str(error)})
            return
        reason = existing_after_sales_reason(state.order)
        if reason:
            self.record_refund_precondition(state, {"kind": "order_after_sales", "reason": reason})

    def record_refund_precondition(self, state: AgentState, decision: dict) -> None:
        order = state.order or {}
        decision = {"can_create": False, "order_id": state.order_id,
                    "payment_status": order.get("payment_status"), "order_status": order.get("order_status"),
                    "origin": "order_lookup", **decision}
        # An internal business decision, not an external call or dependency failure.
        state.add_tool_result(ToolResult(tool_name="refund_decision", success=True, result=decision))
        previous_plan = list(state.route.tool_plan)
        for flag in ("need_refund_request", "need_risk_check", "need_ticket",
                     "manual_review_required", "need_handoff", "handoff_required"):
            setattr(state.route, flag, False)
        state.route.handoff_reason = None
        state.route.tool_plan = build_tool_plan(state.route)
        state.route.agent_plan = self.build_agent_plan(state.route)
        if state.trace:
            add_trace_event(state.trace, event_type="refund_precondition", data={
                **decision, "previous_tool_plan": previous_plan,
                "effective_tool_plan": list(state.route.tool_plan),
            })

    def block_execution(self, state: AgentState, reason: str) -> None:
        state.block(reason)

        if state.trace:
            add_trace_event(
                state.trace,
                event_type="execution_blocked",
                data={
                    "reason": reason,
                    "state": state.to_summary(),
                },
            )

    def append_chain_validation_result(self, state: AgentState) -> None:
        chain_valid, chain_errors = validate_tool_chain(state.route, state.tool_results)

        if state.trace:
            add_trace_event(
                state.trace,
                event_type="tool_chain_validation",
                data={
                    "passed": chain_valid,
                    "errors": chain_errors,
                    "tool_names": [item.tool_name for item in state.tool_results],
                },
            )

        if chain_valid:
            return

        state.add_tool_result(
            ToolResult(
                tool_name="tool_chain_validation",
                success=False,
                result={
                    "error_type": "InvalidToolChain",
                    "error_message": "工具执行链路不符合业务约束，已进入降级处理。",
                    "errors": chain_errors,
                    "fallback_action": "handoff_to_human",
                },
            )
        )

    def run_agent_loop(
        self,
        user_message: str,
        route: RouteDecision,
        conversation_id: str = "",
        history: list[dict] | None = None,
        pending_task: dict | None = None,
        trace: dict | None = None,
    ) -> AgentState:
        state = AgentState(
            conversation_id=conversation_id,
            user_message=user_message,
            route=route,
            history=history or [],
            pending_task=pending_task,
            trace=trace,
        )

        if trace:
            add_trace_event(
                trace,
                event_type="orchestrator_dispatch",
                data=self.describe_plan(route),
            )

        from app.agent.harness import AgentHarness

        return AgentHarness(self).run(state)

    def run(
        self,
        user_message: str,
        conversation_id: str | None = None,
        use_llm: bool = False,
    ) -> dict:
        from app.agent.entry.workflow import run_workflow

        return run_workflow(
            user_message=user_message,
            conversation_id=conversation_id,
            use_llm=use_llm,
        )

    async def stream(
        self,
        user_message: str,
        conversation_id: str | None = None,
        use_llm: bool = False,
        stream_tokens: bool = True,
    ) -> AsyncGenerator[dict, None]:
        from app.agent.entry.stream_runner import stream_workflow

        async for event in stream_workflow(
            user_message=user_message,
            conversation_id=conversation_id,
            use_llm=use_llm,
            stream_tokens=stream_tokens,
        ):
            yield event

    def history(self, conversation_id: str) -> list[dict]:
        from app.agent.entry.workflow import get_conversation_history

        return get_conversation_history(conversation_id)


DEFAULT_ORCHESTRATOR = AgentOrchestrator()


def build_agent_plan(route: RouteDecision) -> list[str]:
    return DEFAULT_ORCHESTRATOR.build_agent_plan(route)


def describe_agent_plan(route: RouteDecision) -> dict:
    return DEFAULT_ORCHESTRATOR.describe_plan(route)


def route_user_request(user_message: str) -> RouteDecision:
    return DEFAULT_ORCHESTRATOR.route(user_message)


def run_orchestrated_state(
    user_message: str,
    route: RouteDecision,
    conversation_id: str = "",
    history: list[dict] | None = None,
    pending_task: dict | None = None,
    trace: dict | None = None,
) -> AgentState:
    return DEFAULT_ORCHESTRATOR.run_agent_loop(
        user_message=user_message,
        route=route,
        conversation_id=conversation_id,
        history=history,
        pending_task=pending_task,
        trace=trace,
    )
