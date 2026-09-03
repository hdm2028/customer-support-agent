import sqlite3
import threading
import time
from dataclasses import replace
from queue import Empty, Queue
from time import perf_counter
from typing import Any, Callable

from app.core.config import get_settings
from app.core.schemas import ToolResult
from app.observability.tracing import add_trace_event, timed_step
from app.tools.registry import (
    READ_ONLY,
    ToolRuntimePolicy,
    can_agent_use_tool,
    execute_registered_tool,
    get_tool_runtime_policy,
)


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


class ToolRuntimeError(Exception):
    """Base type for failures classified by the tool executor."""


class InvalidToolArguments(ToolRuntimeError):
    pass


class ToolPermissionDenied(ToolRuntimeError):
    pass


class ToolTimeout(ToolRuntimeError):
    pass


class ToolTransientError(ToolRuntimeError):
    pass


class ToolExecutionError(ToolRuntimeError):
    pass


_DEFAULT_RUNTIME_POLICY = ToolRuntimePolicy(
    timeout_seconds=5.0,
    max_attempts=1,
    retry_on=(),
    side_effect_class=READ_ONLY,
    idempotent=True,
    fallback_action="handoff_to_human",
)
_WORKER_SLOTS = None
_WORKER_SLOTS_LOCK = threading.Lock()


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


def _get_worker_slots() -> threading.BoundedSemaphore:
    global _WORKER_SLOTS

    if _WORKER_SLOTS is None:
        with _WORKER_SLOTS_LOCK:
            if _WORKER_SLOTS is None:
                _WORKER_SLOTS = threading.BoundedSemaphore(
                    get_settings().tool_timeout_worker_limit
                )

    return _WORKER_SLOTS


def _resolve_runtime_policy(
    tool_name: str,
    runtime_policy: ToolRuntimePolicy | None = None,
) -> ToolRuntimePolicy:
    if runtime_policy is not None:
        return runtime_policy

    policy = get_tool_runtime_policy(tool_name) or _DEFAULT_RUNTIME_POLICY
    settings = get_settings()
    timeout_seconds = settings.tool_timeout_overrides.get(
        tool_name,
        policy.timeout_seconds,
    )
    return replace(
        policy,
        timeout_seconds=timeout_seconds,
        backoff_seconds=settings.tool_retry_backoff_seconds,
    )


def _is_transient_dependency_error(error: Exception) -> bool:
    if isinstance(error, ConnectionError):
        return True

    error_type = type(error)
    module_name = error_type.__module__
    error_name = error_type.__name__

    if module_name.startswith("redis") and error_name in {
        "ConnectionError",
        "TimeoutError",
        "BusyLoadingError",
    }:
        return True

    if module_name.startswith("pymysql") and error_name in {
        "OperationalError",
        "InterfaceError",
    }:
        error_code = error.args[0] if error.args else None
        return error_code in {1205, 1213, 2002, 2003, 2006, 2013}

    return (
        isinstance(error, sqlite3.OperationalError)
        and any(marker in str(error).lower() for marker in ("locked", "busy"))
    )


def classify_tool_error(error: Exception) -> tuple[str, bool]:
    if isinstance(error, InvalidToolArguments):
        return "InvalidToolArguments", False
    if isinstance(error, ToolPermissionDenied):
        return "ToolPermissionDenied", False
    if isinstance(error, (ToolTimeout, TimeoutError)):
        return "ToolTimeout", True
    if isinstance(error, ToolTransientError) or _is_transient_dependency_error(error):
        return "ToolTransientError", True
    return "ToolExecutionError", False


def add_tool_call_trace(
    trace: dict | None,
    tool_name: str,
    arguments: dict,
    *,
    attempt: int = 1,
    policy: ToolRuntimePolicy | None = None,
) -> None:
    if not trace:
        return

    data = {
        "trace_id": trace.get("trace_id"),
        "tool_name": tool_name,
        "arguments": sanitize_trace_value(arguments),
        "attempt": attempt,
    }
    if policy is not None:
        data.update(
            {
                "max_attempts": policy.max_attempts,
                "timeout_seconds": policy.timeout_seconds,
                "side_effect_class": policy.side_effect_class,
            }
        )

    add_trace_event(trace, event_type="tool_call", data=data)


def _trace_status(tool_result: ToolResult) -> str:
    if tool_result.success:
        return "success"
    if (
        isinstance(tool_result.result, dict)
        and tool_result.result.get("error_type") == "ToolTimeout"
    ):
        return "timeout"
    return "failure"


def add_tool_result_trace(
    trace: dict | None,
    tool_result: ToolResult,
    runtime_metadata: dict[str, Any] | None = None,
) -> None:
    if not trace:
        return

    sanitized_result = sanitize_trace_value(tool_result.result)
    event_data = {
        "trace_id": trace.get("trace_id"),
        "tool_name": tool_result.tool_name,
        "success": tool_result.success,
        "status": _trace_status(tool_result),
        "result": sanitized_result,
        **sanitize_trace_value(runtime_metadata or {}),
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
                    "retryable",
                )
                if sanitized_result.get(key) is not None
            }
            event_data["error"] = error or sanitized_result
        else:
            event_data["error"] = str(sanitized_result)

    add_trace_event(trace, event_type="tool_result", data=event_data)


def _add_tool_retry_trace(
    trace: dict | None,
    *,
    tool_name: str,
    attempt: int,
    policy: ToolRuntimePolicy,
    error_type: str,
    error: Exception,
) -> None:
    if not trace:
        return

    add_trace_event(
        trace,
        event_type="tool_retry",
        data={
            "tool_name": tool_name,
            "attempt": attempt,
            "next_attempt": attempt + 1,
            "max_attempts": policy.max_attempts,
            "error_type": error_type,
            "error_message": str(error),
            "retryable": True,
            "backoff_seconds": policy.backoff_seconds,
            "side_effect_class": policy.side_effect_class,
        },
    )


def _run_with_timeout(callback: Callable[[], Any], timeout_seconds: float) -> Any:
    if timeout_seconds <= 0:
        raise ValueError("tool timeout_seconds must be positive")

    worker_slots = _get_worker_slots()
    if not worker_slots.acquire(blocking=False):
        raise ToolTransientError("Tool timeout worker capacity is exhausted")

    outcomes: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            outcomes.put((True, callback()))
        except Exception as error:
            outcomes.put((False, error))
        finally:
            worker_slots.release()

    worker = threading.Thread(
        target=invoke,
        name="tool-runtime-worker",
        daemon=True,
    )
    worker.start()

    try:
        succeeded, value = outcomes.get(timeout=timeout_seconds)
    except Empty as error:
        raise ToolTimeout(
            f"Tool execution exceeded {timeout_seconds:g} seconds"
        ) from error

    if succeeded:
        return value
    raise value


def _coerce_tool_result(tool_name: str, result: Any) -> ToolResult:
    if isinstance(result, ToolResult):
        return result
    return ToolResult(tool_name=tool_name, success=True, result=result)


def _failure_message(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("error_message"):
            return str(value["error_message"])
        if value.get("reason"):
            return str(value["reason"])
        if value.get("errors"):
            return "; ".join(str(item) for item in value["errors"])
    return str(value)


def _normalize_failed_result(
    result: ToolResult,
    *,
    policy: ToolRuntimePolicy,
    fallback_action: str,
    runtime_metadata: dict[str, Any],
) -> ToolResult:
    payload = dict(result.result) if isinstance(result.result, dict) else {
        "reason": str(result.result)
    }
    payload.setdefault("error_type", "ToolExecutionError")
    payload.setdefault("error_message", _failure_message(result.result))
    payload.setdefault("fallback_action", fallback_action)
    payload.setdefault("retryable", False)
    payload.update(runtime_metadata)

    if policy.fail_closed:
        payload["fail_closed"] = True
    if policy.recovery_action:
        payload["recovery_action"] = policy.recovery_action

    return ToolResult(tool_name=result.tool_name, success=False, result=payload)


def _runtime_failure_result(
    tool_name: str,
    *,
    error: Exception,
    error_type: str,
    retryable: bool,
    fallback_action: str,
    policy: ToolRuntimePolicy,
    runtime_metadata: dict[str, Any],
) -> ToolResult:
    payload = {
        "error_type": error_type,
        "error_message": str(error),
        "reason": f"{tool_name} 工具调用失败，已进入降级处理。",
        "fallback_action": fallback_action,
        "retryable": retryable,
        **runtime_metadata,
    }
    if policy.fail_closed:
        payload["fail_closed"] = True
    if policy.recovery_action:
        payload["recovery_action"] = policy.recovery_action

    return ToolResult(tool_name=tool_name, success=False, result=payload)


def _execute_callback(
    tool_name: str,
    callback: Callable[[], Any],
    *,
    arguments: dict,
    trace: dict | None,
    fallback_action: str | None,
    runtime_policy: ToolRuntimePolicy | None,
) -> tuple[ToolResult, dict[str, Any]]:
    policy = _resolve_runtime_policy(tool_name, runtime_policy)
    effective_fallback = fallback_action or policy.fallback_action
    started_at = perf_counter()

    for attempt in range(1, policy.max_attempts + 1):
        add_tool_call_trace(
            trace,
            tool_name,
            arguments,
            attempt=attempt,
            policy=policy,
        )
        attempt_started_at = perf_counter()

        try:
            raw_result = _run_with_timeout(callback, policy.timeout_seconds)
        except Exception as error:
            error_type, error_is_transient = classify_tool_error(error)
            retryable = bool(
                policy.side_effect_class == READ_ONLY
                and error_is_transient
                and error_type in policy.retry_on
            )
            has_next_attempt = retryable and attempt < policy.max_attempts

            if has_next_attempt:
                _add_tool_retry_trace(
                    trace,
                    tool_name=tool_name,
                    attempt=attempt,
                    policy=policy,
                    error_type=error_type,
                    error=error,
                )
                if policy.backoff_seconds:
                    time.sleep(policy.backoff_seconds)
                continue

            metadata = {
                "attempt": attempt,
                "max_attempts": policy.max_attempts,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                "attempt_duration_ms": round(
                    (perf_counter() - attempt_started_at) * 1000,
                    2,
                ),
                "retryable": retryable,
                "side_effect_class": policy.side_effect_class,
            }
            return (
                _runtime_failure_result(
                    tool_name,
                    error=error,
                    error_type=error_type,
                    retryable=retryable,
                    fallback_action=effective_fallback,
                    policy=policy,
                    runtime_metadata=metadata,
                ),
                metadata,
            )

        result = _coerce_tool_result(tool_name, raw_result)
        metadata = {
            "attempt": attempt,
            "max_attempts": policy.max_attempts,
            "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            "attempt_duration_ms": round(
                (perf_counter() - attempt_started_at) * 1000,
                2,
            ),
            "retryable": False,
            "side_effect_class": policy.side_effect_class,
        }
        if not result.success:
            result = _normalize_failed_result(
                result,
                policy=policy,
                fallback_action=effective_fallback,
                runtime_metadata=metadata,
            )
        return result, metadata

    raise AssertionError("tool attempt loop completed without a result")


def safe_tool_call(
    tool_name: str,
    callback,
    fallback_action: str | None = None,
    *,
    trace: dict | None = None,
    arguments: dict | None = None,
    runtime_policy: ToolRuntimePolicy | None = None,
) -> ToolResult:
    result, runtime_metadata = _execute_callback(
        tool_name,
        callback,
        arguments=arguments or {},
        trace=trace,
        fallback_action=fallback_action,
        runtime_policy=runtime_policy,
    )
    add_tool_result_trace(trace, result, runtime_metadata)
    add_tool_failure_trace(trace, result)
    return result


def execute_tool(
    tool_name: str,
    arguments: dict,
    trace: dict | None = None,
    fallback_action: str | None = None,
    *,
    runtime_policy: ToolRuntimePolicy | None = None,
) -> ToolResult:
    runtime_metadata = {}

    def callback() -> ToolResult:
        nonlocal runtime_metadata
        result, runtime_metadata = _execute_callback(
            tool_name,
            lambda: execute_registered_tool(tool_name, arguments),
            arguments=arguments,
            trace=trace,
            fallback_action=fallback_action,
            runtime_policy=runtime_policy,
        )
        return result

    if trace:
        result = timed_step(
            trace,
            f"tool.{tool_name}",
            callback,
            {"tool_name": tool_name},
        )
    else:
        result = callback()

    add_tool_result_trace(trace, result, runtime_metadata)
    add_tool_failure_trace(trace, result)
    return result


def execute_agent_tool(
    agent_key: str,
    tool_name: str,
    arguments: dict,
    trace: dict | None = None,
    fallback_action: str | None = None,
    *,
    runtime_policy: ToolRuntimePolicy | None = None,
) -> ToolResult:
    policy = _resolve_runtime_policy(tool_name, runtime_policy)

    if not can_agent_use_tool(agent_key, tool_name):
        add_tool_call_trace(trace, tool_name, arguments, attempt=1, policy=policy)
        runtime_metadata = {
            "attempt": 1,
            "max_attempts": 1,
            "duration_ms": 0.0,
            "retryable": False,
            "side_effect_class": policy.side_effect_class,
        }
        result = _runtime_failure_result(
            tool_name,
            error=ToolPermissionDenied(f"{agent_key} 无权调用 {tool_name}。"),
            error_type="ToolPermissionDenied",
            retryable=False,
            fallback_action="reject_tool_call",
            policy=policy,
            runtime_metadata=runtime_metadata,
        )
        add_tool_result_trace(trace, result, runtime_metadata)
        add_tool_failure_trace(trace, result)
        return result

    return execute_tool(
        tool_name=tool_name,
        arguments=arguments,
        trace=trace,
        fallback_action=fallback_action,
        runtime_policy=runtime_policy,
    )


def add_tool_failure_trace(trace: dict | None, tool_result: ToolResult) -> None:
    if not trace or tool_result.success:
        return

    sanitized_result = sanitize_trace_value(tool_result.result)
    attempt = (
        sanitized_result.get("attempt")
        if isinstance(sanitized_result, dict)
        else None
    )
    for event in reversed(trace.get("events", [])):
        if event.get("event_type") == "tool_failed":
            message = event.get("message", {})
            if (
                message.get("tool_name") == tool_result.tool_name
                and message.get("attempt") == attempt
            ):
                return
        if event.get("event_type") == "tool_call":
            break

    add_trace_event(
        trace,
        event_type="tool_failed",
        data={
            "tool_name": tool_result.tool_name,
            "attempt": attempt,
            "max_attempts": (
                sanitized_result.get("max_attempts")
                if isinstance(sanitized_result, dict)
                else None
            ),
            "duration_ms": (
                sanitized_result.get("duration_ms")
                if isinstance(sanitized_result, dict)
                else None
            ),
            "error_type": (
                sanitized_result.get("error_type")
                if isinstance(sanitized_result, dict)
                else "ToolExecutionError"
            ),
            "fallback_action": (
                sanitized_result.get("fallback_action")
                if isinstance(sanitized_result, dict)
                else None
            ),
            "retryable": (
                bool(sanitized_result.get("retryable"))
                if isinstance(sanitized_result, dict)
                else False
            ),
            "side_effect_class": (
                sanitized_result.get("side_effect_class")
                if isinstance(sanitized_result, dict)
                else None
            ),
            "result": sanitized_result,
        },
    )
