import asyncio
import json
import unittest
from unittest.mock import patch

from app.agent.entry.stream_runner import stream_llm_reply
from app.agent.response.grounded import checked_reply, policy_excerpts, render_grounded_reply, validate_selection
from app.agent.response.prompt_builder import build_model_messages
from app.core.schemas import ToolResult
from app.observability.tracing import start_trace


def policy():
    return ToolResult(tool_name="policy_search", success=True, result=[{
        "chunk_id": "chunk-one", "source": "规则.md", "citation": "规则.md - 退款条件", "section": "退款条件",
        "start_char": 25, "text": "已完成。\n\n## 退款条件\n\n只有符合条件后，才能创建退款申请。\n\n进入 MQ 队列，支持幂等处理。\n\n如果结果尚未确认，应先查询实际状态，不得重复执行退款。\n\n最后一句已截断"}])


class GroundedReplyTests(unittest.TestCase):
    def test_complete_conditions_are_kept_and_cut_fragments_are_not_offered(self):
        excerpts = policy_excerpts([policy()])
        self.assertEqual(len(excerpts), 2)
        self.assertEqual(excerpts[0]["text"], "只有符合条件后，才能创建退款申请。")
        self.assertIn("不得重复执行", excerpts[1]["text"])
        self.assertFalse(any("MQ" in e["text"] or e["text"] == "已完成。" for e in excerpts))

    def test_model_can_select_but_cannot_write_claims_or_citations(self):
        results = [policy()]
        selected = policy_excerpts(results)[0]
        reply, check = checked_reply(json.dumps({"evidence_ids": [selected["id"]]}), results)
        self.assertTrue(check["passed"])
        self.assertIn(selected["text"], reply)
        self.assertIn("规则.md - 退款条件", reply)
        for invalid in ('已为您退款', '{"evidence_ids":["invented"]}',
                        '{"evidence_ids":[],"status":"已退款"}', '{"evidence_ids":[true]}'):
            reply, check = checked_reply(invalid, results)
            self.assertFalse(check["passed"])
            self.assertNotIn("已为您退款", reply)
            self.assertNotIn("已退款", reply)
            self.assertIn("未通过证据核对", reply)

    def test_order_fact_is_not_interpreted_as_elapsed_time_or_policy(self):
        order = ToolResult(tool_name="order_lookup", success=True, result={"order_id": "O", "product_name": "定制商品",
            "order_status": "已签收", "return_window_days": 0, "notes": "定制商品通常不支持七天无理由，质量问题除外。"})
        reply = render_grounded_reply([order], [])
        self.assertIn("订单备注记录", reply)
        self.assertIn("尚未取得", reply)
        self.assertNotIn("超过", reply)
        self.assertNotIn("已为", reply)

    def test_unknown_reference_is_removed_without_losing_verified_content(self):
        results = [policy()]
        valid = policy_excerpts(results)[0]
        reply, check = checked_reply(json.dumps({"evidence_ids": [valid["id"], "invented"]}), results)
        self.assertFalse(check["passed"])
        self.assertTrue(check["partial"])
        self.assertIn(valid["text"], reply)
        self.assertNotIn("invented", reply)
        self.assertIn("部分内容尚未核实", reply)

    def test_a_complete_valid_selection_is_not_rejected_by_an_arbitrary_excerpt_cap(self):
        excerpts = [{"id": f"p{i}", "text": f"完整规则 {i}。"} for i in range(12)]
        raw = json.dumps({"evidence_ids": [e["id"] for e in excerpts]})
        self.assertEqual(validate_selection(raw, excerpts), excerpts)

    def test_list_introduction_and_negative_section_scope_are_preserved(self):
        result = policy()
        result.result[0]["text"] = "## 不支持直接处理的情况\n\n以下情况需要核实：\n\n- 信息不完整；\n- 存在争议。\n\n核实后再决定下一步。"
        excerpts = policy_excerpts([result])
        self.assertEqual(excerpts[0]["text"], "以下情况需要核实：\n\n- 信息不完整；\n- 存在争议。")
        self.assertIn(excerpts[0]["text"], result.result[0]["text"])
        reply = render_grounded_reply([result], excerpts)
        self.assertIn("不支持直接处理的情况", reply)
        self.assertIn("以下情况需要核实", reply)

    def test_original_evidence_still_reaches_model_context(self):
        result = policy()
        messages = build_model_messages("能退款吗？", [], [result])
        self.assertIn(result.result[0]["text"], messages[-1]["content"])
        self.assertIn("evidence_ids", messages[-1]["content"])

    def test_stream_buffers_unvalidated_output_and_never_exposes_generated_claim(self):
        async def run():
            state = {"tool_results": [policy()], "model_messages": [], "trace": start_trace("政策", "stream-test")}
            with patch("app.agent.entry.stream_runner.call_zhipu_chat_stream", return_value=iter(['已为您', '退款'])):
                events = [event async for event in stream_llm_reply(state, "stream-test")]
            return events, state
        events, state = asyncio.run(run())
        self.assertFalse(any(e["type"] == "token" for e in events))
        self.assertFalse(any("已为您退款" in str(e.get("content")) for e in events))
        self.assertEqual(state["reply_mode"], "grounded_reply_rejected")
        self.assertTrue(any(e["type"] == "message" for e in events))

    def test_valid_stream_and_sync_render_the_same_verified_evidence(self):
        results = [policy()]
        raw = json.dumps({"evidence_ids": [policy_excerpts(results)[1]["id"]]})
        sync, _ = checked_reply(raw, results)
        async def run():
            state = {"tool_results": results, "model_messages": [], "trace": start_trace("政策", "stream-valid")}
            with patch("app.agent.entry.stream_runner.call_zhipu_chat_stream", return_value=iter([raw[:8], raw[8:]])):
                events = [event async for event in stream_llm_reply(state, "stream-valid")]
            return state, events
        state, events = asyncio.run(run())
        self.assertEqual(state["reply"], sync)
        self.assertEqual(next(e["content"] for e in events if e["type"] == "message"), sync)


if __name__ == "__main__":
    unittest.main()
