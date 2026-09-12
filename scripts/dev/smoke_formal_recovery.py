"""Authenticated production-route recovery smoke in two isolated processes.

Uses TestClient/ASGI rather than a network listener. Routing is a declared
fixture; tools, business writes, graph and checkpoint storage are real.
"""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def child(phase):
    from fastapi.testclient import TestClient
    from app.agent.entry import workflow
    from app.core.schemas import RouteDecision
    workflow.route_user_request = lambda query: RouteDecision(
        intent="return_refund", action_type="execute", topic="refund_apply", order_id="10009",
        need_order=True, need_policy=True, need_risk_check=True, need_refund_request=True,
        tool_plan=["order_lookup", "policy_search", "risk_check", "refund_apply"])
    if phase == "crash":
        def crash_after_tools(state):
            os._exit(73)  # No cleanup: preceding tool checkpoint must already be durable.
        workflow.build_model_context_node = crash_after_tools
    import main
    headers = {"Authorization": "Bearer " + "a"*32}
    other = {"Authorization": "Bearer " + "b"*32}
    with TestClient(main.app) as client:
        if phase == "crash":
            response = client.post("/agent/runs", headers=headers, json={"message": "申请退款10009"})
            assert response.status_code == 200, response.text
            created = response.json()
            Path("run.json").write_text(json.dumps(created), encoding="utf-8")
            client.post("/agent/runs/" + created["run_id"] + "/resume", headers=headers)
            raise AssertionError("crash injection was not reached")
        created = json.loads(Path("run.json").read_text(encoding="utf-8"))
        path = "/agent/runs/" + created["run_id"]
        assert client.get(path, headers=other).status_code == 404
        assert client.post(path+"/resume", headers=other).status_code == 404
        before = client.get(path, headers=headers).json()
        assert before["next_nodes"] == ["build_model_context"], before
        response = client.post(path+"/resume/stream", headers=headers)
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        assert [e["node"] for e in events if e["type"] == "node"] == [
            "build_model_context", "generate_reply", "persist_result"], events
        done = next(e["content"] for e in events if e["type"] == "done")
        again = client.post(path+"/resume", headers=headers)
        assert again.status_code == 200 and again.json() == done, again.text
        Path("resumed.json").write_text(json.dumps({"before": before, "events": events,
            "completed_replay_equal": True, "other_owner_rejected": True}, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    from durable_graph_demo import ROOT, prepare_workspace, isolated_env
    output = ROOT / "reports/semantic_durable_integration"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="formal-recovery-") as temporary:
        workspace = prepare_workspace(Path(temporary))
        shutil.copy2(ROOT / "main.py", workspace / "main.py")
        shutil.copytree(ROOT / "web", workspace / "web")
        shutil.copy2(Path(__file__), workspace / "smoke_child.py")
        env = isolated_env(workspace)
        env.update(AGENT_RUNTIME="durable", AGENT_CHECKPOINT_PATH=str(workspace / "checkpoints.sqlite"),
            AUTH_TOKENS=json.dumps({"a"*32: {"user_id": "u009", "role": "customer"},
                                   "b"*32: {"user_id": "u001", "role": "customer"}}))
        def launch(phase):
            result = subprocess.run([sys.executable, "-X", "utf8", "smoke_child.py", phase], cwd=workspace,
                env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (output / (phase+".log")).write_text(result.stdout+result.stderr, encoding="utf-8")
            return result.returncode
        def counts():
            with closing(sqlite3.connect(workspace / "business.sqlite")) as db:
                return {table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                        for table in ("refund_requests", "mq_messages", "conversation_messages", "agent_persisted_turns")}
        assert launch("crash") == 73, "Inspect crash.log"
        before = counts()
        assert before == {"refund_requests": 1, "mq_messages": 1, "conversation_messages": 0, "agent_persisted_turns": 0}, before
        assert launch("resume") == 0, "Inspect resume.log"
        after = counts()
        assert after == {"refund_requests": 1, "mq_messages": 1, "conversation_messages": 2, "agent_persisted_turns": 1}, after
        report = json.loads((workspace / "resumed.json").read_text(encoding="utf-8"))
        report.update(success=True, before_counts=before, after_counts=after,
                      scope="ASGI formal routes; two real processes; fixture routing; SQLite and local RAG; no external services")
        (output / "process_recovery.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"success": True, "before": before, "after": after}))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1])
    else:
        main()
