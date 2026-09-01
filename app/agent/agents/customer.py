from app.core.schemas import RouteDecision
from app.agent.policies.evidence_guardrail import apply_policy_evidence_guardrail
from app.agent.state import AgentResult, AgentState
from app.observability.tracing import add_trace_event
from app.rag.query_builder import build_rag_query
from app.tools.executor import execute_agent_tool


class CustomerAgent:
    key = "customer_agent"
    name = "客服 Agent"
    responsibility = "普通咨询、意图识别、知识库检索和回复生成"

    def should_handle(self, route: RouteDecision) -> bool:
        return route.need_policy or not route.tool_plan

    def planned_tools(self, route: RouteDecision) -> list[str]:
        if route.need_policy:
            return ["policy_search"]

        return []

    def run(self, state: AgentState) -> AgentResult:
        if not state.route.need_policy:
            return AgentResult(agent=self.key, success=True)

        query = build_rag_query(
            user_message=state.user_message,
            route=state.route,
            tool_results=state.tool_results,
        )
        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="policy_search",
            arguments={
                "semantic_query": query.semantic_query,
                "lexical_query": query.lexical_query,
            },
            trace=state.trace,
            fallback_action="handoff_to_human",
        )
        result = apply_policy_evidence_guardrail(state.user_message, result)

        if state.trace:
            report = {}
            if isinstance(result.result, dict):
                report = result.result.get("guardrail_report", {})
            elif result.success and isinstance(result.result, list) and result.result:
                report = result.result[0].get("evidence_guardrail", {})

            add_trace_event(
                state.trace,
                event_type="evidence_guardrail",
                data={
                    "passed": result.success,
                    "report": report,
                },
            )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"policy": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )
