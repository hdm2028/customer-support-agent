from app.core.schemas import ToolResult
from app.observability.tracing import add_trace_event, timed_step
from app.tools.registry import can_agent_use_tool, execute_registered_tool


SENSITIVE_FIELD_MARKERS = (
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "credential",
)
REDACTED_VALUE = "[REDACTED]"


def sanitize_trace_value(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if any(marker in normalized_key for marker in SENSITIVE_FIELD_MARKERS):
                sanitized[key] = REDACTED_VALUE
            else:
                sanitized[key] = sanitize_trace_value(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_trace_value(item) for item in value]

    if isinstance(value, tuple):
        return [sanitize_trace_value(item) for item in value]

    return value


def add_tool_call_trace(trace: dict | None, tool_name: str, arguments: dict) -> None:
    if not trace:
        return

    add_trace_event(
        trace,
        event_type="tool_call",
        data={
            "trace_id": trace.get("trace_id"),
            "tool_name": tool_name,
            "arguments": sanitize_trace_value(arguments),
        },
    )


def add_tool_result_trace(trace: dict | None, tool_result: ToolResult) -> None:
    if not trace:
        return

    sanitized_result = sanitize_trace_value(tool_result.result)
    event_data = {
        "trace_id": trace.get("trace_id"),
        "tool_name": tool_result.tool_name,
        "success": tool_result.success,
        "status": "success" if tool_result.success else "failure",
        "result": sanitized_result,
    }

    if not tool_result.success:
        if isinstance(sanitized_result, dict):
            error = {
                key: sanitized_result[key]
                for key in (
                    "error_type",
                    "error_message",
                    "errors",
                    "reason",
                    "fallback_action",
                )
                if sanitized_result.get(key) is not None
            }
            event_data["error"] = error or sanitized_result
        else:
            event_data["error"] = str(sanitized_result)

    add_trace_event(trace, event_type="tool_result", data=event_data)


def safe_tool_call(
    tool_name: str,
    callback,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
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
    def callback() -> ToolResult:
        return safe_tool_call(
            tool_name,
            lambda: execute_registered_tool(tool_name, arguments),
            fallback_action=fallback_action,
        )

    add_tool_call_trace(trace, tool_name, arguments)

    if trace:
        result = timed_step(
            trace,
            f"tool.{tool_name}",
            callback,
            {"tool_name": tool_name},
        )
    else:
        result = callback()

    add_tool_result_trace(trace, result)
    return result


def execute_agent_tool(
    agent_key: str,
    tool_name: str,
    arguments: dict,
    trace: dict | None = None,
    fallback_action: str = "handoff_to_human",
) -> ToolResult:
    if not can_agent_use_tool(agent_key, tool_name):
        add_tool_call_trace(trace, tool_name, arguments)
        result = ToolResult(
            tool_name=tool_name,
            success=False,
            result={
                "error_type": "ToolPermissionDenied",
                "error_message": f"{agent_key} 无权调用 {tool_name}。",
                "fallback_action": "reject_tool_call",
            },
        )
        add_tool_result_trace(trace, result)
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
