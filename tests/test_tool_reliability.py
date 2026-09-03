import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, patch

from app.agent.orchestrator import AgentOrchestrator
from app.core.schemas import RouteDecision, ToolResult
from app.tools import refund as refund_tool
from app.tools.executor import execute_agent_tool, execute_tool, safe_tool_call
from app.tools.registry import (
    READ_ONLY,
    SIDE_EFFECT,
    TOOL_HANDLERS,
    TOOL_RUNTIME_POLICIES,
)


def fast_policy(tool_name: str, *, timeout_seconds: float = 0.05):
    return replace(
        TOOL_RUNTIME_POLICIES[tool_name],
        timeout_seconds=timeout_seconds,
        backoff_seconds=0,
    )


def successful(tool_name: str, result=None) -> ToolResult:
    return ToolResult(tool_name=tool_name, success=True, result=result or {"ok": True})


class ToolExecutorReliabilityTests(unittest.TestCase):
    def test_registered_tools_have_explicit_runtime_policies(self) -> None:
        self.assertEqual(set(TOOL_RUNTIME_POLICIES), set(TOOL_HANDLERS))
        self.assertTrue(
            all(
                policy.max_attempts == 1
                for policy in TOOL_RUNTIME_POLICIES.values()
                if policy.side_effect_class == SIDE_EFFECT
            )
        )

    def test_read_only_transient_error_retries_once(self) -> None:
        handler = Mock(
            side_effect=[ConnectionError("temporary"), successful("order_lookup")]
        )
        with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}):
            result = execute_tool(
                "order_lookup",
                {"order_id": "10001"},
                runtime_policy=fast_policy("order_lookup"),
            )

        self.assertTrue(result.success)
        self.assertEqual(handler.call_count, 2)

    def test_non_transient_execution_error_is_not_retried(self) -> None:
        handler = Mock(side_effect=ValueError("bad dependency response"))
        with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}):
            result = execute_tool(
                "order_lookup",
                {"order_id": "10001"},
                runtime_policy=fast_policy("order_lookup"),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolExecutionError")
        self.assertFalse(result.result["retryable"])
        self.assertEqual(handler.call_count, 1)

    def test_enforced_timeout_retries_read_only_tool_and_traces_timeout(self) -> None:
        handler = Mock(side_effect=lambda **_: time.sleep(0.05))
        trace = {"trace_id": "trace-timeout", "events": [], "timings": {}}

        with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}):
            result = execute_tool(
                "order_lookup",
                {"order_id": "10001"},
                trace=trace,
                runtime_policy=fast_policy("order_lookup", timeout_seconds=0.005),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolTimeout")
        self.assertEqual(result.result["attempt"], 2)
        self.assertEqual(handler.call_count, 2)
        self.assertEqual(
            [event["event_type"] for event in trace["events"]].count("tool_retry"),
            1,
        )
        tool_result = next(
            event for event in trace["events"] if event["event_type"] == "tool_result"
        )
        self.assertEqual(tool_result["message"]["status"], "timeout")

    def test_policy_search_exception_returns_structured_failure(self) -> None:
        handler = Mock(side_effect=RuntimeError("retriever unavailable"))
        with patch.dict(TOOL_HANDLERS, {"policy_search": handler}):
            result = execute_tool(
                "policy_search",
                {"semantic_query": "退款", "lexical_query": "退款政策"},
                runtime_policy=fast_policy("policy_search"),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolExecutionError")
        self.assertEqual(result.result["fallback_action"], "handoff_to_human")

    def test_policy_search_timeout_is_limited_and_structured(self) -> None:
        handler = Mock(side_effect=TimeoutError("embedding read timed out"))
        with patch.dict(TOOL_HANDLERS, {"policy_search": handler}):
            result = execute_tool(
                "policy_search",
                {"semantic_query": "退款", "lexical_query": "退款政策"},
                runtime_policy=fast_policy("policy_search"),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolTimeout")
        self.assertEqual(result.result["attempt"], 2)
        self.assertEqual(handler.call_count, 2)

    def test_side_effect_transient_error_is_never_retried(self) -> None:
        handler = Mock(side_effect=ConnectionError("database disconnected"))
        with patch.dict(TOOL_HANDLERS, {"refund_apply": handler}):
            result = execute_tool(
                "refund_apply",
                {"order_id": "10001", "user_request": "申请退款"},
                runtime_policy=fast_policy("refund_apply"),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolTransientError")
        self.assertFalse(result.result["retryable"])
        self.assertEqual(
            result.result["recovery_action"],
            "check_order_idempotency_state_before_any_replay",
        )
        self.assertEqual(handler.call_count, 1)

    def test_refund_timeout_does_not_replay_side_effect(self) -> None:
        handler = Mock(side_effect=lambda **_: time.sleep(0.05))
        with patch.dict(TOOL_HANDLERS, {"refund_apply": handler}):
            result = execute_tool(
                "refund_apply",
                {"order_id": "10001", "user_request": "申请退款"},
                runtime_policy=fast_policy("refund_apply", timeout_seconds=0.005),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolTimeout")
        self.assertFalse(result.result["retryable"])
        self.assertEqual(result.result["attempt"], 1)
        self.assertEqual(handler.call_count, 1)

    def test_permission_denied_does_not_enter_handler_or_retry(self) -> None:
        handler = Mock()
        with patch.dict(TOOL_HANDLERS, {"refund_apply": handler}):
            result = execute_agent_tool(
                "customer_agent",
                "refund_apply",
                {"order_id": "10001", "user_request": "申请退款"},
                runtime_policy=fast_policy("refund_apply"),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolPermissionDenied")
        self.assertEqual(result.result["attempt"], 1)
        handler.assert_not_called()

    def test_invalid_arguments_do_not_enter_handler_or_retry(self) -> None:
        handler = Mock()
        with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}):
            result = execute_tool(
                "order_lookup",
                {},
                runtime_policy=fast_policy("order_lookup"),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "InvalidToolArguments")
        self.assertEqual(result.result["attempt"], 1)
        handler.assert_not_called()

    def test_trace_keeps_runtime_fields_and_redacts_sensitive_arguments(self) -> None:
        trace = {"trace_id": "trace-redaction", "events": [], "timings": {}}
        handler = Mock(return_value=successful("order_lookup"))
        with patch.dict(TOOL_HANDLERS, {"order_lookup": handler}):
            execute_tool(
                "order_lookup",
                {"order_id": "10001", "authorization": "secret-value"},
                trace=trace,
                runtime_policy=fast_policy("order_lookup"),
            )

        tool_call = next(
            event for event in trace["events"] if event["event_type"] == "tool_call"
        )
        self.assertEqual(tool_call["message"]["arguments"]["order_id"], "10001")
        self.assertEqual(
            tool_call["message"]["arguments"]["authorization"],
            "[REDACTED]",
        )
        self.assertEqual(tool_call["message"]["side_effect_class"], READ_ONLY)

    def test_safe_tool_call_has_real_timeout_not_only_exception_handling(self) -> None:
        result = safe_tool_call(
            "local_check",
            lambda: time.sleep(0.05),
            runtime_policy=replace(
                fast_policy("order_lookup", timeout_seconds=0.005),
                max_attempts=1,
            ),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.result["error_type"], "ToolTimeout")


class RiskFailClosedTests(unittest.TestCase):
    def _run_refund_route(self, risk_handler: Mock, refund_handler: Mock):
        route = RouteDecision(
            intent="return_refund",
            action_type="execute",
            topic="refund_apply",
            order_id="10001",
            need_order=True,
            need_risk_check=True,
            need_refund_request=True,
            tool_plan=["order_lookup", "risk_check", "refund_apply"],
        )
        handlers = {
            "order_lookup": Mock(
                return_value=successful(
                    "order_lookup",
                    {"order_id": "10001", "user_id": "U-1", "amount": 100},
                )
            ),
            "risk_check": risk_handler,
            "refund_apply": refund_handler,
        }
        with patch.dict(TOOL_HANDLERS, handlers):
            return AgentOrchestrator().run_agent_loop("申请退款", route)

    def test_risk_exception_blocks_refund_apply(self) -> None:
        refund_handler = Mock()
        state = self._run_refund_route(
            Mock(side_effect=RuntimeError("risk service failed")),
            refund_handler,
        )

        self.assertTrue(state.blocked)
        self.assertEqual(state.block_reason, "risk_check_failed")
        risk_result = next(
            item for item in state.tool_results if item.tool_name == "risk_check"
        )
        self.assertTrue(risk_result.result["fail_closed"])
        refund_handler.assert_not_called()

    def test_risk_timeout_blocks_refund_apply_after_limited_retry(self) -> None:
        risk_handler = Mock(side_effect=TimeoutError("risk timed out"))
        refund_handler = Mock()
        state = self._run_refund_route(risk_handler, refund_handler)

        self.assertTrue(state.blocked)
        risk_result = next(
            item for item in state.tool_results if item.tool_name == "risk_check"
        )
        self.assertEqual(risk_result.result["error_type"], "ToolTimeout")
        self.assertTrue(risk_result.result["fail_closed"])
        self.assertEqual(risk_handler.call_count, 2)
        refund_handler.assert_not_called()


class SideEffectFailureTests(unittest.TestCase):
    def test_ticket_db_failure_is_not_reported_as_success(self) -> None:
        with patch(
            "app.tools.ticket.save_ticket_to_db",
            side_effect=ConnectionError("ticket db failed"),
        ) as save:
            result = execute_agent_tool(
                "after_sales_agent",
                "create_ticket",
                {
                    "order_id": "10001",
                    "issue_type": "refund",
                    "user_request": "请处理",
                },
                runtime_policy=fast_policy("create_ticket"),
            )

        self.assertFalse(result.success)
        self.assertFalse(result.result["retryable"])
        save.assert_called_once()

    def test_manual_review_db_failure_is_not_reported_as_success(self) -> None:
        with patch("app.tools.human_review.get_order_by_id", return_value=None), patch(
            "app.tools.human_review.save_manual_review_to_db",
            side_effect=ConnectionError("review db failed"),
        ) as save:
            result = execute_agent_tool(
                "after_sales_agent",
                "create_manual_review",
                {
                    "order_id": "10001",
                    "review_type": "refund",
                    "risk_level": "high",
                    "risk_flags": ["risk_service_failed"],
                    "user_request": "申请退款",
                },
                runtime_policy=fast_policy("create_manual_review"),
            )

        self.assertFalse(result.success)
        self.assertFalse(result.result["retryable"])
        save.assert_called_once()


class RefundRecoveryTests(unittest.TestCase):
    ORDER = {"order_id": "10001", "user_id": "U-1", "amount": 100}
    RISK = {"risk_level": "low", "risk_flags": [], "review_required": False}
    ELIGIBILITY = {
        "eligible": True,
        "reason": "eligible",
        "refund_reason": "refund_request",
        "review_required": False,
    }

    def _refund_dependencies(self):
        return patch.multiple(
            refund_tool,
            get_order_by_id=Mock(return_value=self.ORDER),
            get_customer_profile_from_db=Mock(return_value={}),
            evaluate_refund_eligibility=Mock(return_value=self.ELIGIBILITY),
            infer_refund_reason=Mock(return_value="refund_request"),
        )

    def test_db_failure_before_insert_does_not_publish_or_create_duplicate(self) -> None:
        with self._refund_dependencies(), patch.object(
            refund_tool,
            "save_refund_request_to_db",
            side_effect=RuntimeError("insert failed"),
        ) as save, patch.object(
            refund_tool,
            "get_active_refund_request_by_order_id_from_db",
            return_value=None,
        ), patch.object(refund_tool, "publish_message") as publish:
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                refund_tool._create_refund_request_unlocked(
                    "10001",
                    "申请退款",
                    self.RISK,
                )

        save.assert_called_once()
        publish.assert_not_called()

    def test_lost_insert_response_recovers_existing_refund(self) -> None:
        existing = {
            "refund_id": "R-existing",
            "order_id": "10001",
            "status": "queued",
        }
        with self._refund_dependencies(), patch.object(
            refund_tool,
            "save_refund_request_to_db",
            side_effect=ConnectionError("response lost after insert"),
        ) as save, patch.object(
            refund_tool,
            "get_active_refund_request_by_order_id_from_db",
            return_value=existing,
        ), patch.object(refund_tool, "cache_refund_idempotency") as cache, patch.object(
            refund_tool,
            "publish_message",
        ) as publish:
            result = refund_tool._create_refund_request_unlocked(
                "10001",
                "申请退款",
                self.RISK,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.result["refund_id"], "R-existing")
        self.assertTrue(result.result["idempotent_replay"])
        save.assert_called_once()
        cache.assert_called_once_with("10001", existing)
        publish.assert_not_called()

    def test_cached_refund_is_replayed_without_lock_or_write(self) -> None:
        existing = {"refund_id": "R-cached", "order_id": "10001", "status": "queued"}
        with patch.object(
            refund_tool,
            "get_refund_idempotency",
            return_value=existing,
        ), patch.object(refund_tool, "refund_distributed_lock") as lock:
            result = refund_tool.refund_apply("10001", "申请退款")

        self.assertTrue(result.success)
        self.assertTrue(result.result["idempotent_replay"])
        lock.assert_not_called()

    def test_lock_busy_returns_retry_later_without_write(self) -> None:
        @contextmanager
        def busy_lock(_order_id):
            yield False

        with patch.object(refund_tool, "get_refund_idempotency", return_value=None), patch.object(
            refund_tool,
            "refund_distributed_lock",
            side_effect=busy_lock,
        ), patch.object(
            refund_tool,
            "wait_for_refund_idempotency",
            return_value=None,
        ), patch.object(
            refund_tool,
            "get_active_refund_request_by_order_id_from_db",
            return_value=None,
        ), patch.object(refund_tool, "_create_refund_request_unlocked") as create:
            result = refund_tool.refund_apply("10001", "申请退款")

        self.assertFalse(result.success)
        self.assertEqual(result.result["fallback_action"], "retry_later")
        create.assert_not_called()

    def test_mq_failure_keeps_single_refund_row_and_skips_message_update(self) -> None:
        saved = {
            "refund_id": "R-created",
            "order_id": "10001",
            "status": "queued",
            "risk_level": "low",
        }
        with self._refund_dependencies(), patch.object(
            refund_tool,
            "save_refund_request_to_db",
            return_value=saved,
        ) as save, patch.object(
            refund_tool,
            "publish_message",
            side_effect=ConnectionError("mq failed"),
        ), patch.object(refund_tool, "update_refund_request_in_db") as update:
            with self.assertRaisesRegex(ConnectionError, "mq failed"):
                refund_tool._create_refund_request_unlocked(
                    "10001",
                    "申请退款",
                    self.RISK,
                )

        save.assert_called_once()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
