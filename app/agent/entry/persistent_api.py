"""Authenticated run creation/recovery; shared graph for buffered and SSE output."""
from functools import lru_cache
import json
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import BASE_DIR
from app.core.security import conversation_access, current_principal


def enabled():
    mode = os.getenv("AGENT_RUNTIME", "durable")
    if mode not in {"durable", "legacy"}:
        raise ValueError("AGENT_RUNTIME must be durable or legacy")
    return mode == "durable"


@lru_cache(maxsize=1)
def runtime():
    from app.agent.entry.durable_runtime import DurableGraphRuntime
    return DurableGraphRuntime(Path(os.getenv("AGENT_CHECKPOINT_PATH", str(BASE_DIR / "data/checkpoints/agent.sqlite"))))


def close():
    if runtime.cache_info().currsize:
        runtime().close()
        runtime.cache_clear()


def create(request, conversation_id, *, stream=False):
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("缺少请求身份")
    conversation_access(conversation_id)
    try:
        return runtime().create(request.message, conversation_id, use_llm=request.use_llm,
                                stream_tokens=stream and getattr(request, "stream_tokens", True),
                                owner_id=principal.user_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


def authorize(run_id, *, resume=False):
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("缺少请求身份")
    try:
        owner, conversation_id = runtime().owner(run_id)
    except KeyError:
        raise HTTPException(404, "运行不存在或不可访问") from None
    if owner != principal.user_id and (resume or principal.role != "admin"):
        raise HTTPException(404, "运行不存在或不可访问")
    conversation_access(conversation_id)
    return conversation_id


def status(run_id):
    authorize(run_id)
    try:
        return runtime().status(run_id)
    except KeyError:
        raise HTTPException(404, "运行状态尚未保存") from None


def run(run_id):
    authorize(run_id, resume=True)
    try:
        view = runtime().run(run_id)
        if view["status"] != "completed":
            return JSONResponse(status_code=409, content=view)
        return {**view["result"], "run_id": run_id}
    except Exception as error:
        return JSONResponse(status_code=503, content={"run_id": run_id, "status": "interrupted",
                            "error_type": type(error).__name__, "detail": "执行中断，可查询运行状态后恢复"})


def stream(run_id):
    conversation_id = authorize(run_id, resume=True)
    def events():
        try:
            for event in runtime().events(run_id):
                if event["type"] == "done":
                    event = {"type": "done", "content": {**event["content"]["result"], "run_id": run_id}}
                yield "data: " + json.dumps({**event, "conversation_id": conversation_id,
                                              "run_id": run_id}, ensure_ascii=False) + "\n\n"
        except Exception as error:
            yield "data: " + json.dumps({"type": "error", "run_id": run_id,
                        "error_type": type(error).__name__, "content": "执行中断，可查询运行状态后恢复"}, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"X-Run-ID": run_id, "Cache-Control": "no-cache"})
