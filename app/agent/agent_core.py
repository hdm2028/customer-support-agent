from collections.abc import AsyncGenerator
from time import perf_counter
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.conversation_context import apply_conversation_context
from app.agent.evidence_guardrail import apply_policy_evidence_guardrail
from app.agent.memory import ConversationMemory
from app.agent.pending_task import (
    apply_slot_requirements,
    build_pending_task,
    prepare_pending_task_context,
    should_store_pending_task,
)
from app.agent.router import infer_issue_type, route_tools
from app.agent.ticket_policy import evaluate_ticket_creation
from app.agent.tool_validation import validate_tool_chain, validate_tool_plan
from app.core.schemas import RouteDecision, ToolResult
from app.llm.llm_client import call_zhipu_chat, call_zhipu_chat_stream
from app.observability.tracing import (
    add_trace_event,
    add_trace_timing,
    finish_trace,
    save_trace,
    start_trace,
)
from app.tools.support_tools import create_ticket, order_lookup, policy_search

memory = ConversationMemory()


class AgentWorkflowState(TypedDict, total=False):
    """LangGraph 共享状态。

    每个节点只负责读写自己关心的字段，避免把所有中间变量都塞在一个长函数里。
    """

    user_message: str
    conversation_id: str | None
    real_conversation_id: str
    use_llm: bool
    trace: dict[str, Any]
    history: list[dict]
    pending_task: dict | None
    effective_user_message: str
    used_pending_task: bool
    used_conversation_context: bool
    conversation_context: dict
    slots: dict
    required_slots: list[str]
    missing_slots: list[str]
    route: RouteDecision
    tool_results: list[ToolResult]
    model_messages: list[dict]
    reply: str
    reply_mode: str
    result: dict


def dump_model(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()

    return model.dict()


def timed_step(trace: dict, step_name: str, callback, data: dict | None = None):
    """执行一个步骤并记录耗时，避免每个节点重复写 perf_counter 代码。"""

    start = perf_counter()

    try:
        result = callback()
    except Exception as error:
        duration_ms = (perf_counter() - start) * 1000
        add_trace_timing(
            trace,
            step_name,
            duration_ms,
            {
                "success": False,
                "error_type": type(error).__name__,
                "error_message": str(error),
                **(data or {}),
            },
        )
        raise

    duration_ms = (perf_counter() - start) * 1000
    add_trace_timing(
        trace,
        step_name,
        duration_ms,
        {
            "success": True,
            **(data or {}),
        },
    )

    return result


def build_timing_event(trace: dict, step_name: str, conversation_id: str) -> dict | None:
    """把 trace 中某个步骤的耗时包装成前端可以展示的 SSE 事件。"""

    timing = trace.get("timings", {}).get(step_name)

    if not timing:
        return None

    return {
        "type": "timing",
        "content": timing,
        "conversation_id": conversation_id,
    }


def get_conversation_history(conversation_id: str) -> list[dict]:
    """给 API 层使用：根据会话 ID 返回历史消息。"""

    return memory.load(conversation_id)


def infer_policy_intent(user_message: str) -> str:
    """把用户问题归纳成更适合 RAG 检索的业务意图词。"""

    if "改收货地址" in user_message or "修改地址" in user_message or "改地址" in user_message:
        return "修改收货地址 地址修改 出库前 仓库确认"

    if "取消" in user_message:
        return "取消订单 待发货 出库前"

    if "退款" in user_message or "退货" in user_message or "不想要" in user_message or "不要了" in user_message:
        return "退货退款 七天无理由 质检 审核"

    if "物流" in user_message or "没更新" in user_message or "不更新" in user_message:
        return "物流查询 物流异常 48 小时 工单"

    if "投诉" in user_message or "没人处理" in user_message:
        return "投诉升级 升级工单 人工客服 记录用户诉求"

    if "保修" in user_message or "维修" in user_message or "检测" in user_message or "坏了" in user_message:
        return "保修范围 保修处理方式 检测工单"

    if "发票" in user_message:
        return "电子发票 发票抬头 税号 邮箱"

    if "缺货" in user_message or "补发" in user_message or "补货" in user_message:
        return "缺货订单处理 补发 继续等待 补货提醒"

    if "会员" in user_message:
        return "会员权益 售后权益限制 质量检测"

    return ""


def build_rag_query(
    user_message: str,
    route: RouteDecision,
    tool_results: list[ToolResult],
) -> str:
    """把用户问题、订单状态和业务意图合成更精准的 RAG 检索 query。"""

    query_parts = [user_message]
    intent_text = infer_policy_intent(user_message)

    if intent_text:
        query_parts.append(f"用户意图：{intent_text}")

    if route.handoff_required:
        query_parts.append("风险边界：高风险操作 需要人工审核 不能直接执行")

    order_result = next((item for item in tool_results if item.tool_name == "order_lookup"), None)

    if order_result and order_result.success:
        order = order_result.result
        is_shipping_query = any(
            keyword in user_message
            for keyword in ["物流", "快递", "发货", "没更新", "不更新", "延迟", "丢件"]
        )
        query_parts.extend(
            [
                f"订单状态：{order.get('order_status')}",
                f"商品名称：{order.get('product_name')}",
                f"商品类目：{order.get('category')}",
            ]
        )

        if is_shipping_query:
            query_parts.append(f"物流状态：{order.get('shipping_status')}")

        if any(keyword in user_message for keyword in ["退款", "退货", "不想要", "不要了", "保修", "维修", "坏了"]):
            query_parts.append(f"签收日期：{order.get('signed_date')}")

    return "\n".join(part for part in query_parts if part)


def get_order_lookup_result(tool_results: list[ToolResult]) -> ToolResult | None:
    """从工具结果中取出订单查询结果，供后续节点判断订单是否真实存在。"""

    return next((item for item in tool_results if item.tool_name == "order_lookup"), None)


def safe_tool_call(
    tool_name: str,
    callback,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
    """安全调用工具，把异常统一转成 ToolResult，避免工具故障打断整个 Agent。

    正常的业务失败仍由工具自己返回 success=False；
    这里主要兜住超时、网络错误、解析错误、数据库错误等系统异常。
    """

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


def has_failed_order_lookup(tool_results: list[ToolResult]) -> bool:
    """判断订单查询是否失败；失败时后续政策检索和工单创建都必须停止。"""

    order_result = get_order_lookup_result(tool_results)

    return bool(order_result and not order_result.success)


def get_tool_result(tool_results: list[ToolResult], tool_name: str) -> ToolResult | None:
    """按工具名获取工具结果，减少后续判断里的重复遍历。"""

    return next((item for item in tool_results if item.tool_name == tool_name), None)


def is_system_tool_failure(tool_result: ToolResult | None) -> bool:
    """判断工具失败是否属于系统异常，而不是正常业务拒绝。"""

    if not tool_result or tool_result.success:
        return False

    return isinstance(tool_result.result, dict) and bool(tool_result.result.get("error_type"))


def is_low_confidence_evidence(tool_result: ToolResult | None) -> bool:
    """判断 RAG 失败是否来自证据低置信或意图不匹配。"""

    if not tool_result or tool_result.success or not isinstance(tool_result.result, dict):
        return False

    return tool_result.result.get("error_type") == "LowConfidenceEvidence"


def has_failed_policy_search(tool_results: list[ToolResult]) -> bool:
    """判断政策检索是否失败；失败时不允许模型编造政策，也不继续自动建单。"""

    policy_result = get_tool_result(tool_results, "policy_search")

    return bool(policy_result and not policy_result.success)


def has_failed_tool_call(tool_results: list[ToolResult]) -> bool:
    """判断是否存在工具调用失败，供回复生成阶段选择确定性兜底。"""

    return any(
        not item.success
        and item.tool_name != "ticket_decision"
        for item in tool_results
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


def execute_tools(
    user_message: str,
    route: RouteDecision,
    trace: dict | None = None,
) -> list[ToolResult]:
    """按照路由结果依次执行订单查询、政策检索、工单创建等工具。"""

    if route.blocked_by_guardrail:
        return []
    if route.need_clarification:
        return []

    if route.handoff_required and not route.order_id:
        return []

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

    if not plan_valid:
        return [
            ToolResult(
                tool_name="tool_plan_validation",
                success=False,
                result={
                    "error_type": "InvalidToolPlan",
                    "error_message": "工具调用计划不合法，已停止执行。",
                    "errors": plan_errors,
                    "fallback_action": "ask_user_or_handoff_to_human",
                },
            )
        ]

    tool_results = []

    if route.need_order and route.order_id:
        if trace:
            tool_results.append(
                timed_step(
                    trace,
                    "tool.order_lookup",
                    lambda: safe_tool_call(
                        "order_lookup",
                        lambda: order_lookup(route.order_id),
                        fallback_action="ask_user_to_retry_or_handoff",
                    ),
                    {"tool_name": "order_lookup"},
                )
            )
        else:
            tool_results.append(
                safe_tool_call(
                    "order_lookup",
                    lambda: order_lookup(route.order_id),
                    fallback_action="ask_user_to_retry_or_handoff",
                )
            )

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
        rag_query = build_rag_query(user_message, route, tool_results)
        if trace:
            tool_results.append(
                timed_step(
                    trace,
                    "tool.policy_search",
                    lambda: safe_tool_call(
                        "policy_search",
                        lambda: policy_search(rag_query),
                        fallback_action="handoff_to_human",
                    ),
                    {"tool_name": "policy_search"},
                )
            )
        else:
            tool_results.append(
                safe_tool_call(
                    "policy_search",
                    lambda: policy_search(rag_query),
                    fallback_action="handoff_to_human",
                )
            )

        if tool_results[-1].tool_name == "policy_search":
            tool_results[-1] = apply_policy_evidence_guardrail(user_message, tool_results[-1])
            if trace:
                report = (
                    tool_results[-1].result.get("guardrail_report")
                    if isinstance(tool_results[-1].result, dict)
                    else tool_results[-1].result[0].get("evidence_guardrail", {})
                    if tool_results[-1].result
                    else {}
                )
                add_trace_event(
                    trace,
                    event_type="evidence_guardrail",
                    data={
                        "passed": tool_results[-1].success,
                        "report": report,
                    },
                )

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

    if route.need_ticket:
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
            tool_results.append(ticket_decision_result)
            add_tool_failure_trace(trace, ticket_decision_result)
            return tool_results

        ticket_decision = ticket_decision_result.result

        if not ticket_decision["can_create"]:
            decision_result = ToolResult(
                tool_name="ticket_decision",
                success=False,
                result=ticket_decision,
            )
            tool_results.append(decision_result)

            if trace:
                add_trace_event(
                    trace,
                    event_type="ticket_blocked",
                    data=ticket_decision,
                )

            return tool_results

        if trace:
            tool_results.append(
                timed_step(
                    trace,
                    "tool.create_ticket",
                    lambda: safe_tool_call(
                        "create_ticket",
                        lambda: create_ticket(
                            order_id=route.order_id,
                            issue_type=issue_type,
                            user_request=user_message,
                            priority=ticket_decision["priority"],
                        ),
                        fallback_action="retry_or_handoff_to_human",
                    ),
                    {"tool_name": "create_ticket"},
                )
            )
        else:
            tool_results.append(
                safe_tool_call(
                    "create_ticket",
                    lambda: create_ticket(
                        order_id=route.order_id,
                        issue_type=issue_type,
                        user_request=user_message,
                        priority=ticket_decision["priority"],
                    ),
                    fallback_action="retry_or_handoff_to_human",
                )
            )

        add_tool_failure_trace(trace, tool_results[-1])

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

    if not chain_valid:
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

    return tool_results


def build_order_context(tool_results: list[ToolResult]) -> str:
    """把订单查询结果整理成大模型容易理解的业务上下文。"""

    order_result = next((item for item in tool_results if item.tool_name == "order_lookup"), None)

    if not order_result:
        return ""

    if not order_result.success:
        return f"[订单信息]\n查询失败：{order_result.result}"

    order = order_result.result

    return "\n".join(
        [
            "[订单信息]",
            f"订单号：{order.get('order_id')}",
            f"商品名称：{order.get('product_name')}",
            f"订单状态：{order.get('order_status')}",
            f"物流状态：{order.get('shipping_status')}",
            f"签收日期：{order.get('signed_date')}",
            f"保修月数：{order.get('warranty_months')}",
            f"七天无理由时限：{order.get('return_window_days')} 天",
            f"是否可直接退款：{order.get('can_refund_directly')}",
            f"备注：{order.get('notes')}",
        ]
    )


def build_policy_evidence(tool_results: list[ToolResult]) -> str:
    """把 RAG 检索结果整理成 evidence context，减少模型读错或漏读证据。"""

    policy_result = next((item for item in tool_results if item.tool_name == "policy_search"), None)

    if not policy_result:
        return ""

    if not policy_result.success:
        return f"[售后政策证据]\n检索失败：{policy_result.result}"

    lines = ["[售后政策证据]"]

    for index, item in enumerate(policy_result.result, start=1):
        citation = item.get("citation") or item.get("source") or "未知来源"
        score = item.get("score", "未知")
        text = item.get("text", "").strip()

        lines.extend(
            [
                f"证据 {index}",
                f"来源：{citation}",
                f"相关分数：{score}",
                "内容：",
                text,
                "",
            ]
        )

    return "\n".join(lines).strip()


def build_ticket_context(tool_results: list[ToolResult]) -> str:
    """把工单工具结果整理成客服后续动作说明。"""

    ticket_result = next((item for item in tool_results if item.tool_name == "create_ticket"), None)

    if not ticket_result:
        return ""

    if not ticket_result.success:
        return f"[工单信息]\n创建失败：{ticket_result.result}"

    ticket = ticket_result.result

    return "\n".join(
        [
            "[工单信息]",
            f"工单状态：{ticket.get('status')}",
            f"风险提示：{ticket.get('risk_notice')}",
            f"关联订单：{ticket.get('order_id')}",
            f"问题类型：{ticket.get('issue_type')}",
            f"优先级：{ticket.get('priority')}",
            f"用户诉求：{ticket.get('user_request')}",
            f"下一步：{ticket.get('next_step')}",
        ]
    )


def build_tool_context(tool_results: list[ToolResult]) -> str:
    """统一组装工具上下文，避免把原始 JSON 直接塞给大模型。"""

    context_parts = [
        build_order_context(tool_results),
        build_policy_evidence(tool_results),
        build_ticket_context(tool_results),
    ]
    context_parts = [part for part in context_parts if part]

    if not context_parts:
        return "本轮没有调用工具。"

    return "\n\n==========\n\n".join(context_parts)


def build_model_messages(
    user_message: str,
    history: list[dict],
    tool_results: list[ToolResult],
) -> list[dict]:
    """把历史消息和工具结果整理成大模型 messages。"""

    tool_context = build_tool_context(tool_results)

    system_prompt = (
        "你是中文电商平台的智能售后客服 Agent。"
        "你必须根据订单信息、售后政策、工单结果回答用户。"
        "不要编造工具结果里不存在的信息。"
        "涉及退款、赔付、取消订单、修改地址等高风险操作时，只能解释规则或创建工单，不能承诺已经完成。"
        "如果信息不足，要明确告诉用户还需要补充什么。"
    )

    user_prompt = (
        f"用户当前问题：\n{user_message}\n\n"
        f"工具执行结果：\n{tool_context}\n\n"
        "请生成客服回复，要求：\n"
        "1. 先直接回答用户最关心的问题。\n"
        "2. 说明依据了哪些订单信息或政策。\n"
        "3. 如果使用了售后政策证据，必须引用对应来源。\n"
        "4. 如果生成了工单，告诉用户后续需要人工审核。\n"
        "5. 如果证据没有覆盖用户问题，要明确说明资料不足，不能编造。\n"
        "6. 语气礼貌、清楚、不要夸大承诺。"
    )

    return [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_prompt},
    ]


def fallback_answer(route: RouteDecision, tool_results: list[ToolResult]) -> str:
    """不调用大模型时的兜底回复，保证 demo 离线也能稳定运行。"""
    if route.need_clarification:
        reply = route.clarification_question or "请您补充订单号后，我再帮您继续处理。"

        if route.handoff_required and route.handoff_reason:
            reply += route.handoff_reason

        return reply

    if route.handoff_required and not route.order_id:
        return route.handoff_reason or "该问题需要人工客服进一步处理。"
    if route.blocked_by_guardrail:
        return route.guardrail_reason or "当前请求存在安全风险，已拒绝执行。"

    order_result = get_order_lookup_result(tool_results)

    if order_result and not order_result.success:
        return (
            f"{order_result.result} 请您核对订单号后重新提供，"
            "我再继续查询售后政策并判断是否需要创建工单。"
        )

    policy_result = next((item for item in tool_results if item.tool_name == "policy_search"), None)
    ticket_decision_result = next((item for item in tool_results if item.tool_name == "ticket_decision"), None)
    ticket_result = next((item for item in tool_results if item.tool_name == "create_ticket"), None)
    plan_validation_result = next((item for item in tool_results if item.tool_name == "tool_plan_validation"), None)
    chain_validation_result = next((item for item in tool_results if item.tool_name == "tool_chain_validation"), None)

    parts = []

    if plan_validation_result and not plan_validation_result.success:
        return (
            "本轮工具调用计划没有通过校验，我不会继续执行可能错误的自动操作。"
            "请您补充订单号和具体售后诉求，或由人工客服继续处理。"
        )

    if order_result and order_result.success:
        order = order_result.result
        parts.append(
            f"已查询到订单 {order.get('order_id')}，商品是 {order.get('product_name')}，"
            f"当前订单状态为{order.get('order_status')}。"
        )

    if policy_result and not policy_result.success:
        if is_low_confidence_evidence(policy_result):
            parts.append(
                "但本轮没有检索到足够匹配的售后政策证据，我不能强行判断或创建工单。"
                "建议补充问题细节，或转人工客服核对政策后继续处理。"
            )
        elif is_system_tool_failure(policy_result):
            parts.append(
                "但本轮售后政策检索工具调用失败，我不能在缺少政策依据时直接判断或创建工单。"
                "建议转人工客服核对政策后继续处理。"
            )
        else:
            parts.append(
                "但本轮没有检索到足够匹配的售后政策，我不能编造不存在的政策结论。"
                "建议补充问题细节或转人工客服确认。"
            )

    if policy_result and policy_result.success:
        first_policy = policy_result.result[0]
        citation = first_policy.get("citation") or first_policy.get("source")
        parts.append(f"根据知识库来源《{citation}》，本问题需要结合售后政策进一步判断。")

    if ticket_result and not ticket_result.success:
        if is_system_tool_failure(ticket_result):
            parts.append(
                "工单创建工具本轮调用失败，暂时没有生成工单。"
                "建议稍后重试，或由人工客服继续接入处理。"
            )
        else:
            parts.append(f"工单暂未创建成功：{ticket_result.result}")

    if ticket_result and ticket_result.success:
        ticket = ticket_result.result
        parts.append(
            f"我已生成{ticket['issue_type']}工单草稿，后续需要人工客服核对订单和凭证后处理。"
        )

    if chain_validation_result and not chain_validation_result.success:
        parts.append(
            "另外，本轮工具执行链路没有通过一致性校验，我不会继续扩大自动处理范围。"
            "建议转人工客服复核。"
        )

    if ticket_decision_result and not ticket_decision_result.success:
        reason = ticket_decision_result.result.get("reason", "当前订单状态暂不满足创建工单条件。")
        parts.append(f"根据订单状态，当前暂不创建工单：{reason}")

    if not parts:
        return "您好，我暂时没有找到足够信息。请补充订单号和具体售后问题，我再帮您判断。"

    return "".join(parts)


def should_force_fallback(
    route: RouteDecision,
    tool_results: list[ToolResult] | None = None,
) -> bool:
    """判断当前请求是否必须走确定性兜底回复，而不是交给大模型自由生成。"""

    tool_results = tool_results or []

    return (
        route.blocked_by_guardrail
        or route.need_clarification
        or (route.handoff_required and not route.order_id)
        or has_failed_order_lookup(tool_results)
        or has_failed_tool_call(tool_results)
    )


def load_context_node(state: AgentWorkflowState) -> dict:
    """加载会话历史和 pending task，并把用户本轮输入合并成有效任务输入。"""

    def work() -> dict:
        real_conversation_id = state["real_conversation_id"]
        pending_task = memory.get_pending_task(real_conversation_id)
        (
            effective_user_message,
            used_pending_task,
            slots,
            required_slots,
        ) = prepare_pending_task_context(
            user_message=state["user_message"],
            pending_task=pending_task,
        )
        (
            effective_user_message,
            used_conversation_context,
            conversation_context,
        ) = apply_conversation_context(
            user_message=effective_user_message,
            history=memory.load(real_conversation_id),
            used_pending_task=used_pending_task,
        )

        return {
            "history": memory.load(real_conversation_id),
            "pending_task": pending_task,
            "effective_user_message": effective_user_message,
            "used_pending_task": used_pending_task,
            "used_conversation_context": used_conversation_context,
            "conversation_context": conversation_context,
            "slots": slots,
            "required_slots": required_slots,
        }

    return timed_step(state["trace"], "node.load_context", work)


def route_node(state: AgentWorkflowState) -> dict:
    """执行 Router，并根据槽位要求决定是否追问用户补充信息。"""

    def work() -> dict:
        real_conversation_id = state["real_conversation_id"]
        pending_task = state.get("pending_task")
        effective_user_message = state["effective_user_message"]
        slots = state["slots"]
        required_slots = state["required_slots"]

        route = route_tools(effective_user_message)
        route, missing_slots = apply_slot_requirements(
            route=route,
            required_slots=required_slots,
            slots=slots,
        )

        if state["used_pending_task"] and not missing_slots:
            memory.clear_pending_task(real_conversation_id)

        if should_store_pending_task(route, missing_slots):
            pending_user_request = (
                pending_task.get("user_request")
                if pending_task
                else effective_user_message
            )
            memory.set_pending_task(
                real_conversation_id,
                build_pending_task(
                    user_message=pending_user_request,
                    route=route,
                    slots=slots,
                    required_slots=required_slots,
                    missing_slots=missing_slots,
                ),
            )

        add_trace_event(state["trace"], event_type="route", data=dump_model(route))
        add_trace_event(
            state["trace"],
            event_type="pending_task",
            data={
                "used_pending_task": state["used_pending_task"],
                "used_conversation_context": state.get("used_conversation_context", False),
                "conversation_context": state.get("conversation_context", {}),
                "has_pending_task": memory.get_pending_task(real_conversation_id) is not None,
                "effective_user_message": effective_user_message,
                "slots": slots,
                "required_slots": required_slots,
                "missing_slots": missing_slots,
            },
        )

        return {
            "route": route,
            "missing_slots": missing_slots,
        }

    return timed_step(state["trace"], "node.route", work)


def execute_tools_node(state: AgentWorkflowState) -> dict:
    """根据 route 调用订单查询、RAG 检索和工单创建工具。"""

    def work() -> dict:
        tool_results = execute_tools(
            user_message=state["effective_user_message"],
            route=state["route"],
            trace=state["trace"],
        )
        add_trace_event(
            state["trace"],
            event_type="tool_results",
            data={
                "count": len(tool_results),
                "items": [dump_model(item) for item in tool_results],
            },
        )

        return {"tool_results": tool_results}

    return timed_step(state["trace"], "node.execute_tools", work)


def build_model_context_node(state: AgentWorkflowState) -> dict:
    """把历史消息、工具结果和当前问题整理成大模型上下文。"""

    def work() -> dict:
        model_messages = build_model_messages(
            user_message=state["effective_user_message"],
            history=state["history"],
            tool_results=state["tool_results"],
        )
        add_trace_event(
            state["trace"],
            event_type="model_context",
            data={
                "message_count": len(model_messages),
                "context_chars": sum(len(item["content"]) for item in model_messages),
            },
        )

        return {"model_messages": model_messages}

    return timed_step(state["trace"], "node.build_model_context", work)


def generate_reply_node(state: AgentWorkflowState) -> dict:
    """根据配置选择真实大模型回复或本地兜底回复。"""

    def work() -> dict:
        if should_force_fallback(state["route"], state["tool_results"]):
            reply = fallback_answer(state["route"], state["tool_results"])
            reply_mode = "rule_fallback"
        elif state["use_llm"]:
            reply = call_zhipu_chat(state["model_messages"])
            reply_mode = "llm"
        else:
            reply = fallback_answer(state["route"], state["tool_results"])
            reply_mode = "fallback"

        add_trace_event(
            state["trace"],
            event_type="reply",
            data={
                "reply_mode": reply_mode,
                "reply_chars": len(reply),
            },
        )

        return {
            "reply": reply,
            "reply_mode": reply_mode,
        }

    return timed_step(state["trace"], "node.generate_reply", work)


def persist_result_node(state: AgentWorkflowState) -> dict:
    """保存会话和 trace，并组装 API 层需要返回的最终结果。"""

    start = perf_counter()
    real_conversation_id = state["real_conversation_id"]
    reply = state["reply"]

    try:
        memory.append(real_conversation_id, "user", state["user_message"])
        memory.append(real_conversation_id, "assistant", reply)
    except Exception as error:
        add_trace_timing(
            state["trace"],
            "node.persist_result",
            (perf_counter() - start) * 1000,
            {
                "success": False,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        raise

    add_trace_timing(
        state["trace"],
        "node.persist_result",
        (perf_counter() - start) * 1000,
        {"success": True},
    )
    finished_trace = finish_trace(state["trace"], reply, success=True)
    save_trace(finished_trace)

    return {
        "result": {
            "success": True,
            "conversation_id": real_conversation_id,
            "route": dump_model(state["route"]),
            "tool_results": [dump_model(item) for item in state["tool_results"]],
            "reply": reply,
            "model_messages": state["model_messages"],
            "used_pending_task": state["used_pending_task"],
            "used_conversation_context": state.get("used_conversation_context", False),
            "conversation_context": state.get("conversation_context", {}),
            "effective_user_message": state["effective_user_message"],
            "slots": state["slots"],
            "missing_slots": state["missing_slots"],
            "workflow_engine": "langgraph",
            "timings": finished_trace.get("timings", {}),
            "duration_ms": finished_trace.get("duration_ms"),
        }
    }


def build_agent_workflow():
    """构建 LangGraph 状态图。

    这里先使用线性工作流，后续可以在 route 之后加入条件边，
    例如无须工具时跳过 execute_tools，或高风险任务直接转人工队列。
    """

    graph = StateGraph(AgentWorkflowState)
    graph.add_node("load_context", load_context_node)
    graph.add_node("route", route_node)
    graph.add_node("execute_tools", execute_tools_node)
    graph.add_node("build_model_context", build_model_context_node)
    graph.add_node("generate_reply", generate_reply_node)
    graph.add_node("persist_result", persist_result_node)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "route")
    graph.add_edge("route", "execute_tools")
    graph.add_edge("execute_tools", "build_model_context")
    graph.add_edge("build_model_context", "generate_reply")
    graph.add_edge("generate_reply", "persist_result")
    graph.add_edge("persist_result", END)

    return graph.compile()


agent_workflow = build_agent_workflow()


def run_customer_support_agent(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
) -> dict:
    """Agent 主入口：用 LangGraph 共享状态编排完整客服处理链路。"""

    real_conversation_id = memory.ensure_id(conversation_id)
    trace = start_trace(user_message=user_message, conversation_id=real_conversation_id)
    initial_state: AgentWorkflowState = {
        "user_message": user_message,
        "conversation_id": conversation_id,
        "real_conversation_id": real_conversation_id,
        "use_llm": use_llm,
        "trace": trace,
    }

    try:
        final_state = agent_workflow.invoke(initial_state)
        return final_state["result"]
    except Exception as error:
        add_trace_event(
            trace,
            event_type="error",
            data={
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )

        finished_trace = finish_trace(
            trace,
            reply="",
            success=False,
        )
        save_trace(finished_trace)

        raise


async def stream_customer_support_agent(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
    stream_tokens: bool = True,
) -> AsyncGenerator[dict, None]:
    """流式执行 Agent，并在真实模型模式下转发 LLM 原生 token。

    普通 API 使用完整 LangGraph invoke；网页流式接口为了拿到模型 token，
    复用同一组节点函数逐步推进 state，然后在生成阶段调用智谱 stream。
    """

    real_conversation_id = memory.ensure_id(conversation_id)
    trace = start_trace(user_message=user_message, conversation_id=real_conversation_id)
    initial_state: AgentWorkflowState = {
        "user_message": user_message,
        "conversation_id": conversation_id,
        "real_conversation_id": real_conversation_id,
        "use_llm": use_llm,
        "trace": trace,
    }

    try:
        state = dict(initial_state)

        state.update(load_context_node(state))
        timing_event = build_timing_event(state["trace"], "node.load_context", real_conversation_id)
        if timing_event:
            yield timing_event

        state.update(route_node(state))
        yield {
            "type": "route",
            "content": dump_model(state["route"]),
            "conversation_id": real_conversation_id,
        }
        timing_event = build_timing_event(state["trace"], "node.route", real_conversation_id)
        if timing_event:
            yield timing_event

        state.update(execute_tools_node(state))
        for tool_result in state["tool_results"]:
            yield {
                "type": "tool_result",
                "content": dump_model(tool_result),
                "conversation_id": real_conversation_id,
            }
            timing_event = build_timing_event(
                state["trace"],
                f"tool.{tool_result.tool_name}",
                real_conversation_id,
            )
            if timing_event:
                yield timing_event

        for trace_event in state["trace"].get("events", []):
            if trace_event.get("event_type") != "execution_blocked":
                continue

            yield {
                "type": "execution_blocked",
                "content": trace_event.get("message", {}),
                "conversation_id": real_conversation_id,
            }

        timing_event = build_timing_event(state["trace"], "node.execute_tools", real_conversation_id)
        if timing_event:
            yield timing_event

        state.update(build_model_context_node(state))
        timing_event = build_timing_event(state["trace"], "node.build_model_context", real_conversation_id)
        if timing_event:
            yield timing_event

        if state["use_llm"] and stream_tokens and not should_force_fallback(
            state["route"],
            state["tool_results"],
        ):
            reply_parts = []
            llm_start = perf_counter()

            yield {
                "type": "status",
                "content": "正在调用智谱大模型生成客服回复...",
                "conversation_id": real_conversation_id,
            }

            for token in call_zhipu_chat_stream(state["model_messages"]):
                reply_parts.append(token)
                yield {
                    "type": "token",
                    "content": token,
                    "conversation_id": real_conversation_id,
                }

            reply = "".join(reply_parts)
            state.update(
                {
                    "reply": reply,
                    "reply_mode": "llm_stream",
                }
            )
            add_trace_event(
                state["trace"],
                event_type="reply",
                data={
                    "reply_mode": "llm_stream",
                    "reply_chars": len(reply),
                },
            )
            add_trace_timing(
                state["trace"],
                "node.generate_reply",
                (perf_counter() - llm_start) * 1000,
                {
                    "success": True,
                    "reply_mode": "llm_stream",
                    "reply_chars": len(reply),
                },
            )
            timing_event = build_timing_event(state["trace"], "node.generate_reply", real_conversation_id)
            if timing_event:
                yield timing_event
        else:
            state.update(generate_reply_node(state))
            yield {
                "type": "message",
                "content": state["reply"],
                "conversation_id": real_conversation_id,
            }
            timing_event = build_timing_event(state["trace"], "node.generate_reply", real_conversation_id)
            if timing_event:
                yield timing_event

        state.update(persist_result_node(state))
        final_result = state["result"]
        timing_event = build_timing_event(state["trace"], "node.persist_result", real_conversation_id)
        if timing_event:
            yield timing_event

        yield {
            "type": "done",
            "content": final_result,
            "conversation_id": real_conversation_id,
        }
        yield {
            "type": "workflow",
            "content": "langgraph_nodes_with_llm_stream",
            "conversation_id": real_conversation_id,
        }
    except Exception as error:
        add_trace_event(
            trace,
            event_type="error",
            data={
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        finished_trace = finish_trace(trace, reply="", success=False)
        save_trace(finished_trace)
        yield {
            "type": "error",
            "content": f"{type(error).__name__}: {error}",
            "conversation_id": real_conversation_id,
        }
