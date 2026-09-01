from app.agent.tools.tool_results import get_order_lookup_result
from app.core.schemas import RouteDecision, ToolResult
from app.rag.query_context import RAGQueryContext, RetrievalQuery


INTENT_RETRIEVAL_TERMS: dict[str, tuple[str, ...]] = {
    "address_change": ("收货地址", "地址修改"),
    "cancel_order": ("取消订单", "订单撤销"),
    "return_refund": ("退款", "退货退款"),
    "shipping_exception": ("物流", "物流异常"),
    "warranty_repair": ("保修", "维修"),
    "payment_invoice": ("支付", "发票"),
    "complaint": ("投诉", "升级处理"),
    "membership": ("会员", "会员权益"),
    "order_lookup": ("订单状态",),
}


TOPIC_RETRIEVAL_TERMS: dict[str, tuple[str, ...]] = {
    "refund_policy": ("退款政策", "退货规则", "七天无理由"),
    "refund_timing": ("退款到账", "退款时效"),
    "refund_eligibility": ("退款条件", "退款资格"),
    "refund_apply": ("退款申请",),
    "return_apply": ("退货申请",),
    "cancel_policy": ("取消政策", "取消条件"),
    "cancel_apply": ("取消申请",),
    "address_change_policy": ("地址修改政策", "地址修改条件"),
    "address_change_apply": ("地址修改申请",),
    "shipping_status": ("物流状态", "快递状态"),
    "shipping_delay": ("物流延迟", "配送延迟"),
    "shipping_exception": ("物流异常", "运输异常"),
    "lost_package": ("包裹丢失", "丢件"),
    "shipping_policy": ("物流政策", "配送规则"),
    "warranty_policy": ("保修政策", "保修范围"),
    "repair_apply": ("维修申请", "售后维修"),
    "replacement": ("换货", "换新"),
    "product_failure": ("商品故障", "质量问题", "商品破损"),
    "payment_status": ("支付状态",),
    "payment_failed": ("支付失败", "支付异常"),
    "duplicate_charge": ("重复扣款",),
    "invoice_policy": ("发票政策", "开票规则"),
    "invoice_apply": ("发票申请", "电子发票"),
    "invoice_change": ("发票修改", "发票抬头"),
    "complaint": ("投诉处理",),
    "escalation": ("投诉升级", "升级处理"),
    "membership_policy": ("会员政策",),
    "membership_benefit": ("会员权益",),
    "order_status": ("订单状态",),
    "general_question": (),
    "human_handoff": ("人工客服", "人工处理"),
}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def build_rag_query_context(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
) -> RAGQueryContext:
    """汇总上游语义结论和已查询到的订单事实。"""

    order_result = get_order_lookup_result(tool_results)
    order = (
        order_result.result
        if (
            order_result
            and order_result.success
            and isinstance(order_result.result, dict)
        )
        else {}
    )

    return RAGQueryContext(
        raw_query=user_message.strip(),
        primary_intent=route.intent,
        action_type=route.action_type,
        topic=route.topic,
        related_topics=list(route.related_topics),
        order_status=_optional_text(order.get("order_status")),
        shipping_status=_optional_text(order.get("shipping_status")),
        product_name=_optional_text(order.get("product_name")),
        product_category=_optional_text(order.get("category")),
        signed_date=_optional_text(order.get("signed_date")),
        handoff_required=route.handoff_required,
    )


def _semantic_query(context: RAGQueryContext) -> str:
    parts = [context.raw_query]
    facts = (
        ("订单状态", context.order_status),
        ("物流状态", context.shipping_status),
        ("商品名称", context.product_name),
        ("商品类目", context.product_category),
        ("签收日期", context.signed_date),
    )

    parts.extend(f"{label}：{value}" for label, value in facts if value)

    if context.handoff_required:
        parts.append("处理约束：需要人工处理")

    return "\n".join(part for part in parts if part)


def _lexical_query(context: RAGQueryContext) -> str:
    terms = [context.raw_query]
    terms.extend(INTENT_RETRIEVAL_TERMS.get(context.primary_intent or "", ()))

    topics = [context.topic, *context.related_topics]
    for topic in topics:
        if topic:
            terms.extend(TOPIC_RETRIEVAL_TERMS.get(topic, ()))

    terms.extend(
        value
        for value in (
            context.order_status,
            context.shipping_status,
            context.product_name,
            context.product_category,
            context.signed_date,
        )
        if value
    )

    if context.handoff_required:
        terms.append("人工处理")

    return " ".join(dict.fromkeys(term for term in terms if term))


def build_retrieval_query(context: RAGQueryContext) -> RetrievalQuery:
    return RetrievalQuery(
        semantic_query=_semantic_query(context),
        lexical_query=_lexical_query(context),
    )


def build_rag_query(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
) -> RetrievalQuery:
    context = build_rag_query_context(
        user_message=user_message,
        route=route,
        tool_results=tool_results,
    )

    return build_retrieval_query(context)
