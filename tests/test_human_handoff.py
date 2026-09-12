from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Barrier
import unittest
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.agent.agents.after_sales import AfterSalesAgent
from app.agent.state import AgentState
from app.agent.response.grounded import render_grounded_reply
from app.agent.response.fallback import build_fallback_answer
from app.core.schemas import RouteDecision, ToolResult
from app.core.security import Principal, current_principal
from app.observability.tracing import start_trace
from app.services import ticket_service as service
from app.storage import database as db
from app.tools.human_review import transfer_to_human, review_context_scope


class HumanHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        token = current_principal.set(Principal("u009", "customer"))
        self.addCleanup(current_principal.reset, token)
        p = patch.dict("os.environ", {"AUTH_TOKENS": json.dumps({
            "a" * 32: {"user_id": "operator", "role": "admin"},
            "b" * 32: {"user_id": "second", "role": "admin"},
            "c" * 32: {"user_id": "u009", "role": "customer"}})})
        p.start()
        self.addCleanup(p.stop)

    def state(self):
        return AgentState("handoff-conversation", "请转人工帮我核实", RouteDecision(handoff_required=True),
            history=[{"role": "user", "content": "前一轮的诉求"}],
            trace=start_trace("请转人工帮我核实", "handoff-conversation"),
            tool_results=[ToolResult(tool_name="policy_search", success=True,
                result=[{"chunk_id": "actual-policy", "text": "本轮实际政策", "citation": "实际来源"}])])

    def create(self):
        result = AfterSalesAgent().transfer_to_human(self.state())
        self.assertTrue(result.success, result.error)
        return result.state_updates["handoff"]

    def test_orderless_handoff_is_owned_persisted_and_has_context_and_truthful_receipt(self):
        handoff = self.create()
        self.assertEqual(handoff["user_id"], "u009")
        self.assertIsNone(handoff["order_id"])
        page = service.list_tickets_page(status="open")
        self.assertEqual(page["data"][0]["ticket_id"], handoff["ticket_id"])
        self.assertEqual(handoff["context"]["policy_evidence"][0][0]["chunk_id"], "actual-policy")
        results = [ToolResult(tool_name="transfer_to_human", success=True, result=handoff)]
        for reply in [render_grounded_reply(results, []), build_fallback_answer(RouteDecision(handoff_required=True), results)]:
            self.assertIn(handoff["ticket_id"], reply)
            self.assertIn("等待客服认领", reply)
            self.assertNotIn("已由人工认领", reply)
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("mq_messages"), 0)

    def test_same_trace_concurrent_handoffs_create_once_and_new_trace_creates_new_request(self):
        state, barrier = self.state(), Barrier(4)
        def create(_):
            token = current_principal.set(Principal("u009", "customer"))
            try:
                with review_context_scope(deepcopy(state)):
                    barrier.wait(timeout=10)
                    return transfer_to_human("请人工核实", state.user_message).result
            finally:
                current_principal.reset(token)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, range(4)))
        self.assertEqual(len({r["ticket_id"] for r in results}), 1)
        self.assertEqual(self.fixture.count("tickets"), 1)
        self.create()
        self.assertEqual(self.fixture.count("tickets"), 2)

    def test_persistence_failure_rolls_back_and_does_not_claim_queue_success(self):
        original = db.save_ticket_to_db
        def fail_after_write(ticket):
            original(ticket)
            raise ConnectionError("injected after handoff insert")
        with patch.object(db, "save_ticket_to_db", side_effect=fail_after_write):
            result = AfterSalesAgent().transfer_to_human(self.state())
        self.assertFalse(result.success)
        self.assertEqual(self.fixture.count("tickets"), 0)
        self.assertNotIn("已进入人工待办", render_grounded_reply(result.tool_results, []))

    def test_handoff_order_ownership_cannot_be_bypassed_by_context(self):
        state = self.state()
        state.order = db.get_order_from_db("10001")
        with review_context_scope(state), self.assertRaises(PermissionError):
            transfer_to_human("核实", "测试")
        self.assertEqual(self.fixture.count("tickets"), 0)

    def test_reassignment_preserves_audit_and_changes_who_can_close(self):
        tid = self.create()["ticket_id"]
        token = current_principal.set(Principal("operator", "admin"))
        try:
            service.claim_ticket(tid)
            for target, expected, note in [("u009", "operator", "不能交给客户"),
                                           ("missing", "operator", "不存在"), ("second", "stale", "旧页面"),
                                           ("second", "operator", " ")]:
                with self.assertRaises(ValueError):
                    service.reassign_ticket(tid, target, expected, note)
            assigned = service.reassign_ticket(tid, "second", "operator", "已核实背景，请继续答复")
            self.assertEqual(assigned["ticket"]["assigned_to"], "second")
            self.assertTrue(service.reassign_ticket(tid, "second", "operator", "已核实背景，请继续答复")["idempotent_replay"])
            self.assertEqual(len(service.ticket_details(tid)["audit"]), 2)
            with self.assertRaises(ValueError):
                service.resolve_ticket(tid, "answered", "原认领人不能再结案")
        finally:
            current_principal.reset(token)
        token = current_principal.set(Principal("second", "admin"))
        try:
            result = service.resolve_ticket(tid, "answered", "已完成答复")
            self.assertEqual(result["ticket"]["status"], "resolved")
            with self.assertRaises(ValueError):
                service.reassign_ticket(tid, "operator", "second", "不能转派已结案工单")
        finally:
            current_principal.reset(token)

    def test_reassignment_racing_resolution_has_only_one_winner(self):
        tid = self.create()["ticket_id"]
        token = current_principal.set(Principal("operator", "admin"))
        try:
            service.claim_ticket(tid)
        finally:
            current_principal.reset(token)
        barrier = Barrier(2)
        def act(reassign):
            token = current_principal.set(Principal("operator", "admin"))
            try:
                barrier.wait(timeout=10)
                try:
                    return service.reassign_ticket(tid, "second", "operator", "交接") if reassign else service.resolve_ticket(tid, "answered", "已答复")
                except ValueError:
                    return None
            finally:
                current_principal.reset(token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [r for r in pool.map(act, [True, False]) if r]
        self.assertEqual(len(results), 1)
        self.assertEqual(len(service.ticket_details(tid)["audit"]), 2)
