import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.agent.entry import persistent_api, workflow
from app.agent.entry.durable_runtime import DurableGraphRuntime
from app.agent.routing.memory import ConversationMemory
from app.core.schemas import RouteDecision
from app.storage import database as db, cache


class FormalPersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        tokens = {"a"*32: {"user_id": "u009", "role": "customer"},
                  "b"*32: {"user_id": "u001", "role": "customer"}}
        for item in (patch.dict("os.environ", {"DATABASE_BACKEND": "sqlite", "SEED_DEMO_DATA": "true",
                                              "AUTH_TOKENS": json.dumps(tokens), "AGENT_RUNTIME": "durable"}),
                     patch.object(db, "DB_PATH", self.root / "business.sqlite"), patch.object(db, "_INITIALIZED", False),
                     patch.object(cache, "_CACHE_BACKEND", cache.InMemoryTTLCache())):
            item.start(); self.addCleanup(item.stop)
        db.init_database()
        self.runtime = DurableGraphRuntime(self.root / "checkpoints.sqlite")
        self.addCleanup(self.runtime.close)
        binding = patch.object(persistent_api, "runtime", return_value=self.runtime)
        binding.start(); self.addCleanup(binding.stop)
        routing = patch.object(workflow, "route_user_request", return_value=RouteDecision(
            intent="order_lookup", action_type="query", order_id="10009", need_order=True, tool_plan=["order_lookup"]))
        routing.start(); self.addCleanup(routing.stop)
        self.client = TestClient(importlib.import_module("main").app)
        self.addCleanup(self.client.close)

    def headers(self, user="a"):
        return {"Authorization": "Bearer " + user*32}

    def test_formal_chat_and_stream_share_graph(self):
        result = self.client.post("/agent/chat", headers=self.headers(), json={"message": "查询订单10009"})
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertTrue(body["run_id"])
        response = self.client.post("/agent/stream", headers=self.headers(), json={"message": "查询订单10009"})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        done = next(e for e in events if e["type"] == "done")
        self.assertEqual(done["content"]["reply"], body["reply"])
        self.assertEqual(len([e for e in events if e["type"] == "node"]), 6)

    def test_only_initiator_can_resume_and_completed_resume_is_read_only(self):
        created = self.client.post("/agent/runs", headers=self.headers(), json={"message": "查询订单10009"}).json()
        path = "/agent/runs/" + created["run_id"]
        self.assertEqual(self.client.get(path, headers=self.headers("b")).status_code, 404)
        self.assertEqual(self.client.post(path+"/resume", headers=self.headers("b")).status_code, 404)
        first = self.client.post(path+"/resume", headers=self.headers()).json()
        self.assertTrue(first["success"], first)
        again = self.client.post(path+"/resume", headers=self.headers()).json()
        self.assertEqual(first, again)
        self.assertEqual(len(db.load_messages_from_db(created["conversation_id"], 100)), 2)

    def test_unfinished_conversation_cannot_start_overlapping_run(self):
        created = self.client.post("/agent/runs", headers=self.headers(), json={"message": "查询订单10009"}).json()
        response = self.client.post("/agent/chat", headers=self.headers(), json={
            "message": "查询订单10009", "conversation_id": created["conversation_id"]})
        self.assertEqual(response.status_code, 409)

    def test_persisted_turn_retry_does_not_duplicate_and_conflict_is_rejected(self):
        memory = ConversationMemory()
        memory.append_turn("c", "question", "answer", turn_id="turn")
        memory.append_turn("c", "question", "answer", turn_id="turn")
        self.assertEqual(len(memory.load("c")), 2)
        with self.assertRaises(ValueError):
            memory.append_turn("c", "question", "different", turn_id="turn")
        self.assertEqual(len(memory.load("c")), 2)

    def test_resume_after_message_commit_before_checkpoint(self):
        created = self.client.post("/agent/runs", headers=self.headers(), json={"message": "查询订单10009"}).json()
        path = "/agent/runs/" + created["run_id"]
        with patch.object(workflow, "save_trace", side_effect=RuntimeError("injected after message commit")):
            interrupted = self.client.post(path+"/resume", headers=self.headers())
        self.assertEqual(interrupted.status_code, 503)
        self.assertEqual(len(db.load_messages_from_db(created["conversation_id"], 100)), 2)
        resumed = self.client.post(path+"/resume", headers=self.headers())
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(len(db.load_messages_from_db(created["conversation_id"], 100)), 2)
if __name__ == "__main__":
    unittest.main()
