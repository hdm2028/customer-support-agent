from __future__ import annotations

import argparse
import json
from collections import Counter

from app.core.config import BASE_DIR
from scripts.eval.common import TRACE_PATH, load_traces, write_json


REPORT_PATH = BASE_DIR / "reports" / "trace_analysis.json"
SIDE_EFFECTS = ["[READ ONLY]"]


def event_payload(event: dict) -> dict:
    return event.get("data") or event.get("message") or {}


def filter_traces(traces: list[dict], trace_id: str | None, conversation_id: str | None, limit: int) -> list[dict]:
    if trace_id:
        traces = [trace for trace in traces if trace.get("trace_id") == trace_id]
    if conversation_id:
        traces = [trace for trace in traces if trace.get("conversation_id") == conversation_id]
    if limit > 0:
        traces = traces[-limit:]
    return traces


def analyze_basic(traces: list[dict]) -> dict:
    total = len(traces)
    success = sum(1 for trace in traces if trace.get("success") is True)
    durations = [
        trace.get("duration_ms")
        for trace in traces
        if isinstance(trace.get("duration_ms"), int | float)
    ]
    return {
        "total": total,
        "success_count": success,
        "failed_count": total - success,
        "success_rate": round(success / total, 4) if total else 0,
        "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
    }


def analyze_routes(traces: list[dict]) -> dict:
    intents = Counter()
    route_flags = Counter()
    for trace in traces:
        for event in trace.get("events", []):
            if event.get("event_type") != "route":
                continue
            route = event_payload(event)
            intents[route.get("intent", "unknown")] += 1
            for key in [
                "need_order",
                "need_policy",
                "need_refund_request",
                "need_risk_check",
                "need_ticket",
                "need_clarification",
                "handoff_required",
                "blocked_by_guardrail",
            ]:
                if route.get(key):
                    route_flags[key] += 1
    return {
        "intents": dict(intents),
        "flags": dict(route_flags),
    }


def analyze_agents(traces: list[dict]) -> dict:
    dispatch_counter = Counter()
    result_counter = Counter()
    for trace in traces:
        for event in trace.get("events", []):
            payload = event_payload(event)
            if event.get("event_type") == "agent_dispatch":
                dispatch_counter[payload.get("agent_key", "unknown")] += 1
            elif event.get("event_type") == "agent_result":
                result_counter[payload.get("agent_key", "unknown")] += 1
    return {
        "dispatch": dict(dispatch_counter),
        "results": dict(result_counter),
    }


def analyze_tools(traces: list[dict]) -> dict:
    calls = Counter()
    success = Counter()
    failures = Counter()
    for trace in traces:
        has_tool_result_events = any(
            event.get("event_type") == "tool_result"
            for event in trace.get("events", [])
        )
        for event in trace.get("events", []):
            payload = event_payload(event)
            if event.get("event_type") == "tool_call":
                calls[payload.get("tool_name", "unknown")] += 1
            elif event.get("event_type") == "tool_result":
                tool_name = payload.get("tool_name", "unknown")
                if payload.get("success"):
                    success[tool_name] += 1
                else:
                    failures[tool_name] += 1
            elif event.get("event_type") == "tool_failed" and not has_tool_result_events:
                failures[payload.get("tool_name", "unknown")] += 1
    return {
        "calls": dict(calls),
        "success": dict(success),
        "failures": dict(failures),
    }


def analyze_timings(traces: list[dict]) -> dict:
    values = {}
    for trace in traces:
        for step, timing in trace.get("timings", {}).items():
            duration = timing.get("duration_ms")
            if isinstance(duration, int | float):
                values.setdefault(step, []).append(duration)
    return {
        step: {
            "count": len(items),
            "avg_ms": round(sum(items) / len(items), 2),
            "max_ms": round(max(items), 2),
        }
        for step, items in sorted(values.items())
    }


def analyze_failures(traces: list[dict]) -> list[dict]:
    failures = []
    for trace in traces:
        tool_failures = []
        execution_blocks = []
        for event in trace.get("events", []):
            payload = event_payload(event)
            if event.get("event_type") == "tool_failed":
                tool_failures.append(payload)
            elif event.get("event_type") == "execution_blocked":
                execution_blocks.append(payload)
        if trace.get("success") is False or tool_failures or execution_blocks:
            failures.append(
                {
                    "trace_id": trace.get("trace_id"),
                    "conversation_id": trace.get("conversation_id"),
                    "user_message": trace.get("user_message"),
                    "tool_failures": tool_failures,
                    "execution_blocks": execution_blocks,
                }
            )
    return failures


def build_report(traces: list[dict]) -> dict:
    return {
        "side_effects": SIDE_EFFECTS,
        "trace_path": str(TRACE_PATH),
        "basic": analyze_basic(traces),
        "routes": analyze_routes(traces),
        "agents": analyze_agents(traces),
        "tools": analyze_tools(traces),
        "timings": analyze_timings(traces),
        "failures": analyze_failures(traces),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze agent trace jsonl.")
    parser.add_argument("--trace-id")
    parser.add_argument("--conversation-id")
    parser.add_argument("--limit", type=int, default=200)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    traces = filter_traces(load_traces(), args.trace_id, args.conversation_id, args.limit)
    if not traces:
        print(f"no traces found: {TRACE_PATH}")
        return

    report = build_report(traces)
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"report_path: {REPORT_PATH}")


if __name__ == "__main__":
    main()
