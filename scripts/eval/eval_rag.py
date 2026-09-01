from __future__ import annotations

import argparse
from collections import Counter

from app.agent.policies.evidence_guardrail import validate_policy_evidence
from app.core.config import BASE_DIR
from app.core.schemas import ToolResult
from app.rag.index_manager import RAGIndexManager
from app.rag.query_context import RetrievalQuery
from app.rag.ranking import (
    HYBRID_MODE,
    RANKING_MODES,
    RULE_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
    EvidenceConstraint,
    evaluate_evidence_constraint,
    rank_candidates,
)
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
BASELINE_MODE = RULE_RERANK_MODE
SIDE_EFFECTS = [
    "[READ ONLY BUSINESS DATA]",
    "[REFRESHES IN-MEMORY RAG INDEX]",
    "[WRITES CACHE]",
    "[CALLS EMBEDDING IF CONFIGURED]",
]


def expected_sources(case: dict) -> list[str]:
    if case.get("expected_document"):
        return [case["expected_document"]]
    return list(case.get("expected_sources", []))


def evidence_constraint(case: dict) -> EvidenceConstraint:
    return EvidenceConstraint(
        required_sources=tuple(expected_sources(case)),
        required_terms=tuple(case.get("expected_keywords", [])),
    )


def first_expected_rank(results: list[dict], sources: list[str]) -> int | None:
    for index, item in enumerate(results, start=1):
        if item.get("source") in sources:
            return index
    return None


def source_concentration(results: list[dict]) -> float:
    sources = [item.get("source") for item in results if item.get("source")]
    if not sources:
        return 0.0
    return round(max(Counter(sources).values()) / len(sources), 4)


def simplify_result(result: dict) -> dict:
    return {
        "chunk_id": result.get("chunk_id"),
        "source": result.get("source"),
        "section": result.get("section"),
        "citation": result.get("citation"),
        "score": result.get("score"),
        "hybrid_score": result.get("hybrid_score"),
        "vector_score": result.get("vector_score"),
        "bm25_score": result.get("bm25_score"),
        "keyword_score": result.get("keyword_score"),
        "rerank_bonus": result.get("rerank_bonus"),
        "rerank_score": result.get("rerank_score"),
        "semantic_rerank_score": result.get("semantic_rerank_score"),
        "constraint_original_rank": result.get("constraint_original_rank"),
        "rerank_reasons": result.get("rerank_reasons", []),
        "text_preview": result.get("text", "")[:180],
    }


def classify_failure(
    *,
    mode: str,
    pool_rank: int | None,
    result_rank: int | None,
    pool_constraint: dict,
    result_constraint: dict,
    evidence_guardrail_pass: bool,
) -> str | None:
    if pool_rank is None or not pool_constraint["constraint_satisfied"]:
        return "retrieval_failure"

    if result_rank is None or not result_constraint["constraint_satisfied"]:
        if mode == SEMANTIC_CONSTRAINT_MODE:
            return "constraint_failure"
        return "rerank_failure"

    if not evidence_guardrail_pass:
        return "evidence_guardrail_failure"

    return None


def score_mode(
    case: dict,
    query: RetrievalQuery,
    candidates: list[dict],
    *,
    mode: str,
    top_k: int,
) -> dict:
    constraint = evidence_constraint(case)
    ranked = rank_candidates(
        query,
        candidates,
        mode=mode,
        top_k=top_k,
        evidence_constraint=constraint,
    )
    results = ranked[:top_k]
    sources = list(constraint.required_sources)
    pool_rank = first_expected_rank(candidates, sources) if sources else None
    result_rank = first_expected_rank(results, sources) if sources else None
    pool_constraint = evaluate_evidence_constraint(candidates, constraint)
    result_constraint = evaluate_evidence_constraint(results, constraint)
    guardrail_pass, guardrail_report = validate_policy_evidence(
        case["query"],
        ToolResult(
            tool_name="policy_search",
            success=bool(results),
            result=results if results else "RAG 没有返回任何政策证据。",
        ),
    )
    failure_type = classify_failure(
        mode=mode,
        pool_rank=pool_rank,
        result_rank=result_rank,
        pool_constraint=pool_constraint,
        result_constraint=result_constraint,
        evidence_guardrail_pass=guardrail_pass,
    )

    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "mode": mode,
        "passed": failure_type is None,
        "failure_type": failure_type or "passed",
        "failure_stage": failure_type or "passed",
        "reason": failure_type or "passed",
        "source_metric_supported": bool(sources),
        "expected_document": sources if sources else NA,
        "expected_sources": sources if sources else NA,
        "retrieved_documents": [item.get("source") for item in results],
        "expected_rank": result_rank if result_rank is not None else NA,
        "candidate_expected_rank": pool_rank if pool_rank is not None else NA,
        "hit_at_1": bool(result_rank is not None and result_rank <= 1),
        "hit_at_3": bool(result_rank is not None and result_rank <= 3),
        "hit_at_5": bool(result_rank is not None and result_rank <= 5),
        "reciprocal_rank": round(1 / result_rank, 4) if result_rank else 0.0,
        "required_evidence_coverage": result_constraint[
            "required_evidence_coverage"
        ],
        "keywords_pass": not result_constraint["missing_terms"],
        "missing_keywords": result_constraint["missing_terms"],
        "source_concentration": source_concentration(results),
        "constraint_satisfied": result_constraint["constraint_satisfied"],
        "constraint_report": result_constraint,
        "candidate_constraint_report": pool_constraint,
        "evidence_guardrail_pass": guardrail_pass,
        "evidence_guardrail_report": guardrail_report,
        "candidate_chunk_ids": [item.get("chunk_id") for item in candidates],
        "retrieval_scores": [simplify_result(item) for item in results],
        "notes": case.get("notes", ""),
    }


def build_mode_report(results: list[dict], *, mode: str, top_k: int) -> dict:
    total = len(results)
    failures = Counter(
        item["failure_type"]
        for item in results
        if item["failure_type"] != "passed"
    )
    failed_cases = [item for item in results if not item["passed"]]

    return {
        "mode": mode,
        "total_cases": total,
        "metric_supported_cases": total,
        "passed_count": total - len(failed_cases),
        "failed_count": len(failed_cases),
        "top_k": top_k,
        "hit_at_1": rate(sum(item["hit_at_1"] for item in results), total),
        "hit_at_3": rate(sum(item["hit_at_3"] for item in results), total),
        "hit_at_5": rate(sum(item["hit_at_5"] for item in results), total),
        "mrr": average([item["reciprocal_rank"] for item in results]),
        "required_evidence_coverage": average(
            [item["required_evidence_coverage"] for item in results]
        ),
        "source_concentration": average(
            [item["source_concentration"] for item in results]
        ),
        "constraint_satisfaction": rate(
            sum(item["constraint_satisfied"] for item in results),
            total,
        ),
        "keyword_pass_rate": rate(
            sum(item["keywords_pass"] for item in results),
            total,
        ),
        "evidence_guardrail_pass_rate": rate(
            sum(item["evidence_guardrail_pass"] for item in results),
            total,
        ),
        "failure_counts": dict(failures),
        "failed_cases": failed_cases,
        "results": results,
    }


def run_ablation(
    cases: list[dict],
    retriever: HybridRetriever,
    *,
    top_k: int,
) -> tuple[dict[str, dict], int]:
    candidate_k = retriever.resolve_candidate_k(top_k)
    mode_results: dict[str, list[dict]] = {mode: [] for mode in RANKING_MODES}

    for case in cases:
        query = RetrievalQuery(case["query"], case["query"])
        candidates = retriever.retrieve_candidates(
            query,
            candidate_k=candidate_k,
        )
        for mode in RANKING_MODES:
            mode_results[mode].append(
                score_mode(
                    case,
                    query,
                    candidates,
                    mode=mode,
                    top_k=top_k,
                )
            )

    return {
        mode: build_mode_report(results, mode=mode, top_k=top_k)
        for mode, results in mode_results.items()
    }, candidate_k


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate RAG retrieval ablations.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        manager = RAGIndexManager()
        refresh = manager.refresh()
        retriever = HybridRetriever(manager)
        cases = load_jsonl(EVAL_PATH)
        mode_reports, candidate_k = run_ablation(
            cases,
            retriever,
            top_k=args.top_k,
        )
        baseline = mode_reports[BASELINE_MODE]
        report = {
            **baseline,
            "side_effects": SIDE_EFFECTS,
            "dataset": str(EVAL_PATH),
            "dev_set": True,
            "baseline_mode": BASELINE_MODE,
            "candidate_k": candidate_k,
            "kb_version": refresh.kb_version,
            "constraint_input": "dev_case_evidence_annotations",
            "ablation": mode_reports,
        }
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
