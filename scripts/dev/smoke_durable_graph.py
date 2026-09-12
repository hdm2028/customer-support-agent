"""Bounded HTTP + process-restart smoke; does not load any evaluation dataset."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

from durable_graph_demo import ROOT, isolated_env, prepare_workspace, server_command, stop_server

NODES = ["load_context", "route", "orchestrate_agents", "build_model_context", "generate_reply", "persist_result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/durable_graph_prototype")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    workspace = prepare_workspace(output / "runtime" / stamp)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    http = build_opener(ProxyHandler({}))
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "workspace": str(workspace),
              "scope": "runtime smoke only; fixed routing fixtures, local RAG, SQLite, no model/payment API",
              "checks": [], "success": False}
    child = None
    logs = []
    started = monotonic()

    def check(name, ok, **details):
        report["checks"].append({"name": name, "passed": bool(ok), **details})
        if not ok:
            raise AssertionError(name)

    def request(path, payload=None, stream=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(base + path, data=body, headers={"Content-Type": "application/json"})
        with http.open(req, timeout=30) as response:
            text = response.read().decode("utf-8")
            if stream:
                return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
            return json.loads(text)

    def start_server(phase):
        nonlocal child
        log = (output / f"{stamp}_{phase}.log").open("x", encoding="utf-8")
        logs.append(log)
        child = subprocess.Popen(server_command(workspace, port), cwd=workspace, env=isolated_env(workspace),
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = monotonic() + 40
        while monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"Demo server exited; inspect {log.name}")
            try:
                health = request("/health")
                if health["ready"]:
                    return health
            except (URLError, TimeoutError):
                pass
            sleep(0.1)
        raise TimeoutError(f"Demo server not ready; inspect {log.name}")

    def db_counts(conversation_id):
        with sqlite3.connect(workspace / "business.sqlite") as connection:
            return {
                "refund_requests": connection.execute("SELECT count(*) FROM refund_requests").fetchone()[0],
                "mq_messages": connection.execute("SELECT count(*) FROM mq_messages").fetchone()[0],
                "conversation_messages": connection.execute(
                    "SELECT count(*) FROM conversation_messages WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()[0],
            }

    try:
        first = start_server("before_restart")
        normal = request("/runs", {"message": "查询订单10009", "conversation_id": "normal-query"})
        events = request("/runs/stream", {"message": "查询订单10009", "conversation_id": "sse-query"}, stream=True)
        stream_result = events[-1]["content"]
        check("buffered_and_sse_share_six_graph_nodes", normal["status"] == "completed"
              and normal["completed_nodes"] == NODES and events[-1]["type"] == "done"
              and [e["node"] for e in events if e["type"] == "node"] == NODES)
        check("buffered_and_sse_same_reply_route_and_tools",
              all(normal["result"][key] == stream_result["result"][key]
                  for key in ("reply", "route", "tool_results"))
              and db_counts("normal-query")["conversation_messages"] == 2
              and db_counts("sse-query")["conversation_messages"] == 2)
        report["buffered"] = normal
        report["sse_events"] = events

        paused_events = request("/runs/stream", {"message": "申请退款10009", "conversation_id": "refund-recovery",
                                                  "pause_after_tools": True}, stream=True)
        paused = paused_events[-1]["content"]
        run_id = paused["run_id"]
        counts_before = db_counts("refund-recovery")
        refund_tool = next((t for t in paused["tool_results"] if t["tool_name"] == "refund_apply"), None)
        check("pause_commits_tools_and_next_node",
              paused_events[-1]["type"] == "paused" and paused["next_nodes"] == ["build_model_context"]
              and paused["completed_nodes"] == NODES[:3] and refund_tool is not None and refund_tool["success"]
              and counts_before == {"refund_requests": 1, "mq_messages": 1, "conversation_messages": 0},
              counts=counts_before, next_nodes=paused["next_nodes"])
        report["paused_events"] = paused_events

        # TerminateProcess on Windows / SIGKILL on POSIX: no graceful lifespan or
        # saver.close. The paused notification follows a durable checkpoint.
        stop_server(child)
        second = start_server("after_restart")
        restored = request(f"/runs/{run_id}")
        check("new_process_restores_checkpoint",
              first["pid"] != second["pid"] and restored == paused,
              old_pid=first["pid"], new_pid=second["pid"], checkpoint_id=restored["checkpoint_id"])

        resumed_events = request(f"/runs/{run_id}/resume/stream", {}, stream=True)
        resumed = resumed_events[-1]["content"]
        check("resume_executes_only_remaining_nodes",
              resumed_events[-1]["type"] == "done" and resumed["status"] == "completed"
              and [e["node"] for e in resumed_events if e["type"] == "node"] == NODES[3:]
              and resumed["completed_nodes"] == NODES and resumed["trace_id"] == paused["trace_id"]
              and resumed["tool_results"] == paused["tool_results"])
        counts_after = db_counts("refund-recovery")
        check("resume_does_not_repeat_refund_or_mq",
              counts_after == {"refund_requests": 1, "mq_messages": 1, "conversation_messages": 2},
              before=counts_before, after=counts_after)
        report["resumed_events"] = resumed_events

        repeated = request(f"/runs/{run_id}/resume", {})
        repeated_stream = request(f"/runs/{run_id}/resume/stream", {}, stream=True)
        check("completed_resume_returns_saved_result_without_execution",
              repeated == resumed and repeated_stream == [{"type": "done", "content": resumed}]
              and db_counts("refund-recovery") == counts_after)
        missing_status = None
        try:
            request("/runs/unknown-run/resume", {})
        except HTTPError as error:
            missing_status = error.code
        check("unknown_run_returns_404", missing_status == 404)

        # A new turn in the same conversation gets a different checkpoint thread.
        followup = request("/runs", {"message": "查询订单10009", "conversation_id": "refund-recovery"})
        check("new_turn_has_own_thread_and_preserves_prior_run",
              followup["run_id"] != run_id and followup["status"] == "completed"
              and request(f"/runs/{run_id}") == resumed
              and db_counts("refund-recovery")["conversation_messages"] == 4)
        trace_path = workspace / "data/traces/agent_trace.jsonl"
        traces = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        recovered_trace = [t for t in traces if t["trace_id"] == resumed["trace_id"]]
        check("recovery_preserves_trace_and_usable_wall_duration",
              len(recovered_trace) == 1 and recovered_trace[0]["graph_nodes"] == NODES
              and recovered_trace[0]["duration_basis"] == "wall_clock_including_recovery_pause"
              and 0 <= recovered_trace[0]["duration_ms"] <= (monotonic() - started) * 1000 + 1000)
        report["recovered_trace"] = recovered_trace[0]
        report["success"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if child is not None and child.poll() is None:
            stop_server(child)
        for log in logs:
            log.close()
        report["duration_seconds"] = round(monotonic() - started, 3)
        report_path = output / f"smoke_{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"success": report["success"], "checks_passed": sum(c["passed"] for c in report["checks"]),
                          "checks_executed": len(report["checks"]), "report": str(report_path),
                          "error": report.get("error")}, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
