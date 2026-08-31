from app.agent.state import AgentResult, AgentState
from app.tools.executor import execute_agent_tool


class RiskAgent:
    key = "risk_agent"
    name = "风控 Agent"
    responsibility = "售后风险评分、人工审核判断、异常账号与高危话术检测"

    def should_handle(self, route) -> bool:
        return route.need_risk_check or route.handoff_required or route.manual_review_required

    def planned_tools(self, route) -> list[str]:
        if route.need_risk_check:
            return ["risk_check"]

        return []

    def run(self, state: AgentState) -> AgentResult:
        if not state.route.need_risk_check or not state.order_id:
            return AgentResult(agent=self.key, success=True)

        result = execute_agent_tool(
            agent_key=self.key,
            tool_name="risk_check",
            arguments={
                "order_id": state.order_id,
                "user_request": state.user_message,
            },
            trace=state.trace,
            fallback_action="handoff_to_human",
        )

        return AgentResult(
            agent=self.key,
            success=result.success,
            state_updates={"risk": result.result if result.success else None},
            tool_results=[result],
            error=None if result.success else str(result.result),
        )
