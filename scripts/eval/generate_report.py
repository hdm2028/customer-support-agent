from __future__ import annotations

import json
from typing import Any

from app.core.config import BASE_DIR
from scripts.eval.common import NA, REPORT_DIR, now_iso, status_from_report, write_json, write_text


REPORT_PATH = REPORT_DIR / "evaluation_report.json"
MARKDOWN_PATH = REPORT_DIR / "evaluation_report.md"

SOURCE_REPORTS = {
    "routing": "eval_routing.json",
    "rag": "eval_rag.json",
    "tool_calling": "eval_tools.json",
    "answer": "eval_answer.json",
    "e2e": "eval_e2e.json",
    "refund_idempotency": "reliability_refund_idempotency.json",
    "refund_concurrency": "reliability_refund_concurrency.json",
    "mq_duplicate_delivery": "reliability_mq_duplicate_delivery.json",
    "high_risk_review": "reliability_high_risk_review.json",
    "tool_failure": "reliability_tool_failure.json",
    "service_degradation": "reliability_service_degradation.json",
}


def load_report(filename: str) -> dict | None:
    path = REPORT_DIR / filename
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def metric(report: dict | None, key: str) -> Any:
    if not report:
        return NA
    return report.get(key, NA)


def nested_metric(report: dict | None, section: str, key: str) -> Any:
    if not report:
        return NA
    value = report.get(section)
    if not isinstance(value, dict):
        return NA
    return value.get(key, NA)


def summarize_dataset(reports: dict[str, dict | None]) -> dict:
    def cases(report: dict | None) -> Any:
        if not report:
            return NA
        return report.get("dataset_cases", report.get("total_cases", NA))

    return {
        "routing_cases": cases(reports["routing"]),
        "rag_cases": cases(reports["rag"]),
        "tool_cases": cases(reports["tool_calling"]),
        "answer_cases": cases(reports["answer"]),
        "e2e_cases": cases(reports["e2e"]),
    }


def risk_summary(e2e_report: dict | None) -> dict:
    if not e2e_report or e2e_report.get("skipped"):
        return {
            "risk_recall": NA,
            "false_negative_count": NA,
            "false_positive_count": NA,
        }

    expected_high = 0
    true_positive = 0
    false_negative = 0
    false_positive = 0

    for item in e2e_report.get("results", []):
        expected_route = item.get("route", {})
        expected_is_high = (
            item.get("expected_final_action") == "manual_review_created"
            or item.get("checks", {}).get("risk_pass") is True
            and expected_route.get("risk_level") == "high"
        )
        actual_risk_levels = []
        for tool_result in item.get("tool_results", []):
            if tool_result.get("tool_name") == "risk_check" and isinstance(tool_result.get("result"), dict):
                actual_risk_levels.append(tool_result["result"].get("risk_level"))
        actual_is_high = "high" in actual_risk_levels

        if expected_is_high:
            expected_high += 1
            if actual_is_high:
                true_positive += 1
            else:
                false_negative += 1
        elif actual_is_high:
            false_positive += 1

    return {
        "risk_recall": round(true_positive / expected_high, 4) if expected_high else NA,
        "false_negative_count": false_negative,
        "false_positive_count": false_positive,
    }


def collect_failed_cases(reports: dict[str, dict | None]) -> list[dict]:
    failed_cases = []
    for name, report in reports.items():
        if not report:
            continue
        for item in report.get("failed_cases", []):
            failed_cases.append(
                {
                    "source": name,
                    "case_id": item.get("case_id"),
                    "failure_stage": item.get("failure_stage", name.upper()),
                    "expected": item.get("expected", NA),
                    "actual": item.get("actual", NA),
                    "reason": item.get("reason", NA),
                    "trace_id": item.get("trace_id", NA),
                }
            )
    return failed_cases


def reliability_summary(reports: dict[str, dict | None]) -> dict:
    idempotency = reports["refund_idempotency"]
    concurrency = reports["refund_concurrency"]
    mq = reports["mq_duplicate_delivery"]
    return {
        "refund_idempotency": {
            "status": status_from_report(idempotency) if idempotency else "N/A",
            "requests": metric(idempotency, "requests"),
            "refund_records": metric(idempotency, "refund_records"),
            "duplicate_refunds": metric(idempotency, "duplicate_refunds"),
        },
        "refund_concurrency": {
            "status": status_from_report(concurrency) if concurrency else "N/A",
            "concurrent_requests": metric(concurrency, "requests"),
            "refund_records": metric(concurrency, "refund_records"),
            "duplicate_refunds": metric(concurrency, "duplicate_refunds"),
        },
        "mq": {
            "status": status_from_report(mq) if mq else "N/A",
            "received": metric(mq, "received_events"),
            "processed": metric(mq, "processed_events"),
            "duplicates_ignored": metric(mq, "ignored_duplicate_events"),
        },
        "high_risk_review": {
            "status": status_from_report(reports["high_risk_review"]) if reports["high_risk_review"] else "N/A",
        },
        "tool_failure": {
            "status": status_from_report(reports["tool_failure"]) if reports["tool_failure"] else "N/A",
        },
        "service_degradation": {
            "status": status_from_report(reports["service_degradation"]) if reports["service_degradation"] else "N/A",
        },
    }


def build_report() -> dict:
    reports = {
        name: load_report(filename)
        for name, filename in SOURCE_REPORTS.items()
    }
    e2e = reports["e2e"]
    answer = reports["answer"]
    unified = {
        "title": "Customer Support Agent Evaluation",
        "created_at": now_iso(),
        "dataset": summarize_dataset(reports),
        "routing": {
            "accuracy": metric(reports["routing"], "routing_accuracy"),
            "macro_f1": metric(reports["routing"], "agent_f1"),
        },
        "rag": {
            "hit_at_1": metric(reports["rag"], "hit_at_1"),
            "hit_at_3": metric(reports["rag"], "hit_at_3"),
            "hit_at_5": metric(reports["rag"], "hit_at_5"),
            "mrr": metric(reports["rag"], "mrr"),
        },
        "tool_calling": {
            "selection_accuracy": metric(reports["tool_calling"], "tool_selection_accuracy"),
            "argument_accuracy": metric(reports["tool_calling"], "tool_argument_accuracy"),
            "execution_success_rate": metric(reports["tool_calling"], "tool_execution_success_rate"),
            "dangerous_tool_misuse": metric(reports["tool_calling"], "dangerous_tool_misuse_count"),
        },
        "answer": {
            "correctness": nested_metric(answer, "deterministic_metrics", "correctness"),
            "faithfulness": nested_metric(answer, "deterministic_metrics", "faithfulness_groundedness"),
            "hallucination_rate": nested_metric(answer, "deterministic_metrics", "hallucination_rate"),
        },
        "end_to_end": {
            "task_success_rate": metric(e2e, "task_success_rate"),
            "workflow_success_rate": metric(e2e, "workflow_success_rate"),
            "routing_success_rate": metric(e2e, "routing_success_rate"),
            "rag_success_rate": metric(e2e, "rag_success_rate"),
            "tool_success_rate": metric(e2e, "tool_success_rate"),
            "answer_success_rate": metric(e2e, "answer_success_rate"),
        },
        "risk": risk_summary(e2e),
        "reliability": reliability_summary(reports),
        "failed_cases": collect_failed_cases(reports),
        "source_reports": {
            name: str(REPORT_DIR / filename) if (REPORT_DIR / filename).exists() else NA
            for name, filename in SOURCE_REPORTS.items()
        },
    }
    return unified


def markdown_value(value: Any) -> str:
    if isinstance(value, float):
        return str(value)
    if value is None:
        return NA
    return str(value)


def build_markdown(report: dict) -> str:
    lines = [
        "# Customer Support Agent Evaluation",
        "",
        f"Generated at: {report['created_at']}",
        "",
        "## Dataset",
        "",
    ]
    for key, value in report["dataset"].items():
        lines.append(f"- {key}: {markdown_value(value)}")

    sections = [
        ("Routing", report["routing"]),
        ("RAG", report["rag"]),
        ("Tool Calling", report["tool_calling"]),
        ("Answer", report["answer"]),
        ("End-to-End", report["end_to_end"]),
        ("Risk", report["risk"]),
    ]
    for title, payload in sections:
        lines.extend(["", f"## {title}", ""])
        for key, value in payload.items():
            lines.append(f"- {key}: {markdown_value(value)}")

    lines.extend(["", "## Reliability", ""])
    for title, payload in report["reliability"].items():
        lines.append(f"### {title}")
        for key, value in payload.items():
            lines.append(f"- {key}: {markdown_value(value)}")
        lines.append("")

    lines.extend(["## Failed Cases", ""])
    if not report["failed_cases"]:
        lines.append("- none")
    else:
        for item in report["failed_cases"]:
            lines.append(
                f"- {item.get('source')} / {item.get('case_id')} / "
                f"{item.get('failure_stage')}: {markdown_value(item.get('reason'))}"
            )

    lines.extend(["", "## Source Reports", ""])
    for key, value in report["source_reports"].items():
        lines.append(f"- {key}: {markdown_value(value)}")

    return "\n".join(lines) + "\n"


def main() -> None:
    report = build_report()
    write_json(REPORT_PATH, report)
    write_text(MARKDOWN_PATH, build_markdown(report))
    print(f"generated: {REPORT_PATH}")
    print(f"generated: {MARKDOWN_PATH}")


if __name__ == "__main__":
    main()
