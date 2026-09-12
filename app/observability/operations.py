"""Read-only operational summaries; no messages or payment tasks are executed."""
from collections import Counter
from datetime import datetime, timedelta
import json
import math
import os
import sqlite3

from app.storage import database as db


def read_rows(sql, params=()):
    if db.using_mysql_backend():
        from app.storage.mysql_database import get_mysql_connection
        connection = get_mysql_connection()
        sql = sql.replace("?", "%s")
    else:
        connection = sqlite3.connect(db.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
    finally:
        connection.close()


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize_traces(traces):
    durations, tool_durations = [], []
    tools, failed, kinds = Counter(), Counter(), Counter()
    reported_tokens = Counter()
    llm_calls = reported_calls = legacy_usage_unknown = 0
    for trace in traces:
        duration = trace.get("duration_ms")
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            durations.append(duration)
        for event in trace.get("events", []):
            data = event.get("message") or event.get("data") or {}
            if event.get("event_type") == "tool_result":
                tool = data.get("tool_name", "unknown")
                tools[tool] += 1
                if data.get("success") is False:
                    failed[tool] += 1
                    error = (data.get("error") or data.get("result") or {})
                    error_type = error.get("error_type") if isinstance(error, dict) else None
                    kind = "dependency_or_timeout" if error_type in {"ToolTransientError", "ToolTimeout"} else "business_or_logic"
                    kinds[kind] += 1
            elif event.get("event_type") == "timing" and str(data.get("step", "")).startswith("tool."):
                elapsed = data.get("duration_ms")
                if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                    tool_durations.append(elapsed)
        usage = trace.get("token_usage", {})
        if "provider_reported" not in usage:
            legacy_usage_unknown += 1
        calls = usage.get("llm_calls", [])
        llm_calls += len(calls)
        for call in calls:
            if call.get("provider_usage") is not None:
                reported_calls += 1
                reported_tokens.update(call["provider_usage"])
    count = sum(tools.values())
    failure_count = sum(failed.values())
    return {
        "requests": {"completed_trace_count": len(traces),
                     "execution_failed_count": sum(t.get("success") is False for t in traces),
                     "duration_samples": len(durations), "p50_ms": percentile(durations, .5),
                     "p95_ms": percentile(durations, .95), "max_ms": max(durations) if durations else None,
                     "scope": "completed_agent_workflows_not_answer_quality_or_all_http_requests"},
        "tools": {"result_count": count, "failed_count": failure_count,
                  "failure_rate": failure_count / count if count else None,
                  "failure_kinds": dict(kinds), "results_by_tool": dict(tools),
                  "failures_by_tool": dict(failed), "p95_attempt_ms": percentile(tool_durations, .95)},
        "llm_usage": {"observed_calls": llm_calls, "reported_calls": reported_calls,
                      "unreported_calls": llm_calls - reported_calls,
                      "traces_without_usage_instrumentation": legacy_usage_unknown,
                      "provider_reported_tokens": dict(reported_tokens) if reported_calls else None,
                      "scope": "reported_chat_usage_only_excluding_embeddings_and_local_estimates"},
    }


def thresholds():
    defaults = {"REQUEST_P95_MS": 15000, "TOOL_FAILURE_RATE": .1, "MIN_SAMPLES": 20,
                "MQ_BACKLOG": 100, "MQ_AGE_SECONDS": 300, "PAYMENT_STALE_SECONDS": 900}
    result = {key.lower(): float(os.getenv("OBS_" + key, default)) for key, default in defaults.items()}
    if any(not math.isfinite(v) or v <= 0 for v in result.values()) or result["tool_failure_rate"] > 1:
        raise ValueError("Invalid OBS_* threshold configuration")
    return result


def seconds_since(value, now):
    if value is None:
        return None
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return max(0, (now - stamp).total_seconds())


def operational_summary(window_seconds=900, sample_limit=10000):
    if not 1 <= window_seconds <= 86400 or not 1 <= sample_limit <= 10000:
        raise ValueError("Invalid observation window or sample limit")
    now = datetime.now()
    limits = thresholds()
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")
    # All payloads stay on the server; the response contains aggregates only.
    rows = read_rows("SELECT payload FROM agent_metrics WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                     (cutoff, sample_limit + 1))
    truncated = len(rows) > sample_limit
    traces = [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
              for r in rows[:sample_limit]]
    summary = summarize_traces(traces)
    queue = read_rows("SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest FROM mq_messages GROUP BY status")
    counts = {row["status"]: row["count"] for row in queue}
    waiting = [row for row in queue if row["status"] in {"pending", "failed"}]
    oldest_age = max((seconds_since(r["oldest"], now) for r in waiting), default=0)
    lease_cutoff = (now - timedelta(seconds=max(1, int(os.getenv("MQ_LEASE_SECONDS", "120"))))).isoformat(timespec="seconds")
    overdue = read_rows("SELECT COUNT(*) AS count FROM mq_messages WHERE status = 'processing' AND updated_at <= ?", (lease_cutoff,))[0]["count"]
    payment_cutoff = (now - timedelta(seconds=limits["payment_stale_seconds"])).isoformat(timespec="seconds")
    payments = read_rows("SELECT COUNT(*) AS count FROM refund_requests WHERE status IN ('payment_submitting', 'refund_unknown') AND updated_at <= ?", (payment_cutoff,))[0]["count"]
    summary.update(observed_at=now.isoformat(timespec="seconds"), window_seconds=window_seconds,
                   sample_limit=sample_limit, truncated=truncated, thresholds=limits,
                   queue={"counts": counts, "waiting_count": sum(r["count"] for r in waiting),
                          "oldest_waiting_seconds": oldest_age, "expired_processing_count": overdue},
                   stale_payment_count=payments, alerts=[])
    def alert(code, value, threshold):
        summary["alerts"].append({"code": code, "value": value, "threshold": threshold})
    requests = summary["requests"]
    if requests["duration_samples"] >= limits["min_samples"] and requests["p95_ms"] >= limits["request_p95_ms"]:
        alert("request_latency_high", requests["p95_ms"], limits["request_p95_ms"])
    tools = summary["tools"]
    if tools["result_count"] >= limits["min_samples"] and tools["failure_rate"] >= limits["tool_failure_rate"]:
        alert("tool_failure_rate_high", tools["failure_rate"], limits["tool_failure_rate"])
    for code, value, threshold in (
        ("mq_backlog", summary["queue"]["waiting_count"], limits["mq_backlog"]),
        ("mq_oldest_waiting", oldest_age, limits["mq_age_seconds"]),
        ("mq_lease_expired", overdue, 1), ("mq_dead_letter", counts.get("dead_letter", 0), 1),
        ("payment_reconciliation_overdue", payments, 1),
    ):
        if value >= threshold:
            alert(code, value, threshold)
    if truncated:
        alert("trace_sample_truncated", len(rows), sample_limit)
    return summary
