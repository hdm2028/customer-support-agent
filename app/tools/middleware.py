"""Small synchronous middleware contract for the existing tool executor."""
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.schemas import ToolResult
from app.tools.registry import ToolRuntimePolicy


active_task_tools = ContextVar("active_task_tools", default=None)


@contextmanager
def task_tool_scope(tools):
    parent = active_task_tools.get()
    allowed = frozenset(tools)
    token = active_task_tools.set(allowed if parent is None else allowed & parent)
    try:
        yield
    finally:
        active_task_tools.reset(token)


@dataclass
class ToolCall:
    tool_name: str
    arguments: dict
    callback: Callable[[], Any]
    policy: ToolRuntimePolicy
    trace: dict | None = None
    fallback_action: str | None = None
    agent_key: str | None = None
    enforce_agent_permissions: bool = False
    record_timing: bool = True
    runtime_metadata: dict = field(default_factory=dict)


ToolHandler = Callable[[ToolCall], ToolResult]
ToolMiddleware = Callable[[ToolCall, ToolHandler], ToolResult]


def compose(middlewares: Sequence[ToolMiddleware], terminal: ToolHandler) -> ToolHandler:
    """First middleware enters first and exits last; it may short-circuit."""
    handler = terminal
    for middleware in reversed(middlewares):
        handler = partial(middleware, call_next=handler)
    return handler
