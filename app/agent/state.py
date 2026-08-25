from dataclasses import dataclass, field
from typing import Any

from app.core.schemas import RouteDecision, ToolResult


@dataclass
class AgentResult:
    """统一 Agent 返回值：结果先回到 Orchestrator，再写入共享状态。"""

    agent: str
    success: bool
    state_updates: dict[str, Any] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)
    next_hint: str | None = None
    error: str | None = None


@dataclass
class AgentState:
    """三个 Agent 协作时共享的状态载体。"""

    conversation_id: str
    user_message: str
    route: RouteDecision
    history: list[dict] = field(default_factory=list)
    pending_task: dict | None = None
    trace: dict | None = None
    intent: str | None = None
    order_id: str | None = None
    order: dict | None = None
    policy: list[dict] | dict | None = None
    risk: dict | None = None
    refund: dict | None = None
    ticket: dict | None = None
    manual_review: dict | None = None
    handoff: dict | None = None
    current_agent: str | None = None
    next_agent: str | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    final_response: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    agent_steps: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.intent = self.intent or self.route.intent
        self.order_id = self.order_id or self.route.order_id

    def add_tool_result(self, tool_result: ToolResult) -> None:
        self.tool_results.append(tool_result)

        if not tool_result.success:
            return

        if tool_result.tool_name == "order_lookup" and isinstance(tool_result.result, dict):
            self.order = tool_result.result
        elif tool_result.tool_name == "policy_search":
            self.policy = tool_result.result
        elif tool_result.tool_name == "risk_check" and isinstance(tool_result.result, dict):
            self.risk = tool_result.result
        elif tool_result.tool_name == "refund_apply" and isinstance(tool_result.result, dict):
            self.refund = tool_result.result
        elif tool_result.tool_name == "create_ticket" and isinstance(tool_result.result, dict):
            self.ticket = tool_result.result
        elif tool_result.tool_name == "create_manual_review" and isinstance(tool_result.result, dict):
            self.manual_review = tool_result.result
        elif tool_result.tool_name == "transfer_to_human" and isinstance(tool_result.result, dict):
            self.handoff = tool_result.result

    def apply_agent_result(self, result: AgentResult) -> None:
        for key, value in result.state_updates.items():
            if hasattr(self, key):
                setattr(self, key, value)

        for tool_result in result.tool_results:
            self.add_tool_result(tool_result)

        self.next_agent = result.next_hint
        self.agent_steps.append(
            {
                "agent_key": result.agent,
                "success": result.success,
                "tool_names": [item.tool_name for item in result.tool_results],
                "next_hint": result.next_hint,
                "error": result.error,
            }
        )

    def block(self, reason: str) -> None:
        self.blocked = True
        self.block_reason = reason

    def to_summary(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "intent": self.intent,
            "order_id": self.order_id,
            "current_agent": self.current_agent,
            "next_agent": self.next_agent,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "agent_steps": self.agent_steps,
            "tool_names": [item.tool_name for item in self.tool_results],
        }
