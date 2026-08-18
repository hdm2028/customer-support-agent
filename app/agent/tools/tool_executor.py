from app.agent.policies.evidence_guardrail import apply_policy_evidence_guardrail
from app.agent.orchestration.after_sales_agent import AfterSalesAgent
from app.agent.orchestration.customer_agent import CustomerQAAgent
from app.agent.orchestration.risk_agent import RiskControlAgent
from app.agent.routing.router import infer_issue_type
from app.agent.policies.ticket_policy import evaluate_ticket_creation
from app.agent.tools.tool_registry import execute_registered_tool
from app.agent.tools.tool_results import (
    get_tool_result,
    get_order_lookup_result,
    has_failed_order_lookup,
    has_failed_policy_search,
    is_system_tool_failure,
)
from app.agent.tools.tool_validation import validate_tool_chain, validate_tool_plan
from app.core.schemas import RouteDecision, ToolResult
from app.observability.tracing import add_trace_event, timed_step
from app.rag.query_builder import build_rag_query


CUSTOMER_AGENT = CustomerQAAgent()
AFTER_SALES_AGENT = AfterSalesAgent()
RISK_AGENT = RiskControlAgent()


def safe_tool_call(
    tool_name: str,
    callback,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
    """安全调用工具，把异常统一转成 ToolResult。"""

    try:
        result = callback()
    except Exception as error:
        return ToolResult(
            tool_name=tool_name,
            success=False,
            result={
                "error_type": type(error).__name__,
                "error_message": str(error),
                "fallback_action": fallback_action,
                "reason": f"{tool_name} 工具调用失败，已进入降级处理。",
            },
        )

    if isinstance(result, ToolResult):
        return result

    return ToolResult(
        tool_name=tool_name,
        success=True,
        result=result,
    )


def add_tool_failure_trace(trace: dict | None, tool_result: ToolResult) -> None:
    """把工具失败写入 trace，方便前端执行轨迹和日志排查。"""

    if not trace or tool_result.success:
        return

    add_trace_event(
        trace,
        event_type="tool_failed",
        data={
            "tool_name": tool_result.tool_name,
            "result": tool_result.result,
            "is_system_failure": is_system_tool_failure(tool_result),
        },
    )


def add_agent_event(trace: dict | None, agent_name: str, action: str, data: dict | None = None) -> None:
    """记录 Orchestrator 分发到哪个专职 Agent。"""

    if not trace:
        return

    add_trace_event(
        trace,
        event_type="agent_step",
        data={
            "agent": agent_name,
            "action": action,
            **(data or {}),
        },
    )


def call_tool(
    trace: dict | None,
    step_name: str,
    tool_name: str,
    arguments: dict,
    fallback_action: str,
) -> ToolResult:
    """执行一个注册工具，并在有 trace 时记录单工具耗时。"""

    callback = lambda: safe_tool_call(
        tool_name,
        lambda: execute_registered_tool(tool_name, arguments),
        fallback_action=fallback_action,
    )

    if trace:
        return timed_step(
            trace,
            step_name,
            callback,
            {"tool_name": tool_name},
        )

    return callback()


def append_plan_validation_result(route: RouteDecision, trace: dict | None) -> ToolResult | None:
    """校验路由计划，返回失败 ToolResult 或 None。"""

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


def should_skip_tools(route: RouteDecision) -> bool:
    """判断当前路由是否不需要执行工具。"""

    return (
        route.blocked_by_guardrail
        or route.need_clarification
        or (route.handoff_required and not route.order_id and not route.need_handoff)
    )


def run_order_lookup(route: RouteDecision, trace: dict | None) -> ToolResult:
    """执行订单查询工具。"""

    return call_tool(
        trace=trace,
        step_name="tool.order_lookup",
        tool_name="order_lookup",
        arguments={"order_id": route.order_id},
        fallback_action="ask_user_to_retry_or_handoff",
    )


def run_policy_search(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
    trace: dict | None,
) -> ToolResult:
    """执行政策检索，并应用证据 guardrail。"""

    rag_query = build_rag_query(user_message, route, tool_results)
    policy_result = call_tool(
        trace=trace,
        step_name="tool.policy_search",
        tool_name="policy_search",
        arguments={"query": rag_query},
        fallback_action="handoff_to_human",
    )
    policy_result = apply_policy_evidence_guardrail(user_message, policy_result)

    if trace:
        report = (
            policy_result.result.get("guardrail_report")
            if isinstance(policy_result.result, dict)
            else policy_result.result[0].get("evidence_guardrail", {})
            if policy_result.result
            else {}
        )
        add_trace_event(
            trace,
            event_type="evidence_guardrail",
            data={
                "passed": policy_result.success,
                "report": report,
            },
        )

    return policy_result


def run_product_search(
    user_message: str,
    route: RouteDecision,
    trace: dict | None,
) -> ToolResult:
    """执行商品搜索工具。"""

    return call_tool(
        trace=trace,
        step_name="tool.get_shop_products",
        tool_name="get_shop_products",
        arguments={
            "query": route.product_query or user_message,
            "limit": 5,
        },
        fallback_action="ask_user_or_handoff_to_human",
    )


def run_goods_link(route: RouteDecision, tool_results: list[ToolResult], trace: dict | None) -> ToolResult:
    """基于商品搜索结果生成商品卡片。"""

    product_result = get_tool_result(tool_results, "get_shop_products")
    product_id = None

    if product_result and product_result.success and product_result.result:
        product_id = product_result.result[0].get("product_id")

    return call_tool(
        trace=trace,
        step_name="tool.send_goods_link",
        tool_name="send_goods_link",
        arguments={
            "product_id": product_id,
        },
        fallback_action="ask_user_or_handoff_to_human",
    )


def run_quick_reply(route: RouteDecision, trace: dict | None) -> ToolResult:
    """执行快捷回复模板查询。"""

    return call_tool(
        trace=trace,
        step_name="tool.get_quick_reply",
        tool_name="get_quick_reply",
        arguments={
            "intent": route.quick_reply_intent,
        },
        fallback_action="fallback_to_generated_reply",
    )


def run_risk_check(user_message: str, route: RouteDecision, trace: dict | None) -> ToolResult:
    """调用风控 Agent。"""

    add_agent_event(
        trace,
        RISK_AGENT.name,
        "risk_check",
        {"order_id": route.order_id},
    )

    return call_tool(
        trace=trace,
        step_name="tool.risk_check",
        tool_name="risk_check",
        arguments={
            "order_id": route.order_id,
            "user_request": user_message,
        },
        fallback_action="handoff_to_human",
    )


def run_refund_apply(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
    trace: dict | None,
) -> ToolResult:
    """创建退款申请，并把任务写入 MQ。"""

    risk_result = get_tool_result(tool_results, "risk_check")
    risk_assessment = risk_result.result if risk_result and risk_result.success else None
    add_agent_event(
        trace,
        AFTER_SALES_AGENT.name,
        "refund_apply",
        {
            "order_id": route.order_id,
            "risk_level": risk_assessment.get("risk_level") if risk_assessment else None,
        },
    )

    return call_tool(
        trace=trace,
        step_name="tool.refund_apply",
        tool_name="refund_apply",
        arguments={
            "order_id": route.order_id,
            "user_request": user_message,
            "risk_assessment": risk_assessment,
        },
        fallback_action="create_manual_review_or_explain_policy",
    )


def run_manual_review(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
    trace: dict | None,
) -> ToolResult:
    """创建人工审核单。"""

    risk_result = get_tool_result(tool_results, "risk_check")
    refund_result = get_tool_result(tool_results, "refund_apply")
    risk_assessment = risk_result.result if risk_result and risk_result.success else {}
    refund_request = refund_result.result if refund_result and refund_result.success else {}
    review_type = "refund" if route.need_refund_request else "risk_control"
    related_id = refund_request.get("refund_id") if isinstance(refund_request, dict) else None
    add_agent_event(
        trace,
        AFTER_SALES_AGENT.name,
        "create_manual_review",
        {"review_type": review_type, "related_id": related_id},
    )

    return call_tool(
        trace=trace,
        step_name="tool.create_manual_review",
        tool_name="create_manual_review",
        arguments={
            "order_id": route.order_id,
            "review_type": review_type,
            "risk_level": risk_assessment.get("risk_level", route.risk_level),
            "risk_flags": risk_assessment.get("risk_flags", route.risk_flags),
            "user_request": user_message,
            "related_id": related_id,
        },
        fallback_action="manual_queue",
    )


def apply_risk_result_to_route(route: RouteDecision, risk_result: ToolResult) -> None:
    """把风控结果写回 route，供回复和后续工具判断。"""

    if not risk_result.success or not isinstance(risk_result.result, dict):
        return

    route.risk_level = risk_result.result.get("risk_level", route.risk_level)
    route.risk_flags = risk_result.result.get("risk_flags", [])
    route.manual_review_required = bool(risk_result.result.get("review_required"))

    if route.manual_review_required and "create_manual_review" not in route.tool_plan:
        route.tool_plan.append("create_manual_review")


def run_handoff(user_message: str, route: RouteDecision, trace: dict | None) -> ToolResult:
    """执行转人工交接工具。"""

    return call_tool(
        trace=trace,
        step_name="tool.transfer_to_human",
        tool_name="transfer_to_human",
        arguments={
            "reason": route.handoff_reason or "用户要求人工客服或该场景需要人工接管。",
            "user_request": user_message,
            "priority": "high" if route.risk_level == "high" else "normal",
        },
        fallback_action="manual_queue",
    )


def run_ticket_creation(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
    trace: dict | None,
) -> list[ToolResult]:
    """执行工单资格判断和工单创建。"""

    order_result = get_order_lookup_result(tool_results)
    order = order_result.result if order_result and order_result.success else None
    issue_type = infer_issue_type(user_message)
    ticket_decision_result = safe_tool_call(
        "ticket_decision",
        lambda: evaluate_ticket_creation(
            route=route,
            order=order,
            issue_type=issue_type,
            user_message=user_message,
        ),
        fallback_action="handoff_to_human",
    )

    if not ticket_decision_result.success:
        add_tool_failure_trace(trace, ticket_decision_result)
        return [ticket_decision_result]

    ticket_decision = ticket_decision_result.result

    if not ticket_decision["can_create"]:
        decision_result = ToolResult(
            tool_name="ticket_decision",
            success=False,
            result=ticket_decision,
        )

        if trace:
            add_trace_event(
                trace,
                event_type="ticket_blocked",
                data=ticket_decision,
            )

        return [decision_result]

    create_ticket_result = call_tool(
        trace=trace,
        step_name="tool.create_ticket",
        tool_name="create_ticket",
        arguments={
            "order_id": route.order_id,
            "issue_type": issue_type,
            "user_request": user_message,
            "priority": ticket_decision["priority"],
        },
        fallback_action="retry_or_handoff_to_human",
    )
    add_tool_failure_trace(trace, create_ticket_result)

    return [create_ticket_result]


def append_chain_validation_result(
    route: RouteDecision,
    tool_results: list[ToolResult],
    trace: dict | None,
) -> None:
    """校验工具执行链路，并把失败结果追加到 tool_results。"""

    chain_valid, chain_errors = validate_tool_chain(route, tool_results)

    if trace:
        add_trace_event(
            trace,
            event_type="tool_chain_validation",
            data={
                "passed": chain_valid,
                "errors": chain_errors,
                "tool_names": [item.tool_name for item in tool_results],
            },
        )

    if chain_valid:
        return

    tool_results.append(
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


def execute_tools(
    user_message: str,
    route: RouteDecision,
    trace: dict | None = None,
) -> list[ToolResult]:
    """由 Orchestrator 协调客服、售后和风控 Agent 依次执行工具。"""

    if should_skip_tools(route):
        return []

    plan_validation_result = append_plan_validation_result(route, trace)

    if plan_validation_result:
        return [plan_validation_result]

    tool_results = []

    if route.need_order and route.order_id:
        add_agent_event(
            trace,
            AFTER_SALES_AGENT.name,
            "order_lookup",
            {"order_id": route.order_id},
        )
        tool_results.append(run_order_lookup(route, trace))

        if has_failed_order_lookup(tool_results):
            add_tool_failure_trace(trace, tool_results[-1])
            if trace:
                add_trace_event(
                    trace,
                    event_type="execution_blocked",
                    data={
                        "reason": "order_lookup_failed",
                        "order_id": route.order_id,
                        "message": "订单不存在，已停止政策检索和工单创建。",
                    },
                )

            return tool_results

    if route.need_policy:
        add_agent_event(
            trace,
            CUSTOMER_AGENT.name,
            "policy_search",
            {"intent": route.intent},
        )
        tool_results.append(run_policy_search(user_message, route, tool_results, trace))

        if has_failed_policy_search(tool_results):
            add_tool_failure_trace(trace, tool_results[-1])
            if trace:
                add_trace_event(
                    trace,
                    event_type="execution_blocked",
                    data={
                        "reason": "policy_search_failed",
                        "message": "政策检索失败，已停止自动工单创建，避免缺少政策依据时误处理。",
                    },
                )

            return tool_results

    if route.need_risk_check:
        risk_result = run_risk_check(user_message, route, trace)
        tool_results.append(risk_result)
        add_tool_failure_trace(trace, risk_result)

        if not risk_result.success:
            if trace:
                add_trace_event(
                    trace,
                    event_type="execution_blocked",
                    data={
                        "reason": "risk_check_failed",
                        "message": "风控检测失败，已停止退款申请等高风险业务动作。",
                    },
                )

            return tool_results

        apply_risk_result_to_route(route, risk_result)

    if route.need_product_search:
        add_agent_event(
            trace,
            CUSTOMER_AGENT.name,
            "get_shop_products",
            {"query": route.product_query or user_message},
        )
        tool_results.append(run_product_search(user_message, route, trace))
        add_tool_failure_trace(trace, tool_results[-1])

    if route.need_goods_link:
        add_agent_event(trace, CUSTOMER_AGENT.name, "send_goods_link")
        tool_results.append(run_goods_link(route, tool_results, trace))
        add_tool_failure_trace(trace, tool_results[-1])

    if route.need_quick_reply:
        add_agent_event(trace, CUSTOMER_AGENT.name, "get_quick_reply")
        tool_results.append(run_quick_reply(route, trace))
        add_tool_failure_trace(trace, tool_results[-1])

    if route.need_refund_request:
        refund_result = run_refund_apply(user_message, route, tool_results, trace)
        tool_results.append(refund_result)
        add_tool_failure_trace(trace, refund_result)

    if route.need_ticket:
        refund_result = get_tool_result(tool_results, "refund_apply")

        if not route.need_refund_request or (refund_result and refund_result.success):
            add_agent_event(
                trace,
                AFTER_SALES_AGENT.name,
                "create_ticket",
                {"issue_type": infer_issue_type(user_message)},
            )
            tool_results.extend(run_ticket_creation(user_message, route, tool_results, trace))

    refund_result = get_tool_result(tool_results, "refund_apply")
    should_create_review = (
        route.manual_review_required
        or (
            refund_result
            and isinstance(refund_result.result, dict)
            and refund_result.result.get("status") == "pending_manual_review"
        )
        or (
            refund_result
            and not refund_result.success
            and isinstance(refund_result.result, dict)
            and refund_result.result.get("review_required")
        )
    )

    if should_create_review and route.order_id:
        if "create_manual_review" not in route.tool_plan:
            route.tool_plan.append("create_manual_review")
        review_result = run_manual_review(user_message, route, tool_results, trace)
        tool_results.append(review_result)
        add_tool_failure_trace(trace, review_result)

    if route.need_handoff:
        tool_results.append(run_handoff(user_message, route, trace))
        add_tool_failure_trace(trace, tool_results[-1])

    append_chain_validation_result(route, tool_results, trace)

    return tool_results
