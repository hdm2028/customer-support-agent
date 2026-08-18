from typing import Any

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    key: str = Field(description="Agent 唯一标识")
    description: str = Field(description="Agent 能力描述")


class ServiceMetadata(BaseModel):
    app_name: str
    agents: list[AgentInfo]
    default_agent: str
    models: list[str]
    default_model: str
    rag_embedding_provider: str
    has_llm_key: bool


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户本轮输入")
    conversation_id: str | None = Field(default=None, description="多轮对话 ID")
    use_llm: bool = Field(default=False, description="是否调用真实大模型")


class StreamChatRequest(ChatRequest):
    stream_tokens: bool = Field(default=True, description="是否按 token 流式返回最终回复")


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    result: Any


class RouteDecision(BaseModel):
    intent: str = "general_support"
    confidence: float = 0.0
    routing_reason: str | None = None
    agent_plan: list[str] = Field(default_factory=list)
    tool_plan: list[str] = Field(default_factory=list)
    order_id: str | None = None
    need_order: bool = False
    need_policy: bool = False
    need_ticket: bool = False
    need_refund_request: bool = False
    need_risk_check: bool = False
    manual_review_required: bool = False
    need_handoff: bool = False
    blocked_by_guardrail: bool = False
    guardrail_reason: str | None = None
    need_clarification: bool = False
    clarification_question: str | None = None
    handoff_required: bool = False
    handoff_reason: str | None = None
    risk_level: str = "low"
    risk_flags: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    success: bool
    conversation_id: str
    orchestration: dict = Field(default_factory=dict)
    route: RouteDecision
    tool_results: list[ToolResult]
    reply: str
    model_messages: list[dict]
    timings: dict = Field(default_factory=dict)
    token_usage: dict = Field(default_factory=dict)
    duration_ms: float | None = None
    used_pending_task: bool = False
    used_conversation_context: bool = False
    conversation_context: dict = Field(default_factory=dict)
    effective_user_message: str | None = None
    slots: dict = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    workflow_engine: str | None = None


class ChatHistoryRequest(BaseModel):
    conversation_id: str = Field(..., description="要查询的多轮对话 ID")


class ChatHistoryResponse(BaseModel):
    conversation_id: str
    messages: list[dict]


class FeedbackRequest(BaseModel):
    conversation_id: str
    score: int = Field(..., ge=1, le=5, description="用户评分，1 到 5")
    comment: str | None = Field(default=None, description="用户反馈文本")


class FeedbackResponse(BaseModel):
    success: bool = True
