import json

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from app.agent.agent_core import (
    get_conversation_history,
    run_customer_support_agent,
    stream_customer_support_agent,
)
from app.agent.tool_registry import get_function_tool_specs
from app.core.config import BASE_DIR, get_settings
from app.core.schemas import (
    AgentInfo,
    ChatHistoryRequest,
    ChatHistoryResponse,
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    FeedbackResponse,
    ServiceMetadata,
    StreamChatRequest,
)
from app.rag.rag import list_chunks
from app.storage.database import init_database, list_tickets_from_db
from app.storage.feedback_store import save_feedback
from app.storage.store import get_order_by_id
from app.tools.support_tools import policy_search


settings = get_settings()
app = FastAPI(title=settings.app_name)
init_database()


@app.get("/")
def web_app() -> FileResponse:
    """返回浏览器聊天页面，复用参考项目的 Chat UI 思路。"""

    return FileResponse(BASE_DIR / "web" / "index.html")


@app.get("/health")
def health_check() -> dict:
    """服务健康检查，前端和部署平台都可以用它判断后端是否存活。"""

    return {
        "success": True,
        "app_name": settings.app_name,
        "has_llm_key": settings.has_llm_key,
        "rag_embedding_provider": settings.rag_embedding_provider,
        "zhipu_embedding_model": settings.zhipu_embedding_model,
    }


@app.get("/info", response_model=ServiceMetadata)
def service_info() -> ServiceMetadata:
    """返回服务元数据，前端可以用它渲染标题、Agent 列表和模型状态。"""

    return ServiceMetadata(
        app_name=settings.app_name,
        agents=[
            AgentInfo(
                key="customer-support-agent",
                description="中文电商智能售后客服 Agent，支持订单查询、RAG 政策检索和工单草稿。",
            )
        ],
        default_agent="customer-support-agent",
        models=[settings.zhipu_model],
        default_model=settings.zhipu_model,
        rag_embedding_provider=settings.rag_embedding_provider,
        has_llm_key=settings.has_llm_key,
    )


@app.post("/agent/chat", response_model=ChatResponse)
def agent_chat(req: ChatRequest) -> dict:
    """非流式聊天接口，适合 Swagger 调试和后端自动化测试。"""

    return run_customer_support_agent(
        user_message=req.message,
        conversation_id=req.conversation_id,
        use_llm=req.use_llm,
    )


@app.post("/agent/stream")
async def agent_stream(req: StreamChatRequest) -> StreamingResponse:
    """流式聊天接口，用 SSE 格式把路由、工具结果和回复片段推给前端。"""

    async def event_generator():
        async for event in stream_customer_support_agent(
            user_message=req.message,
            conversation_id=req.conversation_id,
            use_llm=req.use_llm,
            stream_tokens=req.stream_tokens,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/agent/history", response_model=ChatHistoryResponse)
def agent_history(req: ChatHistoryRequest) -> ChatHistoryResponse:
    """根据 conversation_id 查询多轮对话历史。"""

    return ChatHistoryResponse(
        conversation_id=req.conversation_id,
        messages=get_conversation_history(req.conversation_id),
    )


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest) -> FeedbackResponse:
    """记录用户对本轮对话的评分，后续可用于评估集和错误案例复盘。"""

    save_feedback(
        conversation_id=req.conversation_id,
        score=req.score,
        comment=req.comment,
    )

    return FeedbackResponse()


@app.get("/tickets")
def list_tickets(limit: int = 50) -> dict:
    """查看最近创建的工单草稿，方便演示和排查售后流转。"""

    tickets = list_tickets_from_db(limit=limit)

    return {
        "success": True,
        "count": len(tickets),
        "data": tickets,
    }


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    """订单查询调试接口。"""

    order = get_order_by_id(order_id)

    if not order:
        return {
            "success": False,
            "error": f"未找到订单号 {order_id}",
        }

    return {
        "success": True,
        "data": order,
    }


@app.get("/knowledge/search")
def search_knowledge(query: str, top_k: int = 2) -> dict:
    """知识库检索调试接口。"""

    result = policy_search(query=query, top_k=top_k)

    return {
        "success": result.success,
        "data": result.result,
    }


@app.get("/tools")
def list_function_tools() -> dict:
    """查看当前受控 Function Calling 工具 schema。"""

    tools = get_function_tool_specs()

    return {
        "success": True,
        "count": len(tools),
        "data": tools,
    }


@app.get("/knowledge/chunks")
def get_knowledge_chunks() -> dict:
    """查看当前知识库切分结果，方便调试 RAG chunk 是否合理。"""

    chunks = list_chunks()

    return {
        "success": True,
        "count": len(chunks),
        "data": chunks,
    }
