from collections.abc import AsyncGenerator

from app.agent.agents import AfterSalesAgent, CustomerAgent, RiskAgent
from app.agent.routing.router import route_tools
from app.core.schemas import RouteDecision
from app.observability.tracing import add_trace_event


class AgentOrchestrator:
    """系统统一编排入口：路由请求、选择 Agent、分发工具、汇总结果。"""

    def __init__(self) -> None:
        self.customer_agent = CustomerAgent()
        self.after_sales_agent = AfterSalesAgent()
        self.risk_agent = RiskAgent()

    @property
    def agents(self) -> list:
        return [self.customer_agent, self.after_sales_agent, self.risk_agent]

    def build_agent_plan(self, route: RouteDecision) -> list[str]:
        agents = [
            agent.name
            for agent in self.agents
            if agent.should_handle(route)
        ]

        return agents or [self.customer_agent.name]

    def describe_plan(self, route: RouteDecision) -> dict:
        return {
            "entry": "FastAPI",
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

    def dispatch_tools(
        self,
        user_message: str,
        route: RouteDecision,
        trace: dict | None = None,
    ):
        if trace:
            add_trace_event(
                trace,
                event_type="orchestrator_dispatch",
                data=self.describe_plan(route),
            )

        from app.agent.tools.tool_executor import execute_tools

        return execute_tools(
            user_message=user_message,
            route=route,
            trace=trace,
        )

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


def run_orchestrated_tools(
    user_message: str,
    route: RouteDecision,
    trace: dict | None = None,
):
    return DEFAULT_ORCHESTRATOR.dispatch_tools(user_message, route, trace)
