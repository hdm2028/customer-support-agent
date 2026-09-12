import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from app.core.schemas import ToolResult
from app.tools import executor
from app.tools.middleware import compose
from app.tools.registry import TOOL_HANDLERS, TOOL_RUNTIME_POLICIES


class ToolMiddlewareTests(unittest.TestCase):
    def test_chain_unwinds_and_short_circuit_skips_inner_layers(self):
        events = []

        def outer(call, call_next):
            events.append("outer.before")
            result = call_next(call)
            events.append("outer.after")
            return result

        def deny(call, call_next):
            events.append("deny")
            return "denied"

        terminal = Mock()
        self.assertEqual(compose((outer, deny), terminal)(None), "denied")
        self.assertEqual(events, ["outer.before", "deny", "outer.after"])
        terminal.assert_not_called()

    def test_permission_denial_never_starts_worker_and_logs_once(self):
        trace = {"events": [], "timings": {}}
        with patch.object(executor, "can_agent_use_tool", return_value=False), \
                patch.object(executor, "_run_with_timeout") as worker:
            result = executor.execute_agent_tool("unknown", "refund_apply", {}, trace=trace)
        worker.assert_not_called()
        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolPermissionDenied")
        self.assertEqual(trace["timings"], {})
        types = [e["event_type"] for e in trace["events"]]
        self.assertEqual(types.count("tool_call"), 1)
        self.assertEqual(types.count("tool_result"), 1)
        self.assertEqual(types.count("tool_failed"), 1)

    def test_three_entrypoints_share_retry_and_failure_contract(self):
        policy = replace(TOOL_RUNTIME_POLICIES["order_lookup"], backoff_seconds=0)
        results = []
        for entry in ("safe", "tool", "agent"):
            with self.subTest(entry=entry):
                handler = Mock(side_effect=ConnectionError("offline"))
                trace = {"events": [], "timings": {}}
                with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}), \
                        patch.object(executor, "can_agent_use_tool", return_value=True):
                    kwargs = {"trace": trace, "runtime_policy": policy}
                    if entry == "safe":
                        result = executor.safe_tool_call("order_lookup", handler, **kwargs)
                    elif entry == "tool":
                        result = executor.execute_tool("order_lookup", {"order_id": "10009"}, **kwargs)
                    else:
                        result = executor.execute_agent_tool("after_sales_agent", "order_lookup",
                                                             {"order_id": "10009"}, **kwargs)
                self.assertEqual(handler.call_count, policy.max_attempts)
                types = [e["event_type"] for e in trace["events"]]
                self.assertEqual(types.count("tool_result"), 1)
                self.assertEqual(types.count("tool_failed"), 1)
                self.assertEqual("tool.order_lookup" in trace["timings"], entry != "safe")
                results.append({k: result.result[k] for k in
                                ("error_type", "attempt", "max_attempts", "retryable", "fallback_action")})
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_invalid_arguments_cannot_reach_registered_handler(self):
        handler = Mock()
        with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}):
            result = executor.execute_tool("order_lookup", {})
        handler.assert_not_called()
        self.assertEqual(result.result["error_type"], "InvalidToolArguments")

    def test_missing_agent_identity_does_not_become_trusted_internal_call(self):
        with patch.object(executor, "_run_with_timeout") as worker:
            result = executor.execute_agent_tool(None, "refund_apply", {})
        worker.assert_not_called()
        self.assertEqual(result.result["error_type"], "ToolPermissionDenied")

    def test_success_does_not_inherit_previous_call_failure_metadata(self):
        trace = {"events": [], "timings": {}}
        policy = replace(TOOL_RUNTIME_POLICIES["order_lookup"], max_attempts=1, backoff_seconds=0)
        executor.safe_tool_call("order_lookup", Mock(side_effect=ValueError("bad")),
                                trace=trace, runtime_policy=policy)
        result = executor.safe_tool_call("order_lookup", lambda: ToolResult(
            tool_name="order_lookup", success=True, result={"ok": True}),
            trace=trace, runtime_policy=policy)
        self.assertTrue(result.success)
        self.assertEqual(sum(e["event_type"] == "tool_failed" for e in trace["events"]), 1)
        self.assertEqual(sum(e["event_type"] == "tool_result" for e in trace["events"]), 2)


if __name__ == "__main__":
    unittest.main()
