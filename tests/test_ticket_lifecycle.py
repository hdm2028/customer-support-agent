from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.core.security import Principal, current_principal
from app.services import ticket_service as service
from app.services.review_service import resolve_review
from app.storage import database as db
from app.tools.human_review import create_manual_review


class TicketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        token = current_principal.set(Principal("operator", "admin"))
        self.addCleanup(current_principal.reset, token)

    def ticket(self, status="pending_human_review"):
        return db.save_ticket_to_db({"order_id": "10009", "user_id": "u009",
            "issue_type": "物流咨询", "priority": "normal", "status": status,
            "user_request": "请人工核实物流"})["ticket_id"]

    def test_followup_from_approved_review_can_be_claimed_and_closed(self):
        review = create_manual_review("10009", "risk_control", "high", ["需核实"], "人工跟进").result
        result = resolve_review(review["review_id"], "approve", "已核实，交由专人回复")
        tid = result["review"]["continuation"]["ticket_id"]
        before = db.get_order_from_db("10009")
        service.claim_ticket(tid)
        closed = service.resolve_ticket(tid, "answered", "已向用户说明当前物流状态")["ticket"]
        self.assertEqual(closed["status"], "resolved")
        self.assertEqual(closed["review_id"], review["review_id"])
        self.assertEqual([a["action"] for a in closed["audit"]], ["claim", "resolve"])
        self.assertEqual(db.get_order_from_db("10009"), before)
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_retries_preserve_audit_and_conflicting_resolution_is_rejected(self):
        tid = self.ticket()
        service.claim_ticket(tid)
        self.assertTrue(service.claim_ticket(tid)["idempotent_replay"])
        service.resolve_ticket(tid, "withdrawn", "用户确认撤回跟进诉求")
        self.assertTrue(service.resolve_ticket(tid, "withdrawn", "用户确认撤回跟进诉求")["idempotent_replay"])
        with self.assertRaises(ValueError):
            service.resolve_ticket(tid, "answered", "不同结果")
        with self.assertRaises(ValueError):
            service.claim_ticket(tid)
        self.assertEqual(len(service.ticket_details(tid)["audit"]), 2)

    def test_cannot_close_unclaimed_or_another_operators_ticket(self):
        tid = self.ticket()
        with self.assertRaises(ValueError):
            service.resolve_ticket(tid, "answered", "尚未认领")
        service.claim_ticket(tid)
        token = current_principal.set(Principal("other", "admin"))
        try:
            with self.assertRaises(ValueError):
                service.resolve_ticket(tid, "answered", "不能代他人覆盖")
        finally:
            current_principal.reset(token)
        for outcome, note in [("refund_succeeded", "不允许业务动作"), ("answered", " "), ("answered", "x" * 2001)]:
            with self.assertRaises(ValueError):
                service.resolve_ticket(tid, outcome, note)

    def test_concurrent_operators_have_one_owner_and_one_final_result(self):
        tid = self.ticket()
        barrier = Barrier(4)
        def claim(i):
            token = current_principal.set(Principal(f"op{i}", "admin"))
            try:
                barrier.wait(timeout=10)
                try:
                    return service.claim_ticket(tid)["ticket"]["assigned_to"]
                except ValueError:
                    return None
            finally:
                current_principal.reset(token)
        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = [r for r in pool.map(claim, range(4)) if r]
        self.assertEqual(len(winners), 1)
        barrier = Barrier(2)
        def close(outcome):
            token = current_principal.set(Principal(winners[0], "admin"))
            try:
                barrier.wait(timeout=10)
                try:
                    return service.resolve_ticket(tid, outcome, "已完成核实")
                except ValueError:
                    return None
            finally:
                current_principal.reset(token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [r for r in pool.map(close, ["answered", "rejected"]) if r]
        self.assertEqual(len(results), 1)
        self.assertEqual(len(service.ticket_details(tid)["audit"]), 2)

    def test_write_then_failure_rolls_back_status_and_audit(self):
        tid = self.ticket()
        service.claim_ticket(tid)
        before = service.ticket_details(tid)
        execute = service.execute
        def fail_after_write(*args, **kwargs):
            execute(*args, **kwargs)
            raise ConnectionError("injected after UPDATE")
        with patch.object(service, "execute", side_effect=fail_after_write):
            with self.assertRaises(ConnectionError):
                service.resolve_ticket(tid, "answered", "结果提交中断")
        self.assertEqual(service.ticket_details(tid), before)
        self.assertTrue(service.resolve_ticket(tid, "answered", "重试完成")["success"])

    def test_customer_detail_ownership_and_service_admin_boundary(self):
        tid = self.ticket()
        for principal in [None, Principal("u001", "customer"), Principal("u009", "customer")]:
            token = current_principal.set(principal)
            try:
                if principal and principal.user_id == "u009":
                    self.assertEqual(service.ticket_details(tid)["ticket_id"], tid)
                else:
                    with self.assertRaises(PermissionError):
                        service.ticket_details(tid)
                with self.assertRaises(PermissionError):
                    service.claim_ticket(tid)
                with self.assertRaises(PermissionError):
                    service.resolve_ticket(tid, "answered", "越权结案")
            finally:
                current_principal.reset(token)

    def test_agent_ticket_saves_detached_context_through_tool_thread(self):
        from app.agent.agents.after_sales import AfterSalesAgent
        from app.agent.state import AgentState
        from app.core.schemas import RouteDecision, ToolResult
        from app.tools.ticket import create_ticket
        state = AgentState("ticket-conversation", "请核实支付异常", RouteDecision(order_id="10009", need_ticket=True),
            order=db.get_order_from_db("10009"), history=[{"role": "user", "content": str(i)} for i in range(12)],
            tool_results=[ToolResult(tool_name="policy_search", success=True,
                result=[{"chunk_id": "actual-chunk", "text": "工具实际返回的规则", "citation": "政策来源"}])])
        result = AfterSalesAgent().create_ticket(state)
        self.assertTrue(result.success)
        ticket = result.state_updates["ticket"]
        saved = service.ticket_details(ticket["ticket_id"])
        self.assertEqual(saved["context"]["history"], state.history[-8:])
        self.assertEqual(saved["context"]["policy_evidence"][0][0]["chunk_id"], "actual-chunk")
        state.history[-1]["content"] = "后来修改"
        ticket["context"]["policy_evidence"][0][0]["text"] = "后来修改"
        self.assertEqual(service.ticket_details(ticket["ticket_id"]), saved)
        standalone = create_ticket(None, "咨询", "独立新请求").result
        self.assertIsNone(standalone["context"])

    def test_keyset_pages_tied_timestamps_survive_new_inserts_and_deletion(self):
        from app.storage.transactions import execute, transaction
        def save(tid, created="2026-01-01T00:00:00", uid="u009", status="pending_human_review"):
            db.save_ticket_to_db({"ticket_id": tid, "created_at": created, "user_id": uid,
                "order_id": "10009", "issue_type": "咨询", "priority": "normal", "status": status, "user_request": tid})
        for i in range(7):
            save(f"page-{i}")
        first = service.list_tickets_page(3)
        self.assertEqual([t["ticket_id"] for t in first["data"]], ["page-6", "page-5", "page-4"])
        save("newer", "2026-01-02T00:00:00")
        with transaction():
            execute("DELETE FROM tickets WHERE ticket_id = ?", ("page-4",))
        ids = [t["ticket_id"] for t in first["data"]]
        cursor = first["next_cursor"]
        while cursor:
            page = service.list_tickets_page(3, cursor=cursor)
            ids.extend(t["ticket_id"] for t in page["data"])
            cursor = page["next_cursor"]
        self.assertEqual(ids, [f"page-{i}" for i in range(6, -1, -1)])
        self.assertEqual(service.list_tickets_page(1)["data"][0]["ticket_id"], "newer")

    def test_filters_and_customer_scope_apply_before_pagination(self):
        own = self.ticket()
        service.claim_ticket(own)
        closed = self.ticket()
        service.claim_ticket(closed)
        service.resolve_ticket(closed, "answered", "已核实")
        for i in range(25):
            db.save_ticket_to_db({"user_id": "other", "order_id": "other-order", "created_at": "2099-01-01T00:00:00",
                "issue_type": "咨询", "priority": "normal", "status": "pending_human_review", "user_request": str(i)})
        admin_cursor = service.list_tickets_page(1)["next_cursor"]
        token = current_principal.set(Principal("u009", "customer"))
        try:
            page = service.list_tickets_page(1, "open")
            self.assertEqual([t["ticket_id"] for t in page["data"]], [own])
            self.assertIsNone(page["next_cursor"])
            self.assertEqual(service.list_tickets_page(1, "resolved")["data"][0]["ticket_id"], closed)
            self.assertTrue(all(t["user_id"] == "u009" for t in service.list_tickets_page(100, cursor=admin_cursor)["data"]))
            for bad in ["***", "e30=", "W10=", "x" * 513]:
                with self.assertRaises(ValueError):
                    service.list_tickets_page(cursor=bad)
        finally:
            current_principal.reset(token)

    def test_legacy_sqlite_ticket_migration_preserves_only_explicit_owners(self):
        import sqlite3
        import json
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("CREATE TABLE refund_requests (idempotency_key TEXT)")
            connection.execute("CREATE TABLE tickets (ticket_id TEXT PRIMARY KEY, payload TEXT)")
            records = {"owned": {"user_id": "u009", "context": {"history": ["saved"]}},
                       "unowned": {"order_id": "10009"}, "invalid": {"user_id": ["u009"]}}
            for tid, payload in records.items():
                connection.execute("INSERT INTO tickets VALUES (?, ?)", (tid, json.dumps(payload)))
            db.apply_sqlite_migrations(connection)
            db.apply_sqlite_migrations(connection)
            rows = {r["ticket_id"]: dict(r) for r in connection.execute("SELECT * FROM tickets").fetchall()}
            self.assertEqual(rows["owned"]["user_id"], "u009")
            self.assertIsNone(rows["unowned"]["user_id"])
            self.assertIsNone(rows["invalid"]["user_id"])
            self.assertEqual(json.loads(rows["owned"]["payload"]), records["owned"])
