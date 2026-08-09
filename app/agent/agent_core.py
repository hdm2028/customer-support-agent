from collections.abc import AsyncGenerator
from time import perf_counter
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.memory import ConversationMemory
from app.agent.pending_task import (
    apply_slot_requirements,
    build_pending_task,
    prepare_pending_task_context,
    should_store_pending_task,
)
from app.agent.router import infer_issue_type, route_tools
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

    if "退款" in user_message or "退货" in user_message:
        return "退货退款 七天无理由 质检 审核"

    if "物流" in user_message or "没更新" in user_message or "不更新" in user_message:
        return "物流查询 物流异常 48 小时 工单"

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
        query_parts.extend(
            [
                f"订单状态：{order.get('order_status')}",
                f"物流状态：{order.get('shipping_status')}",
                f"商品名称：{order.get('product_name')}",
                f"订单备注：{order.get('notes')}",
            ]
        )

    return "\n".join(part for part in query_parts if part)


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

    tool_results = []

    if route.need_order and route.order_id:
        if trace:
            tool_results.append(
                timed_step(
                    trace,
                    "tool.order_lookup",
                    lambda: order_lookup(route.order_id),
                    {"tool_name": "order_lookup"},
                )
            )
        else:
            tool_results.append(order_lookup(route.order_id))

    if route.need_policy:
        rag_query = build_rag_query(user_message, route, tool_results)
        if trace:
            tool_results.append(
                timed_step(
                    trace,
                    "tool.policy_search",
                    lambda: policy_search(rag_query),
                    {"tool_name": "policy_search"},
                )
            )
        else:
            tool_results.append(policy_search(rag_query))

    if route.need_ticket:
        if trace:
            tool_results.append(
                timed_step(
                    trace,
                    "tool.create_ticket",
                    lambda: create_ticket(
                        order_id=route.order_id,
                        issue_type=infer_issue_type(user_message),
                        user_request=user_message,
                        priority="normal",
                    ),
                    {"tool_name": "create_ticket"},
                )
            )
        else:
            tool_results.append(
                create_ticket(
                    order_id=route.order_id,
                    issue_type=infer_issue_type(user_message),
                    user_request=user_message,
                    priority="normal",
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

    order_result = next((item for item in tool_results if item.tool_name == "order_lookup"), None)
    policy_result = next((item for item in tool_results if item.tool_name == "policy_search"), None)
    ticket_result = next((item for item in tool_results if item.tool_name == "create_ticket"), None)

    parts = []

    if order_result and order_result.success:
        order = order_result.result
        parts.append(
            f"已查询到订单 {order.get('order_id')}，商品是 {order.get('product_name')}，"
            f"当前订单状态为{order.get('order_status')}。"
        )

    if policy_result and policy_result.success:
        first_policy = policy_result.result[0]
        citation = first_policy.get("citation") or first_policy.get("source")
        parts.append(f"根据知识库来源《{citation}》，本问题需要结合售后政策进一步判断。")

    if ticket_result and ticket_result.success:
        ticket = ticket_result.result
        parts.append(
            f"我已生成{ticket['issue_type']}工单草稿，后续需要人工客服核对订单和凭证后处理。"
        )

    if not parts:
        return "您好，我暂时没有找到足够信息。请补充订单号和具体售后问题，我再帮您判断。"

    return "".join(parts)


def should_force_fallback(route: RouteDecision) -> bool:
    """判断当前请求是否必须走确定性兜底回复，而不是交给大模型自由生成。"""

    return (
        route.blocked_by_guardrail
        or route.need_clarification
        or (route.handoff_required and not route.order_id)
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

        return {
            "history": memory.load(real_conversation_id),
            "pending_task": pending_task,
            "effective_user_message": effective_user_message,
            "used_pending_task": used_pending_task,
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
        if should_force_fallback(state["route"]):
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

        timing_event = build_timing_event(state["trace"], "node.execute_tools", real_conversation_id)
        if timing_event:
            yield timing_event

        state.update(build_model_context_node(state))
        timing_event = build_timing_event(state["trace"], "node.build_model_context", real_conversation_id)
        if timing_event:
            yield timing_event

        if state["use_llm"] and stream_tokens and not should_force_fallback(state["route"]):
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
