from app.core.schemas import RouteDecision


class CustomerAgent:
    """客服 Agent：负责普通咨询、政策问答、客服 SOP 和知识库检索。"""

    key = "customer_agent"
    name = "客服 Agent"
    responsibility = "普通咨询、意图识别、知识库检索和回复生成"

    def should_handle(self, route: RouteDecision) -> bool:
        return route.need_policy or not route.tool_plan

    def planned_tools(self, route: RouteDecision) -> list[str]:
        if route.need_policy:
            return ["policy_search"]

        return []


# Backward-compatible alias used by older scripts and trace wording.
CustomerQAAgent = CustomerAgent
