from collections.abc import AsyncGenerator

from app.agent.agents import AfterSalesAgent, CustomerAgent, RiskAgent
from app.agent.routing.router import route_tools
from app.agent.state import AgentResult, AgentState
from app.agent.tools.tool_results import get_tool_result
from app.agent.tools.tool_validation import validate_tool_chain, validate_tool_plan
from app.core.schemas import RouteDecision, ToolResult
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
        route = route_tools(user_message)
        route.agent_plan = self.build_agent_plan(route)
        return route

    def should_skip_agents(self, route: RouteDecision) -> bool:
        return (
            route.blocked_by_guardrail
            or route.need_clarification
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
        risk = state.risk or {}

        return bool(
            state.manual_review is not None
            and state.refund is None
            and state.route.need_refund_request
            and (
                state.route.manual_review_required
                or risk.get("review_required")
            )
        )

    def decide_next_agent(self, state: AgentState) -> str | None:
        if state.blocked:
            return None

        if state.next_agent:
            next_agent = state.next_agent
            state.next_agent = None
            return next_agent

        route = state.route

        if route.need_order and route.order_id and state.order is None:
            return "after_sales_agent"

        if route.need_policy and state.policy is None:
            return "customer_agent"

        if route.need_risk_check and state.risk is None:
            return "risk_agent"

        if self.needs_manual_review(state):
            return "after_sales_agent"

        if self.manual_review_blocks_auto_refund(state):
            if route.need_handoff and state.handoff is None:
                return "after_sales_agent"

            return None

        if route.need_refund_request and state.refund is None:
            return "after_sales_agent"

        if route.need_ticket and state.ticket is None:
            refund_result = get_tool_result(state.tool_results, "refund_apply")
            if not route.need_refund_request or (refund_result and refund_result.success):
                return "after_sales_agent"

        if route.need_handoff and state.handoff is None:
            return "after_sales_agent"

        return None

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

        if self.should_skip_agents(route):
            return state

        plan_validation_result = self.validate_route_plan(route, trace)
        if plan_validation_result:
            state.add_tool_result(plan_validation_result)
            return state

        for _ in range(12):
            next_agent = self.decide_next_agent(state)

            if next_agent is None:
                break

            self.dispatch_agent(next_agent, state)
        else:
            self.block_execution(state, "agent_loop_guard_reached")

        self.append_chain_validation_result(state)

        return state

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
