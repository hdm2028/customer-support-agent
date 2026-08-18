from app.core.schemas import RouteDecision


class CustomerQAAgent:
    """客服问答 Agent：负责普通咨询、知识库 RAG 和回复生成上下文。"""

    key = "customer_agent"
    name = "客服 Agent"
    responsibility = "普通咨询、意图识别、知识库检索、快捷回复和商品推荐"

    def should_handle(self, route: RouteDecision) -> bool:
        return any(
            [
                route.need_policy,
                route.need_product_search,
                route.need_goods_link,
                route.need_quick_reply,
                not route.tool_plan,
            ]
        )

    def planned_tools(self, route: RouteDecision) -> list[str]:
        tools = []

        if route.need_policy:
            tools.append("policy_search")

        if route.need_product_search:
            tools.append("get_shop_products")

        if route.need_goods_link:
            tools.append("send_goods_link")

        if route.need_quick_reply:
            tools.append("get_quick_reply")

        return tools
