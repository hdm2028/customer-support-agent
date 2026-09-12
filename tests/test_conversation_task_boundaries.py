import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

import test_refund_consistency as fixtures
from app.agent.entry import workflow
from app.agent.entry.agent_core import run_customer_support_agent, stream_customer_support_agent
from app.agent.routing.conversation_context import apply_conversation_context
from app.agent.routing.pending_task import pending_message_kind
from app.agent.routing.router_v2 import route_tools_v2
from app.agent.routing.refund_withdrawal import WITHDRAWAL_TOPIC
from app.core.schemas import RouteDecision, ToolResult
from app.observability import tracing
from app.agent.routing.semantic import SemanticRoute
from app.storage import database as db
from app.tools.registry import TOOL_HANDLERS


class ConversationTaskBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        p = patch.object(tracing, "TRACE_PATH", Path(self.fixture.tmp.name) / "trace.jsonl")
        p.start()
        self.addCleanup(p.stop)

    def pending(self, cid="test-conversation", address=False):
        task = {"user_request": "帮我修改地址" if address else "我要申请退款", "slots": {},
                "required_slots": ["order_id", "new_address"] if address else ["order_id"],
                "missing_slots": ["order_id", "new_address"] if address else ["order_id"]}
        workflow.memory.set_pending_task(cid, task)
        return task

    def context(self, message, cid="test-conversation"):
        return workflow.load_context_node(workflow.build_initial_state(message, cid, False))

    def test_order_number_fills_pending_slot(self):
        self.pending()
        context = self.context("订单号是10009")
        self.assertTrue(context["used_pending_task"])
        self.assertEqual(context["slots"]["order_id"], "10009")
        self.assertIn("我要申请退款", context["effective_user_message"])

    def test_new_request_replaces_pending_intent_without_becoming_an_address(self):
        self.pending(address=True)
        context = self.context("不改地址了，我想申请退款，订单10009")
        self.assertFalse(context["used_pending_task"])
        self.assertEqual(context["required_slots"], [])
        self.assertNotIn("new_address", context["slots"])
        self.assertEqual(context["effective_user_message"], "不改地址了，我想申请退款，订单10009")

    def test_cancel_pending_does_not_call_router_or_llm_in_either_entry(self):
        def run_sync():
            self.pending("cancel-sync")
            return run_customer_support_agent("取消申请", "cancel-sync", use_llm=True)
        async def run_stream():
            self.pending("cancel-stream")
            events = [event async for event in stream_customer_support_agent("取消申请", "cancel-stream", use_llm=True)]
            return next(event["content"] for event in events if event["type"] == "done")
        with patch.object(workflow, "route_user_request", side_effect=AssertionError("router must not run")), \
             patch.object(workflow, "call_zhipu_chat", side_effect=AssertionError("LLM must not run")):
            sync = run_sync()
            streamed = asyncio.run(run_stream())
        self.assertEqual(sync["reply"], streamed["reply"])
        self.assertIn("已取消本次待补充", sync["reply"])
        self.assertEqual(sync["tool_results"], [])
        self.assertIsNone(workflow.memory.get_pending_task("cancel-sync"))
        self.assertIsNone(workflow.memory.get_pending_task("cancel-stream"))

    def test_cancel_order_is_not_confused_with_cancel_pending_request(self):
        pending = self.pending()
        self.assertEqual(pending_message_kind("订单10009帮我取消订单", pending), "replace")

    def test_accepted_refund_withdrawal_cannot_turn_into_a_new_application(self):
        # The live router previously planned refund_apply for this withdrawal.
        from app.storage.database import list_refund_requests_from_db
        before = list_refund_requests_from_db()
        async def stream(message):
            events = [event async for event in stream_customer_support_agent(message, "withdraw-stream", use_llm=True)]
            return next(event["content"] for event in events if event["type"] == "done")
        with patch("app.agent.routing.router_v2.infer_semantic_route", side_effect=AssertionError("no reclassification")), \
             patch.object(workflow, "call_zhipu_chat", side_effect=AssertionError("no generated success claim")), \
             patch("app.agent.entry.stream_runner.call_zhipu_chat_stream", side_effect=AssertionError("no stream generation")):
            sync = run_customer_support_agent("取消退款申请，订单10009", "withdraw-sync", use_llm=True)
            streamed = asyncio.run(stream("撤销订单10009的退款申请"))
            for result in (sync, streamed):
                self.assertEqual(result["route"]["topic"], WITHDRAWAL_TOPIC)
                self.assertEqual(result["tool_results"], [])
                self.assertIn("当前聊天尚未撤销申请", result["reply"])
                self.assertIn("退款申请查询与撤销", result["reply"])
        self.assertEqual(list_refund_requests_from_db(), before)

    def test_withdrawal_guidance_replaces_pending_application(self):
        self.pending("withdraw-pending")
        with patch("app.agent.routing.router_v2.infer_semantic_route", side_effect=AssertionError("no reclassification")):
            result = run_customer_support_agent("撤回退款申请", "withdraw-pending", use_llm=False)
        self.assertEqual(result["tool_results"], [])
        self.assertIsNone(workflow.memory.get_pending_task("withdraw-pending"))
        self.assertIn("尚未撤销", result["reply"])

    def test_plain_order_cancellation_still_reaches_semantic_router(self):
        with patch("app.agent.routing.router_v2.infer_semantic_route", side_effect=RuntimeError("normal router")):
            with self.assertRaisesRegex(RuntimeError, "normal router"):
                route_tools_v2("订单10009帮我取消订单")

    def test_refund_outcome_is_deterministic_in_sync_and_stream_even_with_llm_enabled(self):
        result = ToolResult(tool_name="refund_apply", success=True, result={"refund_id": "R", "status": "queued"})
        def tools(_state):
            return {"tool_results": [result], "orchestration": {}}
        async def stream():
            events = [e async for e in stream_customer_support_agent("申请退款", "refund-state-stream", use_llm=True)]
            self.assertFalse(any(e["type"] == "token" for e in events))
            return next(e["content"] for e in events if e["type"] == "done")
        with patch.object(workflow, "route_user_request", return_value=RouteDecision()), \
             patch.object(workflow, "run_orchestrated_state") as agent_state, \
             patch("app.agent.entry.stream_runner.orchestrate_agents_node", side_effect=tools), \
             patch.object(workflow, "call_zhipu_chat", side_effect=AssertionError("no generated transaction state")), \
             patch("app.agent.entry.stream_runner.call_zhipu_chat_stream", side_effect=AssertionError("no streamed transaction state")):
            agent_state.return_value.tool_results = [result]
            agent_state.return_value.agent_steps = []
            agent_state.return_value.to_summary.return_value = {}
            sync = run_customer_support_agent("申请退款", "refund-state-sync", use_llm=True)
            streamed = asyncio.run(stream())
        self.assertEqual(sync["reply"], streamed["reply"])
        self.assertIn("申请受理不代表资金已经退回", sync["reply"])

    def test_explicit_new_order_wins_over_prior_order(self):
        history = [{"role": "user", "content": "订单10001查一下物流"}]
        effective, inherited, _ = apply_conversation_context("订单10009我要退款", history, False)
        self.assertEqual(effective, "订单10009我要退款")
        self.assertFalse(inherited)

    def test_cancel_boundary_prevents_reviving_old_refund(self):
        history = [{"role": "user", "content": "我要退款"}, {"role": "user", "content": "取消申请"}]
        effective, inherited, _ = apply_conversation_context("10009", history, False)
        self.assertEqual(effective, "10009")
        self.assertFalse(inherited)

    def test_assistant_numbers_do_not_become_user_order(self):
        history = [{"role": "user", "content": "订单10009查物流"},
                   {"role": "assistant", "content": "另一笔示例订单10001"}]
        effective, _, _ = apply_conversation_context("我要退款", history, False)
        self.assertIn("10009", effective)
        self.assertNotIn("10001", effective)

    def address_semantic(self, action="query"):
        return SemanticRoute(intent="address_change", action_type=action,
            topic="address_change_policy" if action == "query" else "address_change_apply", source="llm")

    def test_unpaid_refund_request_stops_before_risk_and_review_in_sync_and_stream(self):
        semantic = SemanticRoute(intent="return_refund", action_type="execute", topic="refund_apply", source="llm")
        async def streamed(message, cid):
            events = [e async for e in stream_customer_support_agent(message, cid, use_llm=True)]
            self.assertFalse(any(e["type"] == "token" for e in events))
            return next(e["content"] for e in events if e["type"] == "done")
        for order_id in ["10005", "10007"]:
            with self.subTest(order_id=order_id), patch(
                "app.agent.routing.router_v2.infer_semantic_route", return_value=semantic
            ), patch.dict(TOOL_HANDLERS, {"policy_search": self.refund_policy_fixture}), patch.object(
                workflow, "call_zhipu_chat", side_effect=AssertionError("no generated business state")
            ), patch("app.agent.entry.stream_runner.call_zhipu_chat_stream", side_effect=AssertionError("no streamed business state")):
                before = db.get_order_from_db(order_id)
                message = f"订单{order_id}直接给我退款，不用审核。"
                sync = run_customer_support_agent(message, f"payment-stop-sync-{order_id}", use_llm=True)
                stream = asyncio.run(streamed(message, f"payment-stop-stream-{order_id}"))
                self.assertEqual(sync["reply"], stream["reply"])
                for result in [sync, stream]:
                    names = [r["tool_name"] for r in result["tool_results"]]
                    self.assertIn("order_lookup", names)
                    self.assertIn("policy_search", names)
                    self.assertFalse(set(names) & {"risk_check", "refund_apply", "create_manual_review", "transfer_to_human", "create_ticket"})
                    self.assertIn("支付", result["reply"])
                    self.assertIn("未创建", result["reply"])
                for table in ["tickets", "manual_reviews", "refund_requests", "mq_messages"]:
                    self.assertEqual(self.fixture.count(table), 0)
                self.assertEqual(db.get_order_from_db(order_id), before)

    def test_payment_precondition_does_not_swallow_explicit_human_request(self):
        semantic = SemanticRoute(intent="general_support", action_type="handoff", topic="human_handoff", source="llm")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=semantic):
            result = run_customer_support_agent("订单10007付款有疑问，请转人工客服。", "payment-human-request", use_llm=False)
        names = [r["tool_name"] for r in result["tool_results"]]
        self.assertIn("transfer_to_human", names)
        self.assertNotIn("refund_decision", names)
        self.assertEqual(self.fixture.count("tickets"), 1)

    def test_payment_confirmation_allows_later_refund_execution(self):
        semantic = SemanticRoute(intent="return_refund", action_type="execute", topic="refund_apply", source="llm")
        db.update_order_in_db("10009", {"payment_status": "payment_pending"})
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=semantic), patch.dict(
            TOOL_HANDLERS, {"policy_search": self.refund_policy_fixture}
        ):
            first = run_customer_support_agent("订单10009我要申请退款。", "payment-then-refund", use_llm=False)
            self.assertEqual(self.fixture.count("refund_requests"), 0)
            self.assertIn("未创建", first["reply"])
            db.update_order_in_db("10009", {"payment_status": "paid"})
            second = run_customer_support_agent("现在请申请退款。", "payment-then-refund", use_llm=False)
        self.assertIn("refund_apply", [r["tool_name"] for r in second["tool_results"]])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)

    def test_payment_block_is_explained_even_when_policy_service_fails(self):
        semantic = SemanticRoute(intent="return_refund", action_type="execute", topic="refund_apply", source="llm")
        def unavailable(**kwargs):
            return ToolResult(tool_name="policy_search", success=False, result="policy service unavailable")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=semantic), patch.dict(
            TOOL_HANDLERS, {"policy_search": unavailable}
        ):
            result = run_customer_support_agent("订单10005我要退款。", "payment-policy-failure", use_llm=False)
        self.assertIn("尚未支付", result["reply"])
        self.assertIn("未创建", result["reply"])
        self.assertTrue(any(r["tool_name"] == "policy_search" and not r["success"] for r in result["tool_results"]))
        self.assertEqual(self.fixture.count("manual_reviews"), 0)

    def refund_policy_fixture(self, **kwargs):
        return ToolResult(tool_name="policy_search", success=True, result=[{
            "chunk_id": "test-refund-policy", "source": "退款政策.md", "section": "退款流程",
            "citation": "退款政策.md - 退款流程", "text": "退款申请需要核实订单支付状态。", "score": .95}])

    def test_complaint_query_has_no_business_writes_even_for_high_risk_account(self):
        semantic = SemanticRoute(intent="complaint", action_type="query", topic="complaint", source="llm")
        for order_id in ["10001", "10004"]:
            with self.subTest(order_id=order_id), patch(
                "app.agent.routing.router_v2.infer_semantic_route", return_value=semantic
            ):
                before = db.get_order_from_db(order_id)
                result = run_customer_support_agent(
                    f"订单{order_id}服务体验不好，我只想反馈，暂时不要处理。", f"complaint-query-{order_id}", use_llm=False
                )
                self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["order_lookup"])
                self.assertFalse(result["route"]["manual_review_required"])
                self.assertEqual(db.get_order_from_db(order_id), before)
                for table in ["tickets", "manual_reviews", "refund_requests", "mq_messages"]:
                    self.assertEqual(self.fixture.count(table), 0)

    def test_complaint_query_does_not_create_review_in_stream(self):
        semantic = SemanticRoute(intent="complaint", action_type="query", topic="complaint", source="llm")
        async def streamed():
            events = [e async for e in stream_customer_support_agent(
                "订单10004服务体验不好，只是反馈，先不处理。", "complaint-query-stream", use_llm=False
            )]
            return next(e["content"] for e in events if e["type"] == "done")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=semantic):
            result = asyncio.run(streamed())
        self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["order_lookup"])
        self.assertEqual(self.fixture.count("manual_reviews"), 0)
        self.assertEqual(self.fixture.count("tickets"), 0)

    def test_complaint_execution_still_reaches_risk_review_and_ticket(self):
        semantic = SemanticRoute(intent="complaint", action_type="execute", topic="complaint", source="llm")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=semantic):
            result = run_customer_support_agent(
                "订单10004服务体验不好，请正式提交投诉处理。", "complaint-execute", use_llm=False
            )
        names = [r["tool_name"] for r in result["tool_results"]]
        self.assertIn("risk_check", names)
        self.assertIn("create_manual_review", names)
        self.assertIn("create_ticket", names)
        self.assertEqual(self.fixture.count("manual_reviews"), 1)
        self.assertEqual(self.fixture.count("tickets"), 1)
        self.assertEqual(self.fixture.count("refund_requests"), 0)

    def test_complaint_query_then_explicit_execution_does_not_lose_user_action(self):
        cid = "complaint-query-to-execute"
        query = SemanticRoute(intent="complaint", action_type="query", topic="complaint", source="llm")
        execute = SemanticRoute(intent="complaint", action_type="execute", topic="complaint", source="llm")
        with patch("app.agent.routing.router_v2.infer_semantic_route", side_effect=[query, execute]):
            first = run_customer_support_agent("订单10004的体验问题暂时只反馈。", cid, use_llm=False)
            self.assertEqual(first["tool_results"][0]["tool_name"], "order_lookup")
            self.assertEqual(self.fixture.count("manual_reviews"), 0)
            second = run_customer_support_agent("现在请正式提交投诉。", cid, use_llm=False)
        self.assertEqual(second["route"]["order_id"], "10004")
        self.assertTrue(second["route"]["need_risk_check"])
        self.assertEqual(self.fixture.count("manual_reviews"), 1)
        self.assertEqual(self.fixture.count("tickets"), 1)

    def policy_fixture(self, **kwargs):
        return ToolResult(tool_name="policy_search", success=True, result=[{
            "chunk_id": "address-policy", "text": "修改收货地址前应核实订单的发货状态。", "score": .95,
            "source": "policy.md", "section": "地址规则", "citation": "policy.md - 地址规则"}])

    def test_address_policy_query_without_order_does_not_collect_execution_slots(self):
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.address_semantic()), \
                patch.dict(TOOL_HANDLERS, {"policy_search": self.policy_fixture}):
            result = run_customer_support_agent("修改收货地址有什么规则？", "address-policy-only", use_llm=False)
        self.assertFalse(result["route"]["need_clarification"])
        self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["policy_search"])
        self.assertIsNone(workflow.memory.get_pending_task("address-policy-only"))
        self.assertEqual(self.fixture.count("tickets"), 0)

    def test_address_policy_query_with_order_uses_lookup_and_policy_in_sync_and_stream(self):
        async def streamed():
            events = [e async for e in stream_customer_support_agent("订单10002发货后还能修改收货地址吗？", "address-query-stream", use_llm=False)]
            return next(e["content"] for e in events if e["type"] == "done")
        before = db.get_order_from_db("10002")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.address_semantic()), \
                patch.dict(TOOL_HANDLERS, {"policy_search": self.policy_fixture}):
            sync = run_customer_support_agent("订单10002发货后还能修改收货地址吗？", "address-query-sync", use_llm=False)
            stream = asyncio.run(streamed())
        for result in [sync, stream]:
            self.assertFalse(result["route"]["need_clarification"])
            self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["order_lookup", "policy_search"])
            self.assertNotIn("提供新的收货地址", result["reply"])
        self.assertEqual(db.get_order_from_db("10002"), before)
        self.assertEqual(self.fixture.count("tickets"), 0)
        self.assertEqual(self.fixture.count("manual_reviews"), 0)

    def test_actual_address_execution_collects_order_then_address_and_keeps_house_number_out_of_order_slot(self):
        cid = "address-execution"
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.address_semantic("execute")):
            first = run_customer_support_agent("请帮我修改收货地址", cid, use_llm=False)
            self.assertEqual(first["tool_results"], [])
            self.assertEqual(workflow.memory.get_pending_task(cid)["missing_slots"], ["order_id", "new_address"])
            second = run_customer_support_agent("订单号是10009", cid, use_llm=False)
            self.assertEqual(second["tool_results"], [])
            self.assertEqual(workflow.memory.get_pending_task(cid)["missing_slots"], ["new_address"])
            context = self.context_for(cid, "新地址是测试市测试路1000号")
            self.assertEqual(context["slots"]["order_id"], "10009")
            self.assertEqual(context["slots"]["new_address"], "测试市测试路1000号")
            routed = workflow.route_node(context)
            self.assertEqual(routed["route"].order_id, "10009")
            self.assertFalse(routed["missing_slots"])
            self.assertIsNone(workflow.memory.get_pending_task(cid))
        self.assertEqual(self.fixture.count("tickets"), 0)  # Only information collection/routing above.

    def eligibility_semantic(self, action="query"):
        return SemanticRoute(intent="return_refund", action_type=action,
                             topic="refund_eligibility" if action == "query" else "refund_apply", source="llm")

    def test_eligibility_consultation_without_order_retrieves_policy_in_all_reply_paths(self):
        async def streamed(cid, use_llm):
            events = [e async for e in stream_customer_support_agent(
                "定制商品不合适能退货吗？", cid, use_llm=use_llm)]
            result = next(e["content"] for e in events if e["type"] == "done")
            messages = [e["content"] for e in events if e["type"] == "message"]
            self.assertEqual(messages, [result["reply"]])
            self.assertFalse(any(e["type"] == "token" for e in events))
            return result
        for use_llm in (False, True):
            sync_id, stream_id = f"eligibility-sync-{use_llm}", f"eligibility-stream-{use_llm}"
            with self.subTest(use_llm=use_llm), \
                 patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.eligibility_semantic()), \
                 patch.dict(TOOL_HANDLERS, {"policy_search": self.refund_policy_fixture}), \
                 patch.object(workflow, "call_zhipu_chat", side_effect=AssertionError("clarification uses original policy")), \
                 patch("app.agent.entry.stream_runner.call_zhipu_chat_stream", side_effect=AssertionError("no unverified qualification")):
                sync = run_customer_support_agent("定制商品不合适能退货吗？", sync_id, use_llm=use_llm)
                stream = asyncio.run(streamed(stream_id, use_llm))
                self.assertEqual(sync["reply"], stream["reply"])
                for result in (sync, stream):
                    self.assertTrue(result["route"]["need_clarification"])
                    self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["policy_search"])
                    self.assertIn("退款申请需要核实订单支付状态。", result["reply"])
                    self.assertIn("尚未核实具体订单的退款资格", result["reply"])
                    self.assertIn("请提供订单号", result["reply"])
                for cid in (sync_id, stream_id):
                    task = workflow.memory.get_pending_task(cid)
                    self.assertEqual(task["missing_slots"], ["order_id"])
                    self.assertEqual(task["user_request"], "定制商品不合适能退货吗？")
        for table in ("refund_requests", "mq_messages", "manual_reviews", "tickets"):
            self.assertEqual(self.fixture.count(table), 0)

    def test_eligibility_order_followup_preserves_consultation_then_requires_explicit_execution(self):
        cid = "eligibility-to-apply"
        with patch("app.agent.routing.router_v2.infer_semantic_route", side_effect=[
                self.eligibility_semantic(), self.eligibility_semantic(), self.eligibility_semantic("execute")]) as semantic, \
             patch.dict(TOOL_HANDLERS, {"policy_search": self.refund_policy_fixture}):
            first = run_customer_support_agent("未发货商品能否申请退款？", cid, use_llm=False)
            self.assertEqual([r["tool_name"] for r in first["tool_results"]], ["policy_search"])
            second = run_customer_support_agent("订单号是10009", cid, use_llm=False)
            self.assertIn("未发货商品能否申请退款？", semantic.call_args.args[0])
            self.assertEqual([r["tool_name"] for r in second["tool_results"]], ["order_lookup", "policy_search"])
            self.assertNotIn("请提供订单号", second["reply"])
            self.assertEqual(self.fixture.count("refund_requests"), 0)
            third = run_customer_support_agent("现在帮我提交退款申请", cid, use_llm=False)
        self.assertEqual(third["route"]["order_id"], "10009")
        self.assertIn("refund_apply", [r["tool_name"] for r in third["tool_results"]])
        self.assertEqual(self.fixture.count("refund_requests"), 1)
        self.assertEqual(self.fixture.count("mq_messages"), 1)
        self.assertNotIn("退款已完成", third["reply"])

    def test_eligibility_question_replaces_pending_execution_and_number_remains_query(self):
        cid = "eligibility-replaces-apply"
        self.pending(cid)
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.eligibility_semantic()), \
             patch.dict(TOOL_HANDLERS, {"policy_search": self.refund_policy_fixture}):
            first = run_customer_support_agent("先问一下，未发货商品能退款吗？", cid, use_llm=False)
            self.assertEqual([r["tool_name"] for r in first["tool_results"]], ["policy_search"])
            self.assertEqual(workflow.memory.get_pending_task(cid)["user_request"], "先问一下，未发货商品能退款吗？")
            second = run_customer_support_agent("10009", cid, use_llm=False)
        self.assertEqual(second["route"]["action_type"], "query")
        self.assertEqual([r["tool_name"] for r in second["tool_results"]], ["order_lookup", "policy_search"])
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("tickets"), 0)

    def test_eligibility_consultation_does_not_remove_order_requirement_for_execution(self):
        cid = "eligibility-missing-order-execute"
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.eligibility_semantic("execute")):
            result = run_customer_support_agent("帮我提交退款申请", cid, use_llm=False)
        self.assertTrue(result["route"]["need_clarification"])
        self.assertEqual(result["tool_results"], [])
        self.assertEqual(workflow.memory.get_pending_task(cid)["missing_slots"], ["order_id"])
        self.assertEqual(self.fixture.count("refund_requests"), 0)

    def test_eligibility_policy_failure_reports_missing_evidence_without_writing(self):
        def unavailable(**kwargs):
            return ToolResult(tool_name="policy_search", success=False, result="policy unavailable")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.eligibility_semantic()), \
             patch.dict(TOOL_HANDLERS, {"policy_search": unavailable}), \
             patch.object(workflow, "call_zhipu_chat", side_effect=AssertionError("must not invent policy")):
            result = run_customer_support_agent("什么条件可以退款？", "eligibility-policy-down", use_llm=True)
        self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["policy_search"])
        self.assertFalse(result["tool_results"][0]["success"])
        self.assertIn("政策", result["reply"])
        self.assertNotIn("本轮可核实的政策内容", result["reply"])
        self.assertEqual(self.fixture.count("refund_requests"), 0)
        self.assertEqual(self.fixture.count("manual_reviews"), 0)

    def test_eligibility_pending_keeps_issue_without_business_keywords_for_order_followup(self):
        cid = "eligibility-slot-formats"
        question = "买回来五天了，现在还能退吗？"
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.eligibility_semantic()), \
             patch.dict(TOOL_HANDLERS, {"policy_search": self.refund_policy_fixture}):
            result = run_customer_support_agent(question, cid, use_llm=False)
        self.assertEqual([r["tool_name"] for r in result["tool_results"]], ["policy_search"])
        pending = workflow.memory.get_pending_task(cid)
        for message in ("10009", "订单10009", "订单号是10009", "订单号：10009。"):
            with self.subTest(message=message):
                context = self.context_for(cid, message)
                self.assertTrue(context["used_pending_task"])
                self.assertIn(question, context["effective_user_message"])
        for message in ("取消10009", "订单10009帮我申请退款", "订单10009现在是什么状态"):
            with self.subTest(message=message):
                self.assertEqual(pending_message_kind(message, pending), "replace")

    def context_for(self, cid, message):
        state = workflow.build_initial_state(message, cid, False)
        state.update(workflow.load_context_node(state))
        return state

    def test_pending_address_question_reaches_router_without_becoming_an_address(self):
        cid = "address-question-after-execute"
        self.pending(cid, address=True)
        context = self.context_for(cid, "修改地址是否需要额外费用？")
        self.assertFalse(context["used_pending_task"])
        self.assertEqual(context["effective_user_message"], "修改地址是否需要额外费用？")
        self.assertNotIn("new_address", context["slots"])
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.address_semantic()):
            routed = workflow.route_node(context)
        self.assertFalse(routed["route"].need_clarification)
        self.assertEqual(routed["required_slots"], [])
        self.assertIsNone(workflow.memory.get_pending_task(cid))

    def test_explicit_order_switch_replaces_pending_address_task(self):
        cid = "address-order-switch"
        workflow.memory.set_pending_task(cid, {"user_request": "修改订单10001的地址", "slots": {"order_id": "10001"},
            "required_slots": ["order_id", "new_address"], "missing_slots": ["new_address"]})
        context = self.context_for(cid, "改为修改订单10009的地址")
        self.assertFalse(context["used_pending_task"])
        self.assertNotIn("10001", context["effective_user_message"])
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.address_semantic("execute")):
            routed = workflow.route_node(context)
        self.assertEqual(routed["route"].order_id, "10009")
        self.assertEqual(routed["missing_slots"], ["new_address"])
        self.assertEqual(workflow.memory.get_pending_task(cid)["slots"]["order_id"], "10009")

    def test_execute_after_policy_consultation_collects_address_but_inherits_known_order(self):
        cid = "address-consult-to-execute"
        workflow.memory.append(cid, "user", "订单10009可以修改地址吗？")
        workflow.memory.append(cid, "assistant", "需先核实订单。")
        context = self.context_for(cid, "帮我修改地址")
        with patch("app.agent.routing.router_v2.infer_semantic_route", return_value=self.address_semantic("execute")):
            routed = workflow.route_node(context)
        self.assertEqual(routed["route"].order_id, "10009")
        self.assertEqual(routed["slots"]["order_id"], "10009")
        self.assertEqual(routed["missing_slots"], ["new_address"])


if __name__ == "__main__":
    unittest.main()
