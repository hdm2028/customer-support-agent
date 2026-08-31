from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.policies.evidence_guardrail import validate_policy_evidence
from app.core.config import BASE_DIR
from app.core.schemas import ToolResult
from app.rag.retriever import HybridRetriever
from scripts.eval.common import (
    NA,
    average,
    build_skipped_report,
    load_jsonl,
    print_json_report,
    rate,
    save_report,
)


EVAL_PATH = BASE_DIR / "data" / "eval" / "rag_eval.jsonl"
DEFAULT_TOP_K = 5
SIDE_EFFECTS = ["[READ ONLY BUSINESS DATA]", "[WRITES CACHE]", "[CALLS EMBEDDING IF CONFIGURED]"]


def normalize_text(text: str) -> str:
    return text.lower().replace(" ", "")


def expected_sources(case: dict) -> list[str]:
    if case.get("expected_document"):
        return [case["expected_document"]]
    return list(case.get("expected_sources", []))


def keyword_hit(results: list[dict], expected_keywords: list[str]) -> tuple[bool, list[str]]:
    if not expected_keywords:
        return True, []

    combined = "\n".join(
        f"{item.get('source', '')}\n{item.get('section', '')}\n{item.get('text', '')}"
        for item in results
    )
    normalized = normalize_text(combined)
    missing = [
        keyword
        for keyword in expected_keywords
        if normalize_text(keyword) not in normalized
    ]
    return len(missing) == 0, missing


def first_expected_rank(results: list[dict], sources: list[str]) -> int | None:
    for index, item in enumerate(results, start=1):
        if item.get("source") in sources:
            return index
    return None


def simplify_result(result: dict) -> dict:
    return {
        "source": result.get("source"),
        "section": result.get("section"),
        "citation": result.get("citation"),
        "score": result.get("score"),
        "hybrid_score": result.get("hybrid_score"),
        "vector_score": result.get("vector_score"),
        "bm25_score": result.get("bm25_score"),
        "keyword_score": result.get("keyword_score"),
        "retrieval_score": result.get("retrieval_score"),
        "rerank_bonus": result.get("rerank_bonus"),
        "rerank_score": result.get("rerank_score"),
        "rerank_reasons": result.get("rerank_reasons", []),
        "text_preview": result.get("text", "")[:180],
    }


def run_single_case(case: dict, retriever: HybridRetriever, top_k: int) -> dict:
    query = case["query"]
    results = retriever.retrieve(query, top_k=top_k)
    sources = expected_sources(case)
    source_metric_supported = bool(sources)
    rank = first_expected_rank(results, sources) if source_metric_supported else None
    expected_keywords = list(case.get("expected_keywords", []))
    keywords_pass, missing_keywords = keyword_hit(results, expected_keywords)
    evidence_guardrail_pass, evidence_guardrail_report = validate_policy_evidence(
        query,
        ToolResult(
            tool_name="policy_search",
            success=bool(results),
            result=results if results else "RAG 没有返回任何政策证据。",
        ),
    )

    errors = []
    if not source_metric_supported:
        errors.append("missing expected_document or expected_sources; Hit/MRR metrics are N/A for this case")
    elif rank is None or rank > top_k:
        errors.append(f"expected_source_miss expected={sources}")
    if missing_keywords:
        errors.append(f"missing_keywords={missing_keywords}")
    if not evidence_guardrail_pass:
        errors.append(f"evidence_guardrail_failed={evidence_guardrail_report}")

    passed = bool(
        source_metric_supported
        and rank is not None
        and rank <= top_k
        and keywords_pass
        and evidence_guardrail_pass
    )

    return {
        "case_id": case["id"],
        "query": query,
        "passed": passed,
        "source_metric_supported": source_metric_supported,
        "expected_document": sources if sources else NA,
        "retrieved_documents": [item.get("source") for item in results],
        "expected_rank": rank if rank is not None else NA,
        "hit_at_1": bool(rank is not None and rank <= 1) if source_metric_supported else NA,
        "hit_at_3": bool(rank is not None and rank <= 3) if source_metric_supported else NA,
        "hit_at_5": bool(rank is not None and rank <= 5) if source_metric_supported else NA,
        "reciprocal_rank": round(1 / rank, 4) if rank else 0.0 if source_metric_supported else NA,
        "keywords_pass": keywords_pass,
        "missing_keywords": missing_keywords,
        "evidence_guardrail_pass": evidence_guardrail_pass,
        "evidence_guardrail_report": evidence_guardrail_report,
        "retrieval_scores": [simplify_result(item) for item in results],
        "reason": "; ".join(errors) if errors else "passed",
        "notes": case.get("notes", ""),
    }


def build_report(results: list[dict], top_k: int) -> dict:
    total = len(results)
    supported = [item for item in results if item["source_metric_supported"]]
    supported_count = len(supported)
    passed_count = sum(1 for item in results if item["passed"])
    failed_cases = [
        {
            "case_id": item["case_id"],
            "query": item["query"],
            "expected_document": item["expected_document"],
            "retrieved_documents": item["retrieved_documents"],
            "expected_rank": item["expected_rank"],
            "retrieval_scores": item["retrieval_scores"],
            "reason": item["reason"],
        }
        for item in results
        if not item["passed"]
    ]

    return {
        "side_effects": SIDE_EFFECTS,
        "dataset": str(EVAL_PATH),
        "total_cases": total,
        "metric_supported_cases": supported_count,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "top_k": top_k,
        "hit_at_1": rate(sum(1 for item in supported if item["hit_at_1"] is True), supported_count),
        "hit_at_3": rate(sum(1 for item in supported if item["hit_at_3"] is True), supported_count),
        "hit_at_5": rate(sum(1 for item in supported if item["hit_at_5"] is True), supported_count),
        "mrr": average([
            item["reciprocal_rank"]
            for item in supported
            if isinstance(item["reciprocal_rank"], int | float)
        ]),
        "keyword_pass_rate": rate(sum(1 for item in results if item["keywords_pass"]), total),
        "evidence_guardrail_pass_rate": rate(
            sum(1 for item in results if item["evidence_guardrail_pass"]),
            total,
        ),
        "failed_cases": failed_cases,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate refund-policy RAG retrieval quality.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        retriever = HybridRetriever()
        cases = load_jsonl(EVAL_PATH)
        results = [run_single_case(case, retriever, top_k=args.top_k) for case in cases]
        report = build_report(results, top_k=args.top_k)
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
            dataset=str(EVAL_PATH),
        )
    report_path = save_report("eval_rag", report)
    print_json_report("RAG Evaluation", report, report_path)
    if report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
