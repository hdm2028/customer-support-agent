import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.llm.llm_client import call_zhipu_chat, call_zhipu_chat_stream
from app.observability.tracing import start_trace, finish_trace, timed_step
from app.observability.operations import operational_summary
from app.observability.readiness import readiness_report
from app.storage import database as db
from app.storage.transactions import transaction, execute


MODEL = SimpleNamespace(has_llm_key=True, zhipu_model="test-model", zhipu_base_url="https://provider.invalid/chat",
                        zhipu_api_key="test-only", llm_timeout_seconds=1)
USAGE = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}


class UsageTests(unittest.TestCase):
    def test_sync_and_stream_provider_usage_accumulate_once_and_missing_stays_unknown(self):
        trace = start_trace("test")
        response = {"choices": [{"message": {"content": "answer"}}], "usage": USAGE}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            self.assertEqual(timed_step(trace, "route", lambda: call_zhipu_chat([], MODEL)), "answer")
        chunks = [{"choices": [{"delta": {"content": "reply"}}], "usage": USAGE},
                  {"choices": [], "usage": USAGE}]
        wire = "".join("data: " + json.dumps(c) + "\n" for c in chunks) + "data: [DONE]\n"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(wire.encode())):
            self.assertEqual("".join(call_zhipu_chat_stream([], MODEL, usage_trace=trace)), "reply")
        response.pop("usage")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            timed_step(trace, "reply", lambda: call_zhipu_chat([], MODEL))
        finish_trace(trace, "reply", True)
        actual = trace["token_usage"]["provider_reported"]
        self.assertEqual(actual["tokens"]["total_tokens"], 30)
        self.assertEqual((actual["reported_calls"], actual["unreported_calls"]), (2, 1))
        self.assertFalse(actual["complete"])
        self.assertNotIn("test-only", json.dumps(trace))
        self.assertNotIn("_start_perf", json.dumps(trace))

    def test_failed_call_and_abandoned_stream_do_not_report_zero_charge(self):
        trace = start_trace("test")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("test")):
            with self.assertRaises(TimeoutError):
                timed_step(trace, "route", lambda: call_zhipu_chat([], MODEL))
        wire = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
        with patch("urllib.request.urlopen", return_value=io.BytesIO(wire)):
            stream = call_zhipu_chat_stream([], MODEL, usage_trace=trace)
            next(stream)
            stream.close()
        finish_trace(trace, "", False)
        usage = trace["token_usage"]
        self.assertIsNone(usage["provider_reported"]["tokens"])
        self.assertTrue(all(not call["success"] for call in usage["llm_calls"]))


class OperationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated database fixture, including optional MySQL schemas.
        from tests.test_refund_consistency import RefundConsistencyTests
        RefundConsistencyTests.setUp(self)

    def test_persisted_metrics_and_queue_ages_trigger_aggregate_alerts_without_consuming(self):
        for index in range(20):
            trace = start_trace("private user context")
            trace.update(success=True, duration_ms=20000)
            trace.pop("_start_perf")
            trace["events"].append({"event_type": "tool_result", "message": {
                "tool_name": "order_lookup", "success": False,
                "error": {"error_type": "ToolTimeout" if index < 10 else "InvalidToolArguments"}}})
            db.save_agent_metric_to_db(trace)
        from app.mq.queue import publish_message
        for status in ("pending", "processing", "dead_letter"):
            message = publish_message("refund.created", {"refund_id": "test"})
            with transaction():
                execute("UPDATE mq_messages SET status = ?, created_at = ?, updated_at = ? WHERE message_id = ?",
                        (status, "2000-01-01T00:00:00", "2000-01-01T00:00:00", message["message_id"]))
        summary = operational_summary()
        codes = {a["code"] for a in summary["alerts"]}
        self.assertTrue({"request_latency_high", "tool_failure_rate_high", "mq_oldest_waiting",
                         "mq_lease_expired", "mq_dead_letter"}.issubset(codes))
        self.assertEqual(summary["tools"]["failure_kinds"], {"dependency_or_timeout": 10, "business_or_logic": 10})
        self.assertEqual(summary["queue"]["counts"]["processing"], 1)
        self.assertNotIn("private user context", json.dumps(summary))
        self.assertTrue(operational_summary(sample_limit=2)["truncated"])

    def test_empty_metrics_are_no_samples_not_perfect_accuracy(self):
        summary = operational_summary()
        self.assertIsNone(summary["requests"]["p95_ms"])
        self.assertIsNone(summary["tools"]["failure_rate"])
        self.assertIsNone(summary["llm_usage"]["provider_reported_tokens"])

    def test_health_probes_real_sqlite_and_does_not_create_missing_database(self):
        if db.using_mysql_backend():
            self.skipTest("read-only missing-file check applies to SQLite")
        self.assertTrue(db.database_health()["reachable"])
        missing = Path(self.tmp.name) / "missing.db"
        with patch.object(db, "DB_PATH", missing):
            self.assertFalse(db.database_health()["reachable"])
        self.assertFalse(missing.exists())

    def test_payment_and_backlog_alerts_and_cli_exit_status(self):
        db.save_refund_request_to_db({"order_id": "10009", "user_id": "u009", "amount": 1,
                                      "reason": "refund_request", "status": "refund_unknown", "risk_level": "low"})
        with transaction():
            execute("UPDATE refund_requests SET updated_at = '2000-01-01T00:00:00'")
        from app.mq.queue import publish_message
        publish_message("refund.created", {"refund_id": "fixture"})
        from scripts.observability.check_operations import main
        with patch.dict("os.environ", {"OBS_MQ_BACKLOG": "1"}), \
             patch("sys.argv", ["check_operations"]), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(), 1)
            summary = json.loads(output.getvalue())
        self.assertEqual(summary["stale_payment_count"], 1)
        self.assertIn("mq_backlog", {a["code"] for a in summary["alerts"]})
        self.assertIn("payment_reconciliation_overdue", {a["code"] for a in summary["alerts"]})

    def test_configured_redis_fallback_and_unavailable_database_are_not_ready(self):
        with patch("app.observability.readiness.get_settings", return_value=SimpleNamespace(
                redis_url="redis://configured", has_llm_key=True, database_backend="sqlite",
                mq_backend="database", rag_embedding_provider="local")), \
             patch("app.observability.readiness.database_health", return_value={"reachable": False}), \
             patch("app.observability.readiness.cache_health", return_value={"reachable": True, "backend": "memory"}), \
             patch("app.observability.readiness.get_rag_index_manager") as manager, \
             patch("app.observability.readiness.authenticate", side_effect=PermissionError):
            manager.return_value.get_active_index.return_value = SimpleNamespace(chunks=["exists"])
            report = readiness_report()
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["database"])
        self.assertFalse(report["checks"]["cache"])


if __name__ == "__main__":
    unittest.main()
