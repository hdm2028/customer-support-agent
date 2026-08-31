import json

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from app.agent.entry.agent_core import (
    get_conversation_history,
    run_customer_support_agent,
    stream_customer_support_agent,
)
from app.tools.registry import get_function_tool_specs
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
from app.rag.retriever import HybridRetriever
from app.mq.queue import list_messages
from app.services.refund_service import process_refund_tasks
from app.storage.cache import cache_health, get_agent_state
from app.storage.database import (
    database_health,
    get_database_backend_name,
    init_database,
    list_agent_metrics_from_db,
    list_manual_reviews_from_db,
    list_refund_requests_from_db,
    save_feedback_to_db,
    list_tickets_from_db,
)
from app.storage.store import get_order_by_id
from app.tools.policy import policy_search


settings = get_settings()
app = FastAPI(title=settings.app_name)
init_database()
knowledge_retriever = HybridRetriever()


@app.get("/")
def web_app() -> FileResponse:
    return FileResponse(BASE_DIR / "web" / "index.html")


@app.get("/health")
def health_check() -> dict:
    return {
        "success": True,
        "app_name": settings.app_name,
        "has_llm_key": settings.has_llm_key,
        "rag_embedding_provider": settings.rag_embedding_provider,
        "zhipu_embedding_model": settings.zhipu_embedding_model,
        "database_backend": get_database_backend_name(),
        "database": database_health(),
        "cache": cache_health(),
        "redis_enabled": bool(settings.redis_url),
        "mysql_configured": bool(settings.mysql_dsn),
        "mq_backend": settings.mq_backend,
        "rag_retrieval_mode": "hybrid_vector_bm25_keyword",
    }


@app.get("/info", response_model=ServiceMetadata)
def service_info() -> ServiceMetadata:
    return ServiceMetadata(
        app_name=settings.app_name,
        agents=[
            AgentInfo(
                key="agent_orchestrator",
                description="Agent Orchestrator：负责路由用户请求并协调客服、售后、风控 Agent。",
            ),
            AgentInfo(
                key="customer_agent",
                description="客服问答 Agent：负责普通咨询、意图识别、知识库 RAG 和回复生成。",
            ),
            AgentInfo(
                key="after_sales_agent",
                description="售后处理 Agent：负责订单查询、退款申请、售后工单、MQ 任务和业务流程执行。",
            ),
            AgentInfo(
                key="risk_agent",
                description="风控 Agent：负责高频退款、异常账号、恶意投诉、虚假描述和人工审核判断。",
            )
        ],
        default_agent="agent_orchestrator",
        models=[settings.zhipu_model],
        default_model=settings.zhipu_model,
        rag_embedding_provider=settings.rag_embedding_provider,
        has_llm_key=settings.has_llm_key,
    )


@app.post("/agent/chat", response_model=ChatResponse)
def agent_chat(req: ChatRequest) -> dict:
    return run_customer_support_agent(
        user_message=req.message,
        conversation_id=req.conversation_id,
        use_llm=req.use_llm,
    )


@app.post("/agent/stream")
async def agent_stream(req: StreamChatRequest) -> StreamingResponse:
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
    return ChatHistoryResponse(
        conversation_id=req.conversation_id,
        messages=get_conversation_history(req.conversation_id),
    )


@app.get("/agent/state/{conversation_id}")
def agent_state(conversation_id: str) -> dict:
    return {
        "success": True,
        "data": get_agent_state(conversation_id),
    }


@app.get("/cache/health")
def get_cache_health() -> dict:
    return {
        "success": True,
        "data": cache_health(),
    }


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest) -> FeedbackResponse:
    save_feedback_to_db(
        conversation_id=req.conversation_id,
        score=req.score,
        comment=req.comment,
    )

    return FeedbackResponse()


@app.get("/tickets")
def list_tickets(limit: int = 50) -> dict:
    tickets = list_tickets_from_db(limit=limit)

    return {
        "success": True,
        "count": len(tickets),
        "data": tickets,
    }


@app.get("/refunds")
def list_refunds(limit: int = 50) -> dict:
    refunds = list_refund_requests_from_db(limit=limit)

    return {
        "success": True,
        "count": len(refunds),
        "data": refunds,
    }


@app.get("/manual-reviews")
def list_manual_reviews(limit: int = 50) -> dict:
    reviews = list_manual_reviews_from_db(limit=limit)

    return {
        "success": True,
        "count": len(reviews),
        "data": reviews,
    }


@app.get("/observability/metrics")
def observability_metrics(limit: int = 50) -> dict:
    metrics = list_agent_metrics_from_db(limit=limit)

    return {
        "success": True,
        "count": len(metrics),
        "data": metrics,
    }


@app.get("/mq/messages")
def mq_messages(limit: int = 50) -> dict:
    messages = list_messages(limit=limit)

    return {
        "success": True,
        "count": len(messages),
        "data": messages,
    }


@app.post("/refund-tasks/process")
def process_refund_task_batch(limit: int = 10) -> dict:
    return process_refund_tasks(limit=limit)


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
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
    result = policy_search(query=query, top_k=top_k)

    return {
        "success": result.success,
        "data": result.result,
    }


@app.get("/knowledge/catalog")
def knowledge_catalog() -> dict:
    catalog = knowledge_retriever.catalog()

    return {
        "success": True,
        "data": catalog,
    }


@app.get("/tools")
def list_function_tools() -> dict:
    tools = get_function_tool_specs()

    return {
        "success": True,
        "count": len(tools),
        "data": tools,
    }


@app.get("/knowledge/chunks")
def get_knowledge_chunks() -> dict:
    chunks = knowledge_retriever.list_chunks()

    return {
        "success": True,
        "count": len(chunks),
        "data": chunks,
    }
