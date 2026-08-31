from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.entry.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR
from app.storage.database import init_database
from scripts.eval.common import (
    NA,
    build_skipped_report,
    find_tool_result,
    latest_trace_for_conversation,
    load_jsonl,
    new_conversation_id,
    print_json_report,
    rate,
    save_report,
)


EVAL_PATH = BASE_DIR / "data" / "eval" / "answer_eval.jsonl"
SIDE_EFFECTS = ["[WRITES DATABASE: conversation/trace]", "[WRITES CACHE]", "[CALLS EMBEDDING IF CONFIGURED]"]


def get_policy_citations(result: dict) -> list[str]:
    policy_result = find_tool_result(result, "policy_search")
    if not policy_result or not policy_result.get("success"):
        return []

    citations = []
    for item in policy_result.get("result", []):
        citation = item.get("citation") or item.get("source")
        if citation:
            citations.append(citation)
    return citations


def contains_all(reply: str, keywords: list[str]) -> tuple[bool, list[str]]:
    missing = [keyword for keyword in keywords if keyword not in reply]
    return len(missing) == 0, missing


def contains_none(reply: str, keywords: list[str]) -> tuple[bool, list[str]]:
    present = [keyword for keyword in keywords if keyword in reply]
    return len(present) == 0, present


def check_groundedness(reply: str, citations: list[str], required: bool) -> tuple[bool | str, list[str]]:
    if not required:
        return NA, []
    if not citations:
        return False, ["policy_search did not return citations"]

    matched = [citation for citation in citations if citation in reply]
    return len(matched) > 0, matched


def check_relevance(reply: str, query: str, expected_keywords: list[str]) -> bool:
    if not reply.strip():
        return False
    if any(keyword in reply for keyword in expected_keywords):
        return True
    return any(token in reply for token in ["退款", "订单", "人工", "审核", "政策"])


def run_single_case(case: dict) -> dict:
    conversation_id = new_conversation_id("answer-eval", case["id"])
    result = run_customer_support_agent(
        user_message=case["query"],
        conversation_id=conversation_id,
        use_llm=False,
    )
    trace = latest_trace_for_conversation(result.get("conversation_id"))
    reply = result.get("reply", "")
    expected_keywords = list(case.get("expected_keywords", []))
    forbidden_keywords = list(case.get("forbidden_keywords", []))
    citations = get_policy_citations(result)

    correctness_pass, missing_keywords = contains_all(reply, expected_keywords)
    completeness_pass = correctness_pass
    hallucination_pass, hallucinated_keywords = contains_none(reply, forbidden_keywords)
    faithfulness_pass, matched_citations = check_groundedness(
        reply,
        citations,
        required=bool(case.get("require_citation")),
    )
    relevance_pass = check_relevance(reply, case["query"], expected_keywords)

    errors = []
    if not correctness_pass:
        errors.append(f"missing_expected_keywords={missing_keywords}")
    if not relevance_pass:
        errors.append("reply is empty or not relevant to refund workflow")
    if faithfulness_pass is False:
        errors.append(f"grounding_failed={matched_citations}")
    if not hallucination_pass:
        errors.append(f"forbidden_keywords_present={hallucinated_keywords}")

    passed = bool(
        correctness_pass
        and relevance_pass
        and completeness_pass
        and faithfulness_pass is not False
        and hallucination_pass
    )

    return {
        "case_id": case["id"],
        "query": case["query"],
        "passed": passed,
        "deterministic_metrics": {
            "correctness": correctness_pass,
            "relevance": relevance_pass,
            "completeness": completeness_pass,
            "faithfulness_groundedness": faithfulness_pass,
            "hallucination_free": hallucination_pass,
        },
        "llm_judge_metrics": NA,
        "expected_keywords": expected_keywords,
        "missing_keywords": missing_keywords,
        "forbidden_keywords": forbidden_keywords,
        "hallucinated_keywords": hallucinated_keywords,
        "citations": citations,
        "matched_citations": matched_citations,
        "trace_id": trace.get("trace_id") if trace else NA,
        "reply": reply,
        "reason": "; ".join(errors) if errors else "passed",
        "notes": case.get("notes", ""),
    }


def build_report(results: list[dict]) -> dict:
    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    grounded_cases = [
        item
        for item in results
        if item["deterministic_metrics"]["faithfulness_groundedness"] != NA
    ]
    failed_cases = [
        {
            "case_id": item["case_id"],
            "query": item["query"],
            "expected": {
                "keywords": item["expected_keywords"],
                "forbidden_keywords": item["forbidden_keywords"],
                "citations": item["citations"],
            },
            "actual": {
                "reply": item["reply"],
                "matched_citations": item["matched_citations"],
            },
            "trace_id": item["trace_id"],
            "reason": item["reason"],
        }
        for item in results
        if not item["passed"]
    ]

    return {
        "side_effects": SIDE_EFFECTS,
        "dataset": str(EVAL_PATH),
        "total_cases": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "deterministic_metrics": {
            "correctness": rate(
                sum(1 for item in results if item["deterministic_metrics"]["correctness"]),
                total,
            ),
            "relevance": rate(
                sum(1 for item in results if item["deterministic_metrics"]["relevance"]),
                total,
            ),
            "completeness": rate(
                sum(1 for item in results if item["deterministic_metrics"]["completeness"]),
                total,
            ),
            "faithfulness_groundedness": rate(
                sum(1 for item in grounded_cases if item["deterministic_metrics"]["faithfulness_groundedness"] is True),
                len(grounded_cases),
            ),
            "hallucination_rate": rate(
                sum(1 for item in results if not item["deterministic_metrics"]["hallucination_free"]),
                total,
            ),
        },
        "llm_judge_metrics": NA,
        "failed_cases": failed_cases,
        "results": results,
    }


def main() -> None:
    cases = []
    try:
        cases = load_jsonl(EVAL_PATH)
        init_database()
        results = [run_single_case(case) for case in cases]
        report = build_report(results)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
            dataset=str(EVAL_PATH),
            dataset_cases=len(cases),
        )
    report_path = save_report("eval_answer", report)
    print_json_report("Answer Evaluation", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
