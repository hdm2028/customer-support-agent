from time import perf_counter
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.routing.conversation_context import apply_conversation_context
from app.agent.policies.fallback_policy import build_fallback_answer
from app.agent.routing.memory import ConversationMemory
from app.agent.routing.pending_task import (
    apply_slot_requirements,
    build_pending_task,
    prepare_pending_task_context,
    should_store_pending_task,
)
from app.agent.response.prompt_builder import build_model_messages
from app.agent.orchestrator import (
    build_agent_plan,
    describe_agent_plan,
    route_user_request,
    run_orchestrated_state,
)
from app.agent.tools.tool_results import has_failed_order_lookup, has_failed_tool_call
from app.core.schemas import RouteDecision, ToolResult
from app.llm.llm_client import call_zhipu_chat
from app.observability.tracing import (
    add_trace_event,
    add_trace_timing,
    finish_trace,
    save_trace,
    set_completion_token_usage,
    set_prompt_token_usage,
    start_trace,
    timed_step,
)
from app.storage.cache import cache_agent_state


memory = ConversationMemory()


def mark_agent_state(
    state: "AgentWorkflowState",
    current_node: str,
    status: str,
    payload: dict | None = None,
) -> None:
    """把当前执行节点写入 Redis/内存缓存。缓存失败不能影响主链路。"""

    try:
        cache_agent_state(
            conversation_id=state["real_conversation_id"],
            current_node=current_node,
            status=status,
            payload=payload,
        )
    except Exception:
        pass


class AgentWorkflowState(TypedDict, total=False):
    """LangGraph 共享状态。"""

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
    orchestration: dict
    tool_results: list[ToolResult]
    model_messages: list[dict]
    reply: str
    reply_mode: str
    result: dict


def dump_model(model):
    """兼容 Pydantic v1/v2，把模型转成 dict。"""

    if hasattr(model, "model_dump"):
        return model.model_dump()

    return model.dict()


def get_conversation_history(conversation_id: str) -> list[dict]:
    """给 API 层使用：根据会话 ID 返回历史消息。"""

    return memory.load(conversation_id)


def should_force_fallback(
    route: RouteDecision,
    tool_results: list[ToolResult] | None = None,
) -> bool:
    """判断当前请求是否必须走确定性回复。"""

    tool_results = tool_results or []

    return (
        route.blocked_by_guardrail
        or route.need_clarification
        or (route.handoff_required and not route.order_id)
        or has_failed_order_lookup(tool_results)
        or has_failed_tool_call(tool_results)
    )


def build_initial_state(
    user_message: str,
    conversation_id: str | None,
    use_llm: bool,
) -> AgentWorkflowState:
    """创建一次 Agent 运行的初始状态。"""

    real_conversation_id = memory.ensure_id(conversation_id)

    return {
        "user_message": user_message,
        "conversation_id": conversation_id,
        "real_conversation_id": real_conversation_id,
        "use_llm": use_llm,
        "trace": start_trace(
            user_message=user_message,
            conversation_id=real_conversation_id,
        ),
    }


def load_context_node(state: AgentWorkflowState) -> dict:
    """加载会话历史和 pending task，并合并用户本轮有效输入。"""

    def work() -> dict:
        mark_agent_state(state, "load_context", "running")
        real_conversation_id = state["real_conversation_id"]
        history = memory.load(real_conversation_id)
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
            history=history,
            used_pending_task=used_pending_task,
        )

        result = {
            "history": history,
            "pending_task": pending_task,
            "effective_user_message": effective_user_message,
            "used_pending_task": used_pending_task,
            "used_conversation_context": used_conversation_context,
            "conversation_context": conversation_context,
            "slots": slots,
            "required_slots": required_slots,
        }
        mark_agent_state(
            state,
            "load_context",
            "done",
            {
                "history_count": len(history),
                "has_pending_task": pending_task is not None,
            },
        )

        return result

    return timed_step(state["trace"], "node.load_context", work)


def route_node(state: AgentWorkflowState) -> dict:
    """执行 Router，并根据槽位要求决定是否追问用户补充信息。"""

    def work() -> dict:
        mark_agent_state(state, "route", "running")
        real_conversation_id = state["real_conversation_id"]
        pending_task = state.get("pending_task")
        effective_user_message = state["effective_user_message"]
        slots = state["slots"]
        required_slots = state["required_slots"]

        route = route_user_request(effective_user_message)
        route, missing_slots = apply_slot_requirements(
            route=route,
            required_slots=required_slots,
            slots=slots,
        )
        route.agent_plan = build_agent_plan(route)
        orchestration = describe_agent_plan(route)

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
        add_trace_event(state["trace"], event_type="agent_plan", data=orchestration)
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
        mark_agent_state(
            state,
            "route",
            "done",
            {
                "intent": route.intent,
                "agent_plan": route.agent_plan,
                "tool_plan": route.tool_plan,
                "missing_slots": missing_slots,
            },
        )

        return {
            "route": route,
            "missing_slots": missing_slots,
            "orchestration": orchestration,
        }

    return timed_step(state["trace"], "node.route", work)


def orchestrate_agents_node(state: AgentWorkflowState) -> dict:
    """进入 Orchestrator 的 Agent Loop，由 Orchestrator 调度各 Agent。"""

    def work() -> dict:
        mark_agent_state(
            state,
            "orchestrate_agents",
            "running",
            {
                "agent_plan": state["route"].agent_plan,
                "tool_plan": state["route"].tool_plan,
            },
        )
        agent_state = run_orchestrated_state(
            user_message=state["effective_user_message"],
            route=state["route"],
            conversation_id=state["real_conversation_id"],
            history=state.get("history", []),
            pending_task=state.get("pending_task"),
            trace=state["trace"],
        )
        tool_results = agent_state.tool_results
        orchestration = {
            **state.get("orchestration", {}),
            "runtime_agent_steps": agent_state.agent_steps,
            "shared_state": agent_state.to_summary(),
        }
        add_trace_event(
            state["trace"],
            event_type="agent_loop_results",
            data={
                "count": len(tool_results),
                "items": [dump_model(item) for item in tool_results],
                "agent_steps": agent_state.agent_steps,
            },
        )
        mark_agent_state(
            state,
            "orchestrate_agents",
            "done",
            {
                "tool_results": [dump_model(item) for item in tool_results],
                "agent_steps": agent_state.agent_steps,
            },
        )

        return {
            "tool_results": tool_results,
            "orchestration": orchestration,
        }

    return timed_step(state["trace"], "node.orchestrate_agents", work)


def build_model_context_node(state: AgentWorkflowState) -> dict:
    """把历史消息、工具结果和当前问题整理成大模型上下文。"""

    def work() -> dict:
        mark_agent_state(state, "build_model_context", "running")
        model_messages = build_model_messages(
            user_message=state["effective_user_message"],
            history=state["history"],
            tool_results=state["tool_results"],
        )
        set_prompt_token_usage(state["trace"], model_messages)
        add_trace_event(
            state["trace"],
            event_type="model_context",
            data={
                "message_count": len(model_messages),
                "context_chars": sum(len(item["content"]) for item in model_messages),
            },
        )
        mark_agent_state(
            state,
            "build_model_context",
            "done",
            {"message_count": len(model_messages)},
        )

        return {"model_messages": model_messages}

    return timed_step(state["trace"], "node.build_model_context", work)


def generate_reply_node(state: AgentWorkflowState) -> dict:
    """根据配置选择真实大模型回复或本地确定性回复。"""

    def work() -> dict:
        mark_agent_state(state, "generate_reply", "running")
        if should_force_fallback(state["route"], state["tool_results"]):
            reply = build_fallback_answer(state["route"], state["tool_results"])
            reply_mode = "rule_fallback"
        elif state["use_llm"]:
            reply = call_zhipu_chat(state["model_messages"])
            reply_mode = "llm"
        else:
            reply = build_fallback_answer(state["route"], state["tool_results"])
            reply_mode = "fallback"

        add_trace_event(
            state["trace"],
            event_type="reply",
            data={
                "reply_mode": reply_mode,
                "reply_chars": len(reply),
            },
        )
        set_completion_token_usage(state["trace"], reply)
        mark_agent_state(
            state,
            "generate_reply",
            "done",
            {
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

    mark_agent_state(state, "persist_result", "running")
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
    mark_agent_state(
        state,
        "persist_result",
        "done",
        {
            "conversation_id": real_conversation_id,
            "duration_ms": finished_trace.get("duration_ms"),
            "token_usage": finished_trace.get("token_usage", {}),
        },
    )

    return {
        "result": {
            "success": True,
            "conversation_id": real_conversation_id,
            "orchestration": state.get("orchestration", {}),
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
            "token_usage": finished_trace.get("token_usage", {}),
            "duration_ms": finished_trace.get("duration_ms"),
        }
    }


def build_agent_workflow():
    """构建 LangGraph 状态图。"""

    graph = StateGraph(AgentWorkflowState)
    graph.add_node("load_context", load_context_node)
    graph.add_node("route", route_node)
    graph.add_node("orchestrate_agents", orchestrate_agents_node)
    graph.add_node("build_model_context", build_model_context_node)
    graph.add_node("generate_reply", generate_reply_node)
    graph.add_node("persist_result", persist_result_node)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "route")
    graph.add_edge("route", "orchestrate_agents")
    graph.add_edge("orchestrate_agents", "build_model_context")
    graph.add_edge("build_model_context", "generate_reply")
    graph.add_edge("generate_reply", "persist_result")
    graph.add_edge("persist_result", END)

    return graph.compile()


agent_workflow = build_agent_workflow()


def run_workflow(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
) -> dict:
    """执行非流式 Agent 工作流。"""

    initial_state = build_initial_state(
        user_message=user_message,
        conversation_id=conversation_id,
        use_llm=use_llm,
    )
    trace = initial_state["trace"]

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
        finished_trace = finish_trace(trace, reply="", success=False)
        save_trace(finished_trace)
        raise
