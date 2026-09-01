from __future__ import annotations

import argparse
import hashlib
from collections import Counter

from app.agent.policies.evidence_guardrail import validate_policy_evidence
from app.core.config import BASE_DIR, get_settings
from app.core.schemas import ToolResult
from app.rag.index_manager import RAGIndexManager
from app.rag.query_context import RAGQueryContext, RetrievalQuery
from app.rag.ranking import (
    ABLATION_RUNNER_VERSION,
    BUSINESS_CONSTRAINT_VERSION,
    HYBRID_MODE,
    RANKING_MODES,
    RULE_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
    SEMANTIC_EVIDENCE_CATEGORIES,
    SEMANTIC_RERANK_MODE,
    EvidenceConstraint,
    build_ablation_rankings,
    build_evidence_constraint,
    evaluate_evidence_constraint,
)
from app.rag.reranker import RULE_RERANKER_VERSION
from app.rag.retriever import HybridRetriever
from app.rag.semantic_reranker import SemanticReranker, build_semantic_reranker
from scripts.eval.common import (
    NA,
    average,
    build_skipped_report,
    load_jsonl,
    now_iso,
    print_json_report,
    rate,
    save_report,
)


EVAL_PATH = BASE_DIR / "data" / "eval" / "rag_eval.jsonl"
DEFAULT_TOP_K = 5
CANDIDATE_K = 20
BASELINE_MODE = RULE_RERANK_MODE
COMPARISON_METRICS = (
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "mrr",
    "evidence_coverage_rate",
)
SIDE_EFFECTS = [
    "[READ ONLY BUSINESS DATA]",
    "[REFRESHES IN-MEMORY RAG INDEX]",
    "[WRITES CACHE]",
    "[CALLS EMBEDDING IF CONFIGURED]",
    "[LOADS LOCAL CROSS-ENCODER]",
]


def expected_sources(case: dict) -> list[str]:
    if case.get("expected_document"):
        return [case["expected_document"]]
    return list(case.get("expected_sources", []))


def case_query_context(case: dict) -> RAGQueryContext:
    data = case.get("rag_context")
    if not isinstance(data, dict):
        raise ValueError(
            f"RAG case {case.get('case_id', '<unknown>')} has no rag_context"
        )
    return RAGQueryContext(raw_query=case["query"], **data)


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


def _normalized(text: object) -> str:
    return str(text or "").lower().replace(" ", "")


def keyword_evidence_report(results: list[dict], terms: list[str]) -> dict:
    combined = _normalized(
        "\n".join(
            "\n".join(
                (
                    str(item.get("source", "")),
                    str(item.get("section", "")),
                    str(item.get("text", "")),
                )
            )
            for item in results
        )
    )
    matched = [term for term in terms if _normalized(term) in combined]
    missing = [term for term in terms if term not in matched]
    coverage = len(matched) / len(terms) if terms else 1.0
    return {
        "matched_terms": matched,
        "missing_terms": missing,
        "coverage": round(coverage, 4),
        "satisfied": not missing,
    }


def simplify_result(result: dict) -> dict:
    return {
        "chunk_id": result.get("chunk_id"),
        "source": result.get("source"),
        "section": result.get("section"),
        "citation": result.get("citation"),
        "retrieval_rank": result.get("retrieval_rank"),
        "retrieval_score": result.get("retrieval_score"),
        "hybrid_score": result.get("hybrid_score"),
        "vector_score": result.get("vector_score"),
        "bm25_score": result.get("bm25_score"),
        "keyword_score": result.get("keyword_score"),
        "rule_score": result.get("rule_score"),
        "rule_boost": result.get("rule_boost"),
        "rule_reason": result.get("rule_reason", []),
        "semantic_rerank_score": result.get("semantic_rerank_score"),
        "semantic_rank": result.get("semantic_rank"),
        "constraint_adjusted": result.get("constraint_adjusted", False),
        "constraint_reason": result.get("constraint_reason"),
        "final_rank": result.get("final_rank"),
        "text_preview": result.get("text", "")[:180],
    }


def diagnose_failure(
    *,
    pool_source_pass: bool,
    pool_keywords: dict,
    pool_constraint: dict,
    result_source_pass: bool,
    result_keywords: dict,
    result_constraint: dict,
    evidence_guardrail_pass: bool,
) -> tuple[str, str]:
    pool_problems = []
    if not pool_source_pass:
        pool_problems.append("expected source absent from Top20")
    if not pool_keywords["satisfied"]:
        pool_problems.append(
            "expected terms absent from Top20: "
            + ", ".join(pool_keywords["missing_terms"])
        )
    if not pool_constraint["constraint_satisfied"]:
        pool_problems.append(
            "required categories absent from Top20: "
            + ", ".join(pool_constraint["missing_categories"])
        )
    if pool_problems:
        return "retrieval_failure", "; ".join(pool_problems)

    ranking_problems = []
    if not result_source_pass:
        ranking_problems.append("expected source did not reach final TopK")
    if not result_keywords["satisfied"]:
        ranking_problems.append(
            "expected terms did not reach final TopK: "
            + ", ".join(result_keywords["missing_terms"])
        )
    if ranking_problems:
        return "ranking_failure", "; ".join(ranking_problems)

    if not result_constraint["constraint_satisfied"]:
        return (
            "evidence_coverage_failure",
            "required categories missing from final TopK: "
            + ", ".join(result_constraint["missing_categories"]),
        )

    if not evidence_guardrail_pass:
        return "evidence_guardrail_failure", "existing evidence guardrail rejected TopK"

    return "passed", "all retrieval, ranking, and evidence checks passed"


def classify_failure(**kwargs) -> str | None:
    failure_type, _ = diagnose_failure(**kwargs)
    return None if failure_type == "passed" else failure_type


def score_mode(
    case: dict,
    candidates: list[dict],
    ranked: list[dict],
    constraint: EvidenceConstraint,
    *,
    mode: str,
    top_k: int,
) -> dict:
    results = ranked[:top_k]
    sources = expected_sources(case)
    terms = list(case.get("expected_keywords", []))
    pool_rank = first_expected_rank(candidates, sources) if sources else None
    result_rank = first_expected_rank(results, sources) if sources else None
    pool_source_pass = not sources or pool_rank is not None
    result_source_pass = not sources or result_rank is not None
    pool_keywords = keyword_evidence_report(candidates, terms)
    result_keywords = keyword_evidence_report(results, terms)
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
    failure_type, reason = diagnose_failure(
        pool_source_pass=pool_source_pass,
        pool_keywords=pool_keywords,
        pool_constraint=pool_constraint,
        result_source_pass=result_source_pass,
        result_keywords=result_keywords,
        result_constraint=result_constraint,
        evidence_guardrail_pass=guardrail_pass,
    )

    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "mode": mode,
        "passed": failure_type == "passed",
        "failure_type": failure_type,
        "failure_stage": failure_type,
        "reason": reason,
        "source_metric_supported": bool(sources),
        "expected_sources": sources if sources else NA,
        "retrieved_documents": [item.get("source") for item in results],
        "expected_rank": result_rank if result_rank is not None else NA,
        "candidate_expected_rank": pool_rank if pool_rank is not None else NA,
        "hit_at_1": bool(result_rank is not None and result_rank <= 1),
        "hit_at_3": bool(result_rank is not None and result_rank <= 3),
        "hit_at_5": bool(result_rank is not None and result_rank <= 5),
        "reciprocal_rank": round(1 / result_rank, 4) if result_rank else 0.0,
        "required_evidence_categories": list(constraint.required_categories),
        "evidence_coverage_rate": result_constraint["evidence_coverage_rate"],
        "required_evidence_coverage": result_constraint[
            "required_evidence_coverage"
        ],
        "keywords_pass": result_keywords["satisfied"],
        "missing_keywords": result_keywords["missing_terms"],
        "keyword_report": result_keywords,
        "candidate_keyword_report": pool_keywords,
        "source_concentration": source_concentration(results),
        "constraint_satisfied": result_constraint["constraint_satisfied"],
        "constraint_report": result_constraint,
        "candidate_constraint_report": pool_constraint,
        "evidence_guardrail_pass": guardrail_pass,
        "evidence_guardrail_report": guardrail_report,
        "candidate_chunk_ids": [item.get("chunk_id") for item in candidates],
        "ranking_trace": [simplify_result(item) for item in results],
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
        "candidate_k": CANDIDATE_K,
        "hit_at_1": rate(sum(item["hit_at_1"] for item in results), total),
        "hit_at_3": rate(sum(item["hit_at_3"] for item in results), total),
        "hit_at_5": rate(sum(item["hit_at_5"] for item in results), total),
        "mrr": average([item["reciprocal_rank"] for item in results]),
        "evidence_coverage_rate": average(
            [item["evidence_coverage_rate"] for item in results]
        ),
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
    semantic_reranker: SemanticReranker,
) -> dict[str, dict]:
    if top_k <= 0 or top_k > CANDIDATE_K:
        raise ValueError(f"top_k must be between 1 and {CANDIDATE_K}")

    mode_results: dict[str, list[dict]] = {mode: [] for mode in RANKING_MODES}
    for case in cases:
        context = case_query_context(case)
        constraint = build_evidence_constraint(context)
        query = RetrievalQuery(case["query"], case["query"])
        candidates = retriever.retrieve_candidates(query, candidate_k=CANDIDATE_K)
        rankings = build_ablation_rankings(
            query,
            candidates,
            semantic_reranker=semantic_reranker,
            top_k=top_k,
            evidence_constraint=constraint,
        )
        for mode in RANKING_MODES:
            mode_results[mode].append(
                score_mode(
                    case,
                    candidates,
                    rankings[mode],
                    constraint,
                    mode=mode,
                    top_k=top_k,
                )
            )

    return {
        mode: build_mode_report(results, mode=mode, top_k=top_k)
        for mode, results in mode_results.items()
    }


def _metric_delta(before: dict, after: dict) -> dict:
    return {
        metric: round(float(after[metric]) - float(before[metric]), 4)
        for metric in COMPARISON_METRICS
    }


def build_comparison(mode_reports: dict[str, dict]) -> tuple[list[dict], dict]:
    table = [
        {
            "mode": mode,
            **{metric: mode_reports[mode][metric] for metric in COMPARISON_METRICS},
        }
        for mode in RANKING_MODES
    ]
    deltas = {
        "A_to_B": _metric_delta(
            mode_reports[HYBRID_MODE], mode_reports[RULE_RERANK_MODE]
        ),
        "A_to_C": _metric_delta(
            mode_reports[HYBRID_MODE], mode_reports[SEMANTIC_RERANK_MODE]
        ),
        "C_to_D": _metric_delta(
            mode_reports[SEMANTIC_RERANK_MODE],
            mode_reports[SEMANTIC_CONSTRAINT_MODE],
        ),
    }
    return table, deltas


def dataset_identity() -> dict:
    digest = hashlib.sha256(EVAL_PATH.read_bytes()).hexdigest()
    return {
        "path": str(EVAL_PATH),
        "version": f"sha256:{digest}",
    }


def reproducibility_metadata(
    *,
    manager: RAGIndexManager,
    semantic_reranker: SemanticReranker,
    top_k: int,
) -> dict:
    settings = get_settings()
    index = manager.get_active_index()
    return {
        "runner_version": ABLATION_RUNNER_VERSION,
        "run_timestamp": now_iso(),
        "dataset": dataset_identity(),
        "kb_version": manager.active_kb_version,
        "candidate_k": CANDIDATE_K,
        "top_k": top_k,
        "hybrid_retrieval": {
            "semantic_weight": settings.rag_semantic_weight,
            "bm25_weight": settings.rag_bm25_weight,
            "keyword_weight": settings.rag_keyword_weight,
            "candidate_multiplier": settings.rag_candidate_multiplier,
        },
        "embedding_identity": index.embedding_identity if index else None,
        "semantic_reranker": semantic_reranker.identity.to_dict(),
        "rule_reranker": {
            "mode": RULE_RERANK_MODE,
            "version": RULE_RERANKER_VERSION,
        },
        "business_constraint": {
            "version": BUSINESS_CONSTRAINT_VERSION,
            "semantic_mapping": dict(sorted(SEMANTIC_EVIDENCE_CATEGORIES.items())),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate RAG rerank ablations.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        manager = RAGIndexManager()
        refresh = manager.refresh()
        retriever = HybridRetriever(manager)
        semantic_reranker = build_semantic_reranker()
        cases = load_jsonl(EVAL_PATH)
        mode_reports = run_ablation(
            cases,
            retriever,
            top_k=args.top_k,
            semantic_reranker=semantic_reranker,
        )
        comparison, deltas = build_comparison(mode_reports)
        baseline = mode_reports[BASELINE_MODE]
        report = {
            **baseline,
            "side_effects": SIDE_EFFECTS,
            "dataset": str(EVAL_PATH),
            "dataset_cases": len(cases),
            "dev_set": True,
            "baseline_mode": BASELINE_MODE,
            "candidate_k": CANDIDATE_K,
            "kb_version": refresh.kb_version,
            "constraint_input": "rag_context_upstream_semantics",
            "comparison": comparison,
            "deltas": deltas,
            "reproducibility": reproducibility_metadata(
                manager=manager,
                semantic_reranker=semantic_reranker,
                top_k=args.top_k,
            ),
            "ablation": mode_reports,
        }
    except Exception as error:
        report = build_skipped_report(
            reason=f"{type(error).__name__}: {error}",
            side_effects=SIDE_EFFECTS,
            dataset=str(EVAL_PATH),
        )
        report["failed_count"] = 1
        report["execution_error"] = report["skip_reason"]

    report_path = save_report("eval_rag", report)
    print_json_report("RAG Evaluation", report, report_path)
    if report.get("skipped") or report.get("failed_count"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
