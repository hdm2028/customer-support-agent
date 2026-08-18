from collections.abc import AsyncGenerator
from time import perf_counter

from app.agent.entry.workflow import (
    build_initial_state,
    build_model_context_node,
    dump_model,
    execute_tools_node,
    generate_reply_node,
    load_context_node,
    persist_result_node,
    route_node,
    should_force_fallback,
)
from app.llm.llm_client import call_zhipu_chat_stream
from app.observability.tracing import (
    add_trace_event,
    add_trace_timing,
    build_timing_event,
    finish_trace,
    save_trace,
    set_completion_token_usage,
)


async def stream_workflow(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
    stream_tokens: bool = True,
) -> AsyncGenerator[dict, None]:
    """流式执行 Agent，并在真实模型模式下转发 LLM 原生 token。"""

    state = build_initial_state(
        user_message=user_message,
        conversation_id=conversation_id,
        use_llm=use_llm,
    )
    trace = state["trace"]
    real_conversation_id = state["real_conversation_id"]

    try:
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

        if use_llm and stream_tokens and not should_force_fallback(
            state["route"],
            state["tool_results"],
        ):
            async for event in stream_llm_reply(state, real_conversation_id):
                yield event
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


async def stream_llm_reply(state: dict, conversation_id: str) -> AsyncGenerator[dict, None]:
    """调用 LLM 流式回复，并把结果写回 state。"""

    reply_parts = []
    llm_start = perf_counter()

    yield {
        "type": "status",
        "content": "正在调用智谱大模型生成客服回复...",
        "conversation_id": conversation_id,
    }

    for token in call_zhipu_chat_stream(state["model_messages"]):
        reply_parts.append(token)
        yield {
            "type": "token",
            "content": token,
            "conversation_id": conversation_id,
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
    set_completion_token_usage(state["trace"], reply)
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
    timing_event = build_timing_event(state["trace"], "node.generate_reply", conversation_id)
    if timing_event:
        yield timing_event
