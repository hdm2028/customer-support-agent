from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import BASE_DIR
from app.mq.queue import REFUND_CREATED_TOPIC, list_messages
from app.storage.database import (
    list_refund_requests_from_db,
)


REPORT_DIR = BASE_DIR / "reports"
FAILED_CASE_DIR = REPORT_DIR / "failed_cases"
TRACE_PATH = BASE_DIR / "data" / "traces" / "agent_trace.jsonl"
NA = "N/A"
DANGEROUS_TOOLS = {"refund_apply"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def save_report(name: str, report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report["report_name"] = name
    if not report.get("created_at"):
        report["created_at"] = now_iso()
    report_path = REPORT_DIR / f"{name}.json"
    write_json(report_path, report)

    failed_cases = report.get("failed_cases", [])
    if failed_cases:
        write_json(FAILED_CASE_DIR / f"{name}_failed_cases.json", {"failed_cases": failed_cases})

    return report_path


def build_skipped_report(
    reason: str,
    side_effects: list[str] | None = None,
    dataset: str | None = None,
    dataset_cases: int | str | None = None,
) -> dict:
    report = {
        "skipped": True,
        "skip_reason": reason,
        "side_effects": side_effects or [],
        "total_cases": 0,
        "passed_count": 0,
        "failed_count": 0,
        "failed_cases": [],
        "results": [],
    }
    if dataset:
        report["dataset"] = dataset
    if dataset_cases is not None:
        report["dataset_cases"] = dataset_cases
    return report


def rate(numerator: int, denominator: int) -> float | str:
    if denominator <= 0:
        return NA
    return round(numerator / denominator, 4)


def average(values: list[float]) -> float | str:
    if not values:
        return NA
    return round(sum(values) / len(values), 4)


def status_from_report(report: dict) -> str:
    if report.get("skipped"):
        return "SKIPPED"
    if report.get("failed_count", 0):
        return "FAIL"
    return "PASS"


def result_to_dict(item: Any) -> dict:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if hasattr(item, "dict"):
        return item.dict()
    return {"value": item}


def get_tool_results(result: dict) -> list[dict]:
    return [result_to_dict(item) for item in result.get("tool_results", [])]


def get_tool_names(result: dict) -> list[str]:
    return [
        item.get("tool_name")
        for item in get_tool_results(result)
        if item.get("tool_name")
    ]


def find_tool_result(result: dict, tool_name: str) -> dict | None:
    for item in get_tool_results(result):
        if item.get("tool_name") == tool_name:
            return item
    return None


def find_all_tool_results(result: dict, tool_name: str) -> list[dict]:
    return [
        item
        for item in get_tool_results(result)
        if item.get("tool_name") == tool_name
    ]


def tool_arguments_from_trace(trace: dict | None) -> list[dict]:
    if not trace:
        return []

    calls = []
    for event in trace.get("events", []):
        if event.get("event_type") != "tool_call":
            continue
        payload = event.get("data") or event.get("message") or {}
        calls.append(
            {
                "tool_name": payload.get("tool_name"),
                "arguments": payload.get("arguments", {}),
            }
        )
    return calls


def load_traces() -> list[dict]:
    if not TRACE_PATH.exists():
        return []

    traces = []
    for line in TRACE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            traces.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return traces


def latest_trace_for_conversation(conversation_id: str | None) -> dict | None:
    if not conversation_id:
        return None

    matched = [
        trace
        for trace in load_traces()
        if trace.get("conversation_id") == conversation_id
    ]
    return matched[-1] if matched else None


def new_conversation_id(prefix: str, case_id: str) -> str:
    return f"{prefix}-{case_id}-{uuid4().hex}"


def ordered_subsequence(actual: list[str], expected: list[str]) -> bool:
    if not expected:
        return True

    cursor = 0
    for item in actual:
        if item == expected[cursor]:
            cursor += 1
            if cursor == len(expected):
                return True
    return False


def precision_recall_f1_by_label(rows: list[dict], expected_key: str, actual_key: str) -> dict:
    labels = sorted({
        label
        for row in rows
        for label in (row.get(expected_key, []) + row.get(actual_key, []))
    })
    per_label = {}
    precision_values = []
    recall_values = []
    f1_values = []

    for label in labels:
        tp = fp = fn = 0
        for row in rows:
            expected = set(row.get(expected_key, []))
            actual = set(row.get(actual_key, []))
            if label in expected and label in actual:
                tp += 1
            elif label not in expected and label in actual:
                fp += 1
            elif label in expected and label not in actual:
                fn += 1

        precision = rate(tp, tp + fp)
        recall = rate(tp, tp + fn)
        if isinstance(precision, float):
            precision_values.append(precision)
        if isinstance(recall, float):
            recall_values.append(recall)
        if isinstance(precision, float) and isinstance(recall, float) and precision + recall > 0:
            f1: float | str = round(2 * precision * recall / (precision + recall), 4)
            f1_values.append(f1)
        else:
            f1 = NA
        per_label[label] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    return {
        "per_label": per_label,
        "macro_precision": average(precision_values),
        "macro_recall": average(recall_values),
        "macro_f1": average(f1_values),
    }


def order_refunds(order_id: str, limit: int = 1000) -> list[dict]:
    return [
        refund
        for refund in list_refund_requests_from_db(limit=limit)
        if str(refund.get("order_id")) == str(order_id)
    ]


def active_order_refunds(order_id: str, limit: int = 1000) -> list[dict]:
    inactive_statuses = {"failed", "rejected", "cancelled", "canceled"}
    return [
        refund
        for refund in order_refunds(order_id, limit=limit)
        if str(refund.get("status", "")).lower() not in inactive_statuses
    ]


def order_refund_messages(order_id: str, limit: int = 1000) -> list[dict]:
    return [
        message
        for message in list_messages(limit=limit)
        if (
            message.get("topic") == REFUND_CREATED_TOPIC
            and str(message.get("payload", {}).get("order_id")) == str(order_id)
        )
    ]


def dangerous_tool_misuse(result: dict, forbidden: list[str] | None = None) -> list[str]:
    forbidden_set = DANGEROUS_TOOLS if forbidden is None else set(forbidden)
    return [
        tool_name
        for tool_name in get_tool_names(result)
        if tool_name in forbidden_set
    ]


def print_json_report(title: str, report: dict, report_path: Path) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2, default=str))
    print(f"report_path: {report_path}")
    print("=" * 72)
