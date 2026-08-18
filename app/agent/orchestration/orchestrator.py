from app.agent.orchestration.after_sales_agent import AfterSalesAgent
from app.agent.orchestration.customer_agent import CustomerQAAgent
from app.agent.orchestration.risk_agent import RiskControlAgent
from app.agent.routing.router import route_tools
from app.core.schemas import RouteDecision
from app.observability.tracing import add_trace_event


CUSTOMER_AGENT = CustomerQAAgent()
AFTER_SALES_AGENT = AfterSalesAgent()
RISK_AGENT = RiskControlAgent()


def build_agent_plan(route: RouteDecision) -> list[str]:
    """根据路由结果生成多 Agent 协作计划。"""

    agents = []

    if CUSTOMER_AGENT.should_handle(route):
        agents.append(CUSTOMER_AGENT.name)

    if AFTER_SALES_AGENT.should_handle(route):
        agents.append(AFTER_SALES_AGENT.name)

    if RISK_AGENT.should_handle(route):
        agents.append(RISK_AGENT.name)

    return agents or [CUSTOMER_AGENT.name]


def describe_agent_plan(route: RouteDecision) -> dict:
    """输出适合接口和 trace 展示的 Orchestrator 计划。"""

    return {
        "entry": "FastAPI",
        "orchestrator": "Agent Orchestrator",
        "agents": route.agent_plan,
        "tool_plan": route.tool_plan,
        "backends": {
            "customer_agent": ["知识库 RAG", "Vector DB"],
            "after_sales_agent": ["业务 Tool", "MySQL/SQLite"],
            "risk_agent": ["风险规则", "人工审核"],
            "state": ["Redis", "MQ消息队列"],
        },
    }


def route_user_request(user_message: str) -> RouteDecision:
    """FastAPI 之后的统一编排入口：先路由，再生成 Agent 协作计划。"""

    route = route_tools(user_message)
    route.agent_plan = build_agent_plan(route)
    return route


def run_orchestrated_tools(
    user_message: str,
    route: RouteDecision,
    trace: dict | None = None,
):
    """由 Orchestrator 分发到专职 Agent 执行工具链。"""

    if trace:
        add_trace_event(
            trace,
            event_type="orchestrator_dispatch",
            data=describe_agent_plan(route),
        )

    from app.agent.tools.tool_executor import execute_tools

    return execute_tools(
        user_message=user_message,
        route=route,
        trace=trace,
    )
