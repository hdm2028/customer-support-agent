import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.core.security import Principal, conversation_access, current_principal
from app.storage import database as db, cache
from app.tools.executor import execute_tool

TOKENS = {"a" * 32: {"user_id": "u009", "role": "customer"},
          "b" * 32: {"user_id": "u001", "role": "customer"},
          "c" * 32: {"user_id": "operator", "role": "admin"}}


class APIAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in [patch.dict("os.environ", {"DATABASE_BACKEND": "sqlite", "AUTH_TOKENS": json.dumps(TOKENS), "SEED_DEMO_DATA": "true"}),
                  patch.object(db, "DB_PATH", Path(self.tmp.name) / "auth.db"),
                  patch.object(db, "_INITIALIZED", False),
                  patch.object(cache, "_CACHE_BACKEND", cache.InMemoryTTLCache())]:
            p.start()
            self.addCleanup(p.stop)
        db.init_database()
        self.main = importlib.import_module("main")
        self.client = TestClient(self.main.app)
        self.addCleanup(self.client.close)

    def headers(self, user="a"):
        return {"Authorization": "Bearer " + user * 32}

    def conversation(self):
        token = current_principal.set(Principal("u009", "customer"))
        try:
            return conversation_access(None, create=True)
        finally:
            current_principal.reset(token)

    def test_missing_bad_and_unconfigured_credentials_fail_closed(self):
        self.assertEqual(self.client.get("/orders/10009").status_code, 401)
        self.assertEqual(self.client.get("/orders/10009", headers=self.headers("x")).status_code, 401)
        with patch.dict("os.environ", {"AUTH_TOKENS": ""}):
            self.assertEqual(self.client.get("/orders/10009", headers=self.headers()).status_code, 503)

    def test_customer_can_only_read_owned_order(self):
        self.assertEqual(self.client.get("/orders/10009", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/orders/10001", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.get("/orders/10001", headers=self.headers("c")).status_code, 200)

    def test_review_supplement_owner_access_retry_and_material_version(self):
        from app.tools.human_review import create_manual_review
        review = create_manual_review("10009", "refund", "high", [], "申请人工核实").result
        body = {"review_id": review["review_id"], "text": "补充包装破损说明", "submission_id": "submission-1"}
        self.assertEqual(self.client.post("/review-supplements", json=body).status_code, 401)
        self.assertEqual(self.client.post("/review-supplements", headers=self.headers("b"), json=body).status_code, 403)
        first = self.client.post("/review-supplements", headers=self.headers(), json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["supplement_version"], 1)
        retry = self.client.post("/review-supplements", headers=self.headers(), json=body)
        self.assertTrue(retry.json()["idempotent_replay"])
        path = f"/manual-reviews/{review['review_id']}/resolve"
        self.assertEqual(self.client.post(path, headers=self.headers(), json={"decision": "approve", "note": "test", "supplement_version": 1}).status_code, 403)
        stale = self.client.post(path, headers=self.headers("c"), json={"decision": "approve", "note": "旧页面", "supplement_version": 0})
        self.assertEqual(stale.status_code, 409)
        details = self.client.get(f"/manual-reviews/{review['review_id']}", headers=self.headers("c")).json()["data"]
        self.assertEqual(details["user_request"], "申请人工核实")
        self.assertEqual(details["supplements"][0]["text"], body["text"])
        decision = self.client.post(path, headers=self.headers("c"), json={"decision": "approve", "note": "已看补充", "supplement_version": details["supplement_version"]})
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(decision.json()["review"]["resolution"]["supplement_version"], 1)

    def test_ticket_details_and_management_are_scoped(self):
        ticket = db.save_ticket_to_db({"order_id": "10009", "user_id": "u009", "issue_type": "物流",
            "priority": "normal", "status": "pending_human_review", "user_request": "人工核实"})
        tid = ticket["ticket_id"]
        self.assertEqual(self.client.get(f"/tickets/{tid}", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get(f"/tickets/{tid}", headers=self.headers("b")).status_code, 403)
        self.assertEqual(self.client.get("/tickets/missing", headers=self.headers("c")).status_code, 404)
        for action, body in [("claim", {}), ("resolve", {"outcome": "answered", "note": "已说明"})]:
            path = f"/admin/tickets/{tid}/{action}"
            self.assertEqual(self.client.post(path, json=body, headers=self.headers()).status_code, 403)
            self.assertEqual(self.client.post(path, json=body, headers=self.headers("c")).status_code, 200)
        self.assertEqual(self.client.get(f"/tickets/{tid}", headers=self.headers()).json()["data"]["status"], "resolved")
        self.assertEqual(self.client.post(f"/admin/tickets/{tid}/resolve", headers=self.headers("c"),
            json={"outcome": "rejected", "note": "覆盖"}).status_code, 409)

    def test_ticket_list_http_paging_and_filter_validation(self):
        for i in range(3):
            db.save_ticket_to_db({"user_id": "u009", "order_id": "10009", "issue_type": "咨询", "priority": "normal",
                                 "status": "pending_human_review", "user_request": str(i)})
        first = self.client.get("/tickets?limit=2&status=open", headers=self.headers()).json()
        second = self.client.get("/tickets", params={"limit": 2, "status": "open", "cursor": first["next_cursor"]}, headers=self.headers()).json()
        self.assertEqual(len({t["ticket_id"] for t in first["data"] + second["data"]}), 3)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(self.client.get("/tickets?status=resolved", headers=self.headers()).json()["count"], 0)
        self.assertEqual(self.client.get("/tickets?cursor=bad", headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.get("/tickets?status=bogus", headers=self.headers()).status_code, 422)

    def test_operator_directory_and_reassignment_require_admin(self):
        tokens = {**TOKENS, "d" * 32: {"user_id": "second", "role": "admin"}}
        with patch.dict("os.environ", {"AUTH_TOKENS": json.dumps(tokens)}):
            self.assertEqual(self.client.get("/admin/operators", headers=self.headers()).status_code, 403)
            response = self.client.get("/admin/operators", headers=self.headers("c"))
            self.assertEqual(response.json()["data"], ["operator", "second"])
            self.assertNotIn("c" * 32, response.text)
            ticket = db.save_ticket_to_db({"user_id": "u009", "issue_type": "人工接管", "priority": "normal",
                                          "status": "pending_human_takeover", "user_request": "核实"})
            tid = ticket["ticket_id"]
            self.client.post(f"/admin/tickets/{tid}/claim", headers=self.headers("c"))
            body = {"target_user_id": "second", "expected_assignee": "operator", "note": "继续跟进"}
            path = f"/admin/tickets/{tid}/reassign"
            self.assertEqual(self.client.post(path, json=body, headers=self.headers()).status_code, 403)
            self.assertEqual(self.client.post(path, json=body, headers=self.headers("c")).status_code, 200)
            close = {"outcome": "answered", "note": "已核实"}
            self.assertEqual(self.client.post(f"/admin/tickets/{tid}/resolve", json=close, headers=self.headers("c")).status_code, 409)
            self.assertEqual(self.client.post(f"/admin/tickets/{tid}/resolve", json=close, headers=self.headers("d")).status_code, 200)

    def test_admin_routes_reject_customer_including_task_execution(self):
        for path in ("/manual-reviews", "/mq/messages", "/observability/metrics", "/observability/summary", "/knowledge/chunks"):
            self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 403, path)
            self.assertEqual(self.client.get(path, headers=self.headers("c")).status_code, 200, path)
        self.assertEqual(self.client.post("/refund-tasks/process", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.post("/manual-reviews/example/resolve", headers=self.headers(), json={"decision": "approve", "note": "test"}).status_code, 403)
        self.assertEqual(self.client.post("/admin/refunds/example/submit-payment", headers=self.headers()).status_code, 403)

    def test_liveness_and_readiness_have_different_status_codes(self):
        self.assertEqual(self.client.get("/live").status_code, 200)
        with patch.object(self.main, "readiness_report", return_value={"success": False, "checks": {"database": False}}):
            self.assertEqual(self.client.get("/ready").status_code, 503)
            self.assertEqual(self.client.get("/health").status_code, 503)
        with patch.object(self.main, "readiness_report", return_value={"success": True, "checks": {"database": True}}):
            self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_conversation_history_state_feedback_and_chat_are_scoped(self):
        cid = self.conversation()
        self.assertEqual(self.client.post("/agent/history", json={"conversation_id": cid}, headers=self.headers()).status_code, 200)
        for path, body in [("/agent/history", {"conversation_id": cid}),
                           ("/feedback", {"conversation_id": cid, "score": 5}),
                           ("/agent/chat", {"conversation_id": cid, "message": "你好"}),
                           ("/agent/stream", {"conversation_id": cid, "message": "你好"})]:
            self.assertEqual(self.client.post(path, json=body, headers=self.headers("b")).status_code, 403, path)
        self.assertEqual(self.client.get(f"/agent/state/{cid}", headers=self.headers("b")).status_code, 403)

    def test_unknown_or_legacy_conversation_cannot_be_claimed(self):
        db.append_message_to_db("legacy", "user", "private")
        for cid in ("legacy", "invented"):
            response = self.client.post("/agent/chat", json={"conversation_id": cid, "message": "继续"}, headers=self.headers())
            self.assertEqual(response.status_code, 403)

    def test_tool_timeout_thread_preserves_identity(self):
        token = current_principal.set(Principal("u009", "customer"))
        try:
            own = execute_tool("order_lookup", {"order_id": "10009"})
            other = execute_tool("order_lookup", {"order_id": "10001"})
            self.assertTrue(own.success)
            self.assertFalse(other.success)
            self.assertNotIn("蓝牙", str(other.result))
        finally:
            current_principal.reset(token)

    def test_cached_refund_cannot_bypass_order_ownership(self):
        from app.tools.refund import refund_apply
        from app.concurrency.refund_guard import cache_refund_idempotency
        cache_refund_idempotency("10001", {"refund_id": "private", "order_id": "10001"})
        token = current_principal.set(Principal("u009", "customer"))
        try:
            with self.assertRaises(PermissionError):
                refund_apply("10001", "退款")
        finally:
            current_principal.reset(token)

    def test_refund_lists_filter_before_limit(self):
        for oid, uid in (("10009", "u009"), ("10001", "u001")):
            db.save_refund_request_to_db({"order_id": oid, "user_id": uid, "status": "queued", "reason": "test", "risk_level": "low"})
        response = self.client.get("/refunds?limit=1", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["user_id"] for r in response.json()["data"]], ["u009"])

    def test_customer_can_cancel_only_owned_refund_through_api(self):
        from app.tools.refund import refund_apply
        refund_id = refund_apply("10009", "申请退款").result["refund_id"]
        route = f"/refunds/{refund_id}/cancel"
        self.assertEqual(self.client.post(route, headers=self.headers("b"), json={"note": "越权撤销"}).status_code, 403)
        result = self.client.post(route, headers=self.headers(), json={"note": "暂不退款"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["refund"]["status"], "cancelled")

    def test_admin_address_approval_persists_confirmed_address_through_api(self):
        from app.tools.human_review import create_manual_review
        db.update_order_in_db("10009", {"shipping_status": "未发货"})
        review = create_manual_review("10009", "address_change", "high", [], "修改地址").result
        result = self.client.post(f"/manual-reviews/{review['review_id']}/resolve", headers=self.headers("c"),
                                  json={"decision": "approve", "note": "用户已确认", "new_address": "测试省测试市新地址 18 号"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(db.get_order_from_db("10009")["shipping_address"], "测试省测试市新地址 18 号")


if __name__ == "__main__":
    unittest.main()
