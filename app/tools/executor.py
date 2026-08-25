from app.core.schemas import ToolResult
from app.observability.tracing import add_trace_event, timed_step
from app.tools.registry import can_agent_use_tool, execute_registered_tool


def safe_tool_call(
    tool_name: str,
    callback,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
    """执行工具并把异常统一转成 ToolResult。"""

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

    return ToolResult(tool_name=tool_name, success=True, result=result)


def execute_tool(
    tool_name: str,
    arguments: dict,
    trace: dict | None = None,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
    """Tool Executor 只做查找、参数校验、异常捕获、执行和 tracing。"""

    def callback() -> ToolResult:
        return safe_tool_call(
            tool_name,
            lambda: execute_registered_tool(tool_name, arguments),
            fallback_action=fallback_action,
        )

    if trace:
        return timed_step(
            trace,
            f"tool.{tool_name}",
            callback,
            {"tool_name": tool_name},
        )

    return callback()


def execute_agent_tool(
    agent_key: str,
    tool_name: str,
    arguments: dict,
    trace: dict | None = None,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
    """按 Agent 权限执行工具，防止越权调用。"""

    if not can_agent_use_tool(agent_key, tool_name):
        result = ToolResult(
            tool_name=tool_name,
            success=False,
            result={
                "error_type": "ToolPermissionDenied",
                "error_message": f"{agent_key} 无权调用 {tool_name}。",
                "fallback_action": "reject_tool_call",
            },
        )
        add_tool_failure_trace(trace, result)
        return result

    return execute_tool(
        tool_name=tool_name,
        arguments=arguments,
        trace=trace,
        fallback_action=fallback_action,
    )


def add_tool_failure_trace(trace: dict | None, tool_result: ToolResult) -> None:
    if not trace or tool_result.success:
        return

    add_trace_event(
        trace,
        event_type="tool_failed",
        data={
            "tool_name": tool_result.tool_name,
            "result": tool_result.result,
        },
    )
