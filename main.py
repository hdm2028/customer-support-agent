import json

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, StreamingResponse

from app.agent.entry.agent_core import (
    get_conversation_history,
    run_customer_support_agent,
    stream_customer_support_agent,
)
from app.agent.entry import persistent_api
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
from app.rag.index_manager import get_rag_index_manager
from app.rag.retriever import HybridRetriever
from app.mq.queue import list_messages
from app.services.refund_service import process_refund_tasks, repair_missing_refund_events
from app.storage.cache import cache_health, get_agent_state
from app.storage.database import (
    init_database,
    list_agent_metrics_from_db,
    list_manual_reviews_from_db,
    list_refund_requests_from_db,
    save_feedback_to_db,
    list_tickets_from_db,
)
from app.storage.store import get_order_by_id
from app.tools.policy import policy_search
from app.core.security import IdentityMiddleware, conversation_access, list_visible_records, current_principal
from app.services.review_service import resolve_review, review_details, append_review_supplement
from pydantic import BaseModel, Field
from typing import Literal
from app.services.payments import (PaymentNotConfigured, visible_refund,
                                  submit_refund_payment, reconcile_refund_payment)
from app.observability.operations import operational_summary
from app.observability.readiness import readiness_report
from app.services.refund_cancellation import cancel_refund
from app.services.ticket_service import claim_ticket, resolve_ticket, ticket_details, list_tickets_page, reassign_ticket


settings = get_settings()
app = FastAPI(title=settings.app_name)
app.add_middleware(IdentityMiddleware)
init_database()
knowledge_retriever = HybridRetriever()


@app.exception_handler(PermissionError)
async def permission_error(request, error):
    return JSONResponse({"detail": str(error)}, status_code=403)


class ReviewDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=2000)
    new_address: str | None = Field(default=None, min_length=6, max_length=500)
    supplement_version: int | None = Field(default=None, ge=0)


class ReviewSupplementRequest(BaseModel):
    review_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    submission_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class RefundCancellationRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class TicketResolutionRequest(BaseModel):
    outcome: Literal["answered", "rejected", "withdrawn"]
    note: str = Field(min_length=1, max_length=2000)


class TicketReassignmentRequest(BaseModel):
    target_user_id: str = Field(min_length=1, max_length=64)
    expected_assignee: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)


@app.get("/admin/operators")
def list_operators():
    from app.core.security import configured_operators
    return {"success": True, "data": configured_operators()}


@app.post("/admin/tickets/{ticket_id}/reassign")
def reassign_manual_ticket(ticket_id: str, req: TicketReassignmentRequest):
    return ticket_operation(reassign_ticket, ticket_id, req.target_user_id, req.expected_assignee, req.note)


def ticket_operation(callback, *args):
    try:
        return callback(*args)
    except LookupError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=409)


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    return ticket_operation(lambda: {"success": True, "data": ticket_details(ticket_id)})


@app.post("/admin/tickets/{ticket_id}/claim")
def claim_manual_ticket(ticket_id: str):
    return ticket_operation(claim_ticket, ticket_id)


@app.post("/admin/tickets/{ticket_id}/resolve")
def resolve_manual_ticket(ticket_id: str, req: TicketResolutionRequest):
    return ticket_operation(resolve_ticket, ticket_id, req.outcome, req.note)


@app.post("/refunds/{refund_id}/cancel")
def cancel_refund_request(refund_id: str, req: RefundCancellationRequest):
    try:
        return cancel_refund(refund_id, req.note)
    except LookupError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=409)


@app.post("/manual-reviews/{review_id}/resolve")
def resolve_manual_review(review_id: str, req: ReviewDecisionRequest):
    try:
        return resolve_review(review_id, req.decision, req.note, new_address=req.new_address,
                              supplement_version=req.supplement_version)
    except LookupError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=409)


@app.get("/manual-reviews/{review_id}")
def get_review_details(review_id: str):
    try:
        return {"success": True, "data": review_details(review_id)}
    except LookupError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)


@app.post("/review-supplements")
def submit_review_supplement(req: ReviewSupplementRequest):
    # Customer identity is accepted here; the service checks review ownership.
    return ticket_operation(append_review_supplement, req.review_id, req.text, req.submission_id)


def payment_operation(callback, refund_id):
    try:
        return callback(refund_id)
    except PaymentNotConfigured as error:
        return JSONResponse({"detail": str(error)}, status_code=503)
    except LookupError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=409)
    except (TimeoutError, ConnectionError):
        return JSONResponse({"detail": "支付渠道结果待核实，请查询状态，不要重复提交"}, status_code=502)


@app.get("/refunds/{refund_id}")
def get_refund_status(refund_id: str):
    try:
        return {"success": True, "data": visible_refund(refund_id)}
    except LookupError as error:
        return JSONResponse({"detail": str(error)}, status_code=404)


@app.post("/admin/refunds/{refund_id}/submit-payment")
def submit_payment(refund_id: str):
    return payment_operation(submit_refund_payment, refund_id)


@app.post("/admin/refunds/{refund_id}/reconcile-payment")
def reconcile_payment(refund_id: str):
    return payment_operation(reconcile_refund_payment, refund_id)


@app.on_event("startup")
def refresh_knowledge_index() -> None:
    get_rag_index_manager().refresh()


@app.on_event("shutdown")
def close_graph_runtime() -> None:
    persistent_api.close()


@app.get("/")
def web_app() -> FileResponse:
    return FileResponse(BASE_DIR / "web" / "index.html")


@app.get("/health")
@app.get("/ready")
def health_check():
    result = readiness_report()
    return JSONResponse(result, status_code=200 if result["success"] else 503)


@app.get("/live")
def liveness():
    return {"success": True}


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


@app.get("/me")
def current_identity() -> dict:
    principal = current_principal.get()
    return {"user_id": principal.user_id, "role": principal.role}


@app.post("/agent/chat", response_model=ChatResponse)
def agent_chat(req: ChatRequest) -> dict:
    conversation_id = conversation_access(req.conversation_id, create=True)
    if persistent_api.enabled():
        return persistent_api.run(persistent_api.create(req, conversation_id))
    return run_customer_support_agent(
        user_message=req.message,
        conversation_id=conversation_id,
        use_llm=req.use_llm,
    )


@app.post("/agent/stream")
def agent_stream(req: StreamChatRequest) -> StreamingResponse:
    conversation_id = conversation_access(req.conversation_id, create=True)
    if persistent_api.enabled():
        return persistent_api.stream(persistent_api.create(req, conversation_id, stream=True))
    async def event_generator():
        async for event in stream_customer_support_agent(
            user_message=req.message,
            conversation_id=conversation_id,
            use_llm=req.use_llm,
            stream_tokens=req.stream_tokens,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/agent/runs")
def create_agent_run(req: StreamChatRequest):
    conversation_id = conversation_access(req.conversation_id, create=True)
    run_id = persistent_api.create(req, conversation_id, stream=req.stream_tokens)
    return {"run_id": run_id, "conversation_id": conversation_id, "status": "suspended"}


@app.get("/agent/runs/{run_id}")
def agent_run_status(run_id: str):
    return persistent_api.status(run_id)


@app.post("/agent/runs/{run_id}/resume")
def resume_agent_run(run_id: str):
    return persistent_api.run(run_id)


@app.post("/agent/runs/{run_id}/resume/stream")
def resume_agent_run_stream(run_id: str):
    return persistent_api.stream(run_id)


@app.post("/agent/history", response_model=ChatHistoryResponse)
def agent_history(req: ChatHistoryRequest) -> ChatHistoryResponse:
    conversation_access(req.conversation_id)
    return ChatHistoryResponse(
        conversation_id=req.conversation_id,
        messages=get_conversation_history(req.conversation_id),
    )


@app.get("/agent/state/{conversation_id}")
def agent_state(conversation_id: str) -> dict:
    conversation_access(conversation_id)
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
    conversation_access(req.conversation_id)
    save_feedback_to_db(
        conversation_id=req.conversation_id,
        score=req.score,
        comment=req.comment,
    )

    return FeedbackResponse()


@app.get("/tickets")
def list_tickets(limit: int = Query(50, ge=1, le=100),
                 status: Literal["all", "open", "resolved"] = "all",
                 cursor: str | None = Query(None, min_length=1, max_length=512)):
    try:
        return list_tickets_page(limit, status, cursor)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=400)


@app.get("/refunds")
def list_refunds(limit: int = Query(50, ge=1, le=100)) -> dict:
    refunds = list_visible_records("refund_requests", limit)

    return {
        "success": True,
        "count": len(refunds),
        "data": refunds,
    }


@app.get("/manual-reviews")
def list_manual_reviews(limit: int = Query(50, ge=1, le=100)) -> dict:
    reviews = list_manual_reviews_from_db(limit=limit)

    return {
        "success": True,
        "count": len(reviews),
        "data": reviews,
    }


@app.get("/observability/metrics")
def observability_metrics(limit: int = Query(50, ge=1, le=100)) -> dict:
    metrics = list_agent_metrics_from_db(limit=limit)

    return {
        "success": True,
        "count": len(metrics),
        "data": metrics,
    }


@app.get("/observability/summary")
def observation_summary(window_seconds: int = Query(900, ge=1, le=86400)):
    try:
        return {"success": True, "data": operational_summary(window_seconds)}
    except Exception as error:
        return JSONResponse({"success": False, "error_type": type(error).__name__,
                             "detail": "运行指标读取失败"}, status_code=503)


@app.get("/mq/messages")
def mq_messages(limit: int = 50) -> dict:
    messages = list_messages(limit=limit)

    return {
        "success": True,
        "count": len(messages),
        "data": messages,
    }


@app.post("/refund-tasks/process")
def process_refund_task_batch(limit: int = Query(10, ge=1, le=100)) -> dict:
    return process_refund_tasks(limit=limit)


@app.post("/refund-tasks/reconcile-events")
def reconcile_refund_events(limit: int = Query(100, ge=1, le=100)) -> dict:
    return repair_missing_refund_events(limit)


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
    result = policy_search(
        semantic_query=query,
        lexical_query=query,
        top_k=top_k,
    )

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
