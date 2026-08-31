from __future__ import annotations

import argparse
import json

from app.core.config import BASE_DIR
from scripts.eval.common import FAILED_CASE_DIR, NA, REPORT_DIR, load_traces


EVALUATION_REPORT = REPORT_DIR / "evaluation_report.json"
SIDE_EFFECTS = ["[READ ONLY]"]


def load_failed_cases() -> list[dict]:
    if EVALUATION_REPORT.exists():
        report = json.loads(EVALUATION_REPORT.read_text(encoding="utf-8"))
        return report.get("failed_cases", [])

    cases = []
    if FAILED_CASE_DIR.exists():
        for path in sorted(FAILED_CASE_DIR.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in payload.get("failed_cases", []):
                cases.append({"source_file": str(path), **item})
    return cases


def select_cases(cases: list[dict], case_id: str | None, trace_id: str | None, source: str | None) -> list[dict]:
    selected = cases
    if case_id:
        selected = [item for item in selected if item.get("case_id") == case_id]
    if trace_id:
        selected = [item for item in selected if item.get("trace_id") == trace_id]
    if source:
        selected = [item for item in selected if item.get("source") == source]
    return selected


def find_trace(trace_id: str | None) -> dict | None:
    if not trace_id or trace_id == NA:
        return None
    for trace in reversed(load_traces()):
        if trace.get("trace_id") == trace_id:
            return trace
    return None


def event_payload(event: dict) -> dict:
    return event.get("data") or event.get("message") or {}


def trace_timeline(trace: dict | None) -> list[dict]:
    if not trace:
        return []

    timeline = [
        {
            "step": "User Input",
            "data": {
                "trace_id": trace.get("trace_id"),
                "conversation_id": trace.get("conversation_id"),
                "user_message": trace.get("user_message"),
            },
        }
    ]
    for event in trace.get("events", []):
        event_type = event.get("event_type")
        payload = event_payload(event)
        if event_type == "route":
            timeline.append({"step": "Router Decision", "data": payload})
        elif event_type == "agent_dispatch":
            timeline.append({"step": "Agent Dispatch", "data": payload})
        elif event_type == "tool_call":
            timeline.append({"step": "Tool Arguments", "data": payload})
        elif event_type == "tool_result":
            timeline.append({"step": "Tool Result", "data": payload})
        elif event_type == "execution_blocked":
            timeline.append({"step": "Execution Blocked", "data": payload})
        elif event_type == "agent_result":
            timeline.append({"step": "Agent Result", "data": payload})
        elif event_type == "reply":
            timeline.append({"step": "Final Result", "data": {"reply": trace.get("reply"), **payload}})

    return timeline


def build_inspection(cases: list[dict]) -> dict:
    inspections = []
    for case in cases:
        trace = find_trace(case.get("trace_id"))
        inspections.append(
            {
                "case_id": case.get("case_id"),
                "source": case.get("source", case.get("source_file", NA)),
                "failure_stage": case.get("failure_stage", NA),
                "expected": case.get("expected", NA),
                "actual": case.get("actual", NA),
                "reason": case.get("reason", NA),
                "trace_id": case.get("trace_id", NA),
                "timeline": trace_timeline(trace),
            }
        )
    return {
        "side_effects": SIDE_EFFECTS,
        "count": len(inspections),
        "items": inspections,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect failed eval cases and linked traces.")
    parser.add_argument("--case-id")
    parser.add_argument("--trace-id")
    parser.add_argument("--source")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases = select_cases(load_failed_cases(), args.case_id, args.trace_id, args.source)
    report = build_inspection(cases)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
