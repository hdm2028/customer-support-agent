"""Launch an isolated, loopback-only graph persistence demo (no live LLM/payment)."""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
MARKER = ".durable-graph-demo"


def prepare_workspace(workspace: Path):
    workspace = workspace.resolve()
    if (workspace / MARKER).is_file():
        return workspace  # Keep the same source and databases across restarts.
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("Choose an empty workspace or an existing durable demo workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    for folder in ("app", "data/knowledge"):
        shutil.copytree(ROOT / folder, workspace / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "data/orders.json", workspace / "data/orders.json")
    shutil.copy2(Path(__file__), workspace / "_durable_server.py")
    (workspace / MARKER).write_text("isolated demo; source snapshot retained on restart\n", encoding="utf-8")
    return workspace


def isolated_env(workspace):
    # Start with OS essentials, not application credentials/config inherited from
    # the invoking shell. The source snapshot contains no .env file.
    names = {"systemroot", "windir", "path", "pathext", "temp", "tmp", "home",
             "userprofile", "appdata", "localappdata", "programfiles", "programfiles(x86)"}
    env = {key: value for key, value in os.environ.items() if key.lower() in names}
    env.update({
        "DATABASE_BACKEND": "sqlite", "DATABASE_PATH": str(workspace / "business.sqlite"),
        "SEED_DEMO_DATA": "true", "MQ_BACKEND": "sqlite", "PAYMENT_ADAPTER": "",
        "REDIS_URL": "", "REDIS_HOST": "", "MYSQL_DSN": "", "MYSQL_USER": "", "MYSQL_DATABASE": "",
        "ZHIPUAI_API_KEY": "", "ZHIPU_API_KEY": "", "BIGMODEL_API_KEY": "", "LLM_API_KEY": "",
        "RAG_EMBEDDING_PROVIDER": "local", "EMBEDDING_DIMENSIONS": "256",
        "CHUNK_STRATEGY": "fixed_256", "RAG_RANKING_MODE": "hybrid_rule",
        "LANGSMITH_TRACING": "false", "PYTHONPATH": str(workspace), "PYTHONIOENCODING": "utf-8",
    })
    return env


def server_command(workspace, port):
    return [sys.executable, "-X", "utf8", str(workspace / "_durable_server.py"),
            "--child", "--workspace", str(workspace), "--port", str(port)]


def stop_server(child):
    # Windows venv python.exe may launch a second interpreter. Stop the whole
    # owned child tree so recovery really starts with a new server process.
    if child.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    else:
        child.kill()
    child.wait(timeout=10)


def serve(workspace, port):
    if Path(__file__).resolve().parent != workspace or not (workspace / MARKER).is_file():
        raise RuntimeError("The child server must run from its isolated source snapshot")
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    from app.agent.entry import workflow
    from app.agent.entry.durable_runtime import DurableGraphRuntime
    from app.core.schemas import RouteDecision
    from app.rag.index_manager import get_rag_index_manager
    from app.storage.database import init_database

    # Two explicitly synthetic routes exercise real business nodes. This is a
    # runtime smoke fixture, not an intent/answer quality evaluation.
    def demo_route(query):
        if "申请退款10009" in query:
            return RouteDecision(intent="return_refund", action_type="execute", topic="refund_apply",
                                 order_id="10009", need_order=True, need_policy=True, need_risk_check=True,
                                 need_refund_request=True,
                                 tool_plan=["order_lookup", "policy_search", "risk_check", "refund_apply"])
        if "查询订单10009" in query:
            return RouteDecision(intent="order_lookup", action_type="query", topic="order_status",
                                 order_id="10009", need_order=True, tool_plan=["order_lookup"])
        raise ValueError("Only the two documented runtime fixture messages are supported")

    workflow.route_user_request = demo_route
    init_database()
    get_rag_index_manager().refresh()
    runtime = DurableGraphRuntime(workspace / "checkpoints.sqlite")

    @asynccontextmanager
    async def lifespan(app):
        yield
        runtime.close()

    app = FastAPI(title="Isolated durable graph demo", lifespan=lifespan)

    class RunRequest(BaseModel):
        message: Literal["查询订单10009", "申请退款10009"] = "查询订单10009"
        conversation_id: str | None = None
        pause_after_tools: bool = False

    def require_run(run_id):
        try:
            return runtime.status(run_id)
        except KeyError:
            raise HTTPException(404, "Unknown run_id") from None

    def sse(run_id, pause_after_tools=False):
        def encode():
            try:
                for event in runtime.events(run_id, pause_after_tools=pause_after_tools):
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            except Exception as error:
                yield "data: " + json.dumps({"type": "error", "run_id": run_id,
                                             "error": str(error)}, ensure_ascii=False) + "\n\n"
        return StreamingResponse(encode(), media_type="text/event-stream",
                                 headers={"X-Run-ID": run_id, "Cache-Control": "no-cache"})

    @app.get("/health")
    def health():
        return {"ready": True, "pid": os.getpid(), "mode": "isolated_fixture", "workers": 1}

    @app.post("/runs")
    def run(request: RunRequest):
        run_id = runtime.create(request.message, request.conversation_id)
        return runtime.run(run_id, pause_after_tools=request.pause_after_tools)

    @app.post("/runs/stream")
    def stream(request: RunRequest):
        run_id = runtime.create(request.message, request.conversation_id)
        return sse(run_id, request.pause_after_tools)

    @app.get("/runs/{run_id}")
    def status(run_id: str):
        return require_run(run_id)

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str):
        require_run(run_id)
        return runtime.run(run_id)

    @app.post("/runs/{run_id}/resume/stream")
    def resume_stream(run_id: str):
        require_run(run_id)
        return sse(run_id)

    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, log_level="warning")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT / "data/durable_demo")
    parser.add_argument("--port", type=int, default=8147)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        serve(args.workspace.resolve(), args.port)
        return 0
    workspace = prepare_workspace(args.workspace)
    print(f"Demo: http://127.0.0.1:{args.port}/docs\nPersistent workspace: {workspace}", flush=True)
    child = subprocess.Popen(server_command(workspace, args.port), cwd=workspace, env=isolated_env(workspace),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return child.wait()
    except KeyboardInterrupt:
        stop_server(child)
        return 0


if __name__ == "__main__":
    sys.exit(main())
