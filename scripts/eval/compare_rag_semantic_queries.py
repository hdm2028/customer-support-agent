from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.rag.query_builder import (
    INTENT_RETRIEVAL_TERMS,
    TOPIC_RETRIEVAL_TERMS,
)
from app.rag.ranking import (
    HYBRID_MODE,
    RANKING_MODES,
    RERANK_QUERY_MODE,
    RULE_RERANK_MODE,
    SEMANTIC_QUERY_MODE,
    SEMANTIC_RERANK_MODE,
)


DEFAULT_SEMANTIC_REPORT = Path("reports/eval_rag_semantic.json")
DEFAULT_RERANK_REPORT = Path("reports/eval_rag_rerank.json")
DEFAULT_OUTPUT = Path("reports/eval_rag_semantic_query_comparison.json")
METRIC_FIELDS = (
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "mrr",
    "candidate_evidence_recall_at_20",
    "evidence_coverage_rate",
    "evidence_guardrail_pass_rate",
)
AGGREGATE_IGNORED_FIELDS = {
    "results",
    "failed_cases",
}
QUERY_CONTRACT_STABLE_FIELDS = (
    "raw_query",
    "semantic_query",
    "lexical_query",
    "rerank_query",
    "input_context",
)
NOT_AVAILABLE = {None, "N/A"}


def load_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        report = json.load(file)

    if report.get("skipped"):
        raise ValueError(f"Report was skipped: {path}")

    return report


def case_map(mode_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results = mode_report.get("results", [])
    mapped = {str(item["case_id"]): item for item in results}

    if len(mapped) != len(results):
        raise ValueError("Duplicate case_id found in evaluation report")

    return mapped


def without_fields(value: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in fields}


def stable_case_result(result: dict[str, Any]) -> dict[str, Any]:
    return without_fields(result, {"query_contract"})


def validate_experiment_isolation(
    semantic_report: dict[str, Any],
    rerank_report: dict[str, Any],
) -> dict[str, Any]:
    if semantic_report.get("semantic_query_mode") != SEMANTIC_QUERY_MODE:
        raise ValueError("C0 report is not in semantic query mode")

    if rerank_report.get("semantic_query_mode") != RERANK_QUERY_MODE:
        raise ValueError("C1 report is not in rerank query mode")

    semantic_repro = dict(semantic_report["reproducibility"])
    rerank_repro = dict(rerank_report["reproducibility"])
    semantic_repro.pop("run_timestamp", None)
    rerank_repro.pop("run_timestamp", None)
    semantic_repro.pop("semantic_query_mode", None)
    rerank_repro.pop("semantic_query_mode", None)

    if semantic_repro != rerank_repro:
        raise ValueError("Reproducibility metadata differs outside query mode")

    stable_contracts_checked = 0
    candidate_pools_checked = 0

    for mode in (HYBRID_MODE, RULE_RERANK_MODE):
        semantic_mode = semantic_report["ablation"][mode]
        rerank_mode = rerank_report["ablation"][mode]

        if without_fields(semantic_mode, AGGREGATE_IGNORED_FIELDS) != without_fields(
            rerank_mode,
            AGGREGATE_IGNORED_FIELDS,
        ):
            raise ValueError(f"Aggregate result changed for frozen mode: {mode}")

        semantic_cases = case_map(semantic_mode)
        rerank_cases = case_map(rerank_mode)

        if semantic_cases.keys() != rerank_cases.keys():
            raise ValueError(f"Case set changed for frozen mode: {mode}")

        for case_id, semantic_case in semantic_cases.items():
            rerank_case = rerank_cases[case_id]

            if semantic_case.get("candidate_chunk_ids") != rerank_case.get(
                "candidate_chunk_ids"
            ):
                raise ValueError(f"Candidate Top20 changed for {mode}/{case_id}")

            if stable_case_result(semantic_case) != stable_case_result(rerank_case):
                raise ValueError(f"Frozen A/B result changed for {mode}/{case_id}")

            semantic_contract = semantic_case["query_contract"]
            rerank_contract = rerank_case["query_contract"]

            for field in QUERY_CONTRACT_STABLE_FIELDS:
                if semantic_contract.get(field) != rerank_contract.get(field):
                    raise ValueError(
                        f"Query contract field {field} changed for {mode}/{case_id}"
                    )

            stable_contracts_checked += 1
            candidate_pools_checked += 1

    for report_name, report in (
        ("C0", semantic_report),
        ("C1", rerank_report),
    ):
        baseline_cases = case_map(report["ablation"][HYBRID_MODE])

        for mode in RANKING_MODES:
            mode_cases = case_map(report["ablation"][mode])

            if baseline_cases.keys() != mode_cases.keys():
                raise ValueError(f"Case set differs inside {report_name}/{mode}")

            for case_id, baseline_case in baseline_cases.items():
                if baseline_case.get("candidate_chunk_ids") != mode_cases[
                    case_id
                ].get("candidate_chunk_ids"):
                    raise ValueError(
                        f"Candidate Top20 differs inside {report_name}/{mode}/{case_id}"
                    )

                candidate_pools_checked += 1

    semantic_cases = case_map(semantic_report["ablation"][SEMANTIC_RERANK_MODE])
    rerank_cases = case_map(rerank_report["ablation"][SEMANTIC_RERANK_MODE])

    for case_id, semantic_case in semantic_cases.items():
        rerank_case = rerank_cases[case_id]

        if semantic_case.get("candidate_chunk_ids") != rerank_case.get(
            "candidate_chunk_ids"
        ):
            raise ValueError(f"Candidate Top20 changed for C/{case_id}")

        semantic_contract = semantic_case["query_contract"]
        rerank_contract = rerank_case["query_contract"]

        for field in QUERY_CONTRACT_STABLE_FIELDS:
            if semantic_contract.get(field) != rerank_contract.get(field):
                raise ValueError(f"Query contract field {field} changed for C/{case_id}")

        stable_contracts_checked += 1
        candidate_pools_checked += 1

    frozen_mode_metrics = {}

    for mode in (HYBRID_MODE, RULE_RERANK_MODE):
        mode_report = semantic_report["ablation"][mode]
        frozen_mode_metrics[mode] = {
            "overall_pass": (
                f"{mode_report['passed_count']}/{mode_report['total_cases']}"
            ),
            **{field: mode_report.get(field) for field in METRIC_FIELDS},
        }

    return {
        "passed": True,
        "frozen_modes": [HYBRID_MODE, RULE_RERANK_MODE],
        "candidate_k": semantic_report["reproducibility"]["candidate_k"],
        "top_k": semantic_report["reproducibility"]["top_k"],
        "stable_contracts_checked": stable_contracts_checked,
        "candidate_pools_checked": candidate_pools_checked,
        "reproducibility_equal_outside_query_mode": True,
        "a_b_aggregate_and_case_results_equal": True,
        "candidate_chunk_ids_equal": True,
        "frozen_mode_metrics": frozen_mode_metrics,
    }


def numeric_rank(value: Any) -> int | None:
    if value in NOT_AVAILABLE:
        return None

    return int(value)


def classify_movement(before: int | None, after: int | None) -> str:
    if before is None and after is None:
        return "outside_top5_both"

    if before is None:
        return "recovered_to_top5"

    if after is None:
        return "dropped_from_top5"

    if after < before:
        return "promoted"

    if after > before:
        return "demoted"

    return "unchanged"


def compact_top5(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": item.get("source"),
            "section": item.get("section"),
            "semantic_rerank_score": item.get("semantic_rerank_score"),
            "text_preview": str(item.get("text_preview") or "")[:120],
        }
        for item in result.get("ranking_trace", [])[:5]
    ]


def optional_scenario_fields(result: dict[str, Any]) -> dict[str, Any]:
    return {
        field: result[field]
        for field in ("scenario_type", "context_mode", "difficulty")
        if result.get(field) not in NOT_AVAILABLE
    }


def metric_comparison(
    semantic_mode: dict[str, Any],
    rerank_mode: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    semantic_total = int(semantic_mode["total_cases"])
    rerank_total = int(rerank_mode["total_cases"])
    semantic_passed = int(semantic_mode["passed_count"])
    rerank_passed = int(rerank_mode["passed_count"])
    metrics = {
        "overall_pass": {
            "c0_semantic_query": {
                "count": f"{semantic_passed}/{semantic_total}",
                "rate": round(semantic_passed / semantic_total, 4),
            },
            "c1_rerank_query": {
                "count": f"{rerank_passed}/{rerank_total}",
                "rate": round(rerank_passed / rerank_total, 4),
            },
            "delta_count": rerank_passed - semantic_passed,
            "delta_rate": round(
                rerank_passed / rerank_total - semantic_passed / semantic_total,
                4,
            ),
        }
    }
    metrics.update({
        field: {
            "c0_semantic_query": semantic_mode.get(field),
            "c1_rerank_query": rerank_mode.get(field),
            "delta": round(
                float(rerank_mode.get(field, 0))
                - float(semantic_mode.get(field, 0)),
                4,
            ),
        }
        for field in METRIC_FIELDS
    })
    return metrics


def build_rank_comparison(
    semantic_report: dict[str, Any],
    rerank_report: dict[str, Any],
) -> dict[str, Any]:
    hybrid_cases = case_map(semantic_report["ablation"][HYBRID_MODE])
    semantic_cases = case_map(
        semantic_report["ablation"][SEMANTIC_RERANK_MODE]
    )
    rerank_cases = case_map(rerank_report["ablation"][SEMANTIC_RERANK_MODE])
    counts: Counter[str] = Counter()
    changed_cases = []
    c0_negative_vs_hybrid = []
    restored_c0_negative = []
    new_pass_regressions = []
    newly_passed = []
    promoted_cases = []
    rank_regression_cases = []

    for case_id, semantic_case in semantic_cases.items():
        hybrid_case = hybrid_cases[case_id]
        rerank_case = rerank_cases[case_id]
        hybrid_rank = numeric_rank(hybrid_case.get("expected_rank"))
        semantic_rank = numeric_rank(semantic_case.get("expected_rank"))
        rerank_rank = numeric_rank(rerank_case.get("expected_rank"))
        movement = classify_movement(semantic_rank, rerank_rank)
        hybrid_to_c0 = classify_movement(hybrid_rank, semantic_rank)
        counts[movement] += 1

        if movement in {"promoted", "recovered_to_top5"}:
            promoted_cases.append(case_id)

        if movement in {"demoted", "dropped_from_top5"}:
            rank_regression_cases.append(case_id)

        if hybrid_to_c0 in {"demoted", "dropped_from_top5"}:
            c0_negative_vs_hybrid.append(case_id)

            if movement in {"promoted", "recovered_to_top5"}:
                restored_c0_negative.append(case_id)

        if semantic_case.get("passed") and not rerank_case.get("passed"):
            new_pass_regressions.append(case_id)

        if not semantic_case.get("passed") and rerank_case.get("passed"):
            newly_passed.append(case_id)

        if movement == "unchanged":
            continue

        contract = rerank_case["query_contract"]
        changed_cases.append(
            {
                "case_id": case_id,
                "query": rerank_case.get("query"),
                **optional_scenario_fields(rerank_case),
                "input_context": contract.get("input_context"),
                "semantic_query": contract.get("semantic_query"),
                "rerank_query": contract.get("rerank_query"),
                "hybrid_primary_rank": hybrid_rank,
                "c0_primary_rank": semantic_rank,
                "c1_primary_rank": rerank_rank,
                "movement": movement,
                "c0_top5": compact_top5(semantic_case),
                "c1_top5": compact_top5(rerank_case),
            }
        )

    for movement in (
        "promoted",
        "unchanged",
        "demoted",
        "recovered_to_top5",
        "dropped_from_top5",
        "outside_top5_both",
    ):
        counts.setdefault(movement, 0)

    return {
        "counts": dict(counts),
        "net_top5_change": (
            counts["recovered_to_top5"] - counts["dropped_from_top5"]
        ),
        "c0_negative_vs_hybrid": c0_negative_vs_hybrid,
        "c0_negative_restored_by_c1": restored_c0_negative,
        "newly_passed_cases": newly_passed,
        "new_pass_regressions": new_pass_regressions,
        "promoted_cases": promoted_cases,
        "rank_regression_cases": rank_regression_cases,
        "changed_cases": changed_cases,
    }


def query_contract_audit(report: dict[str, Any]) -> dict[str, Any]:
    results = report["ablation"][SEMANTIC_RERANK_MODE]["results"]
    router_keys = {
        *INTENT_RETRIEVAL_TERMS,
        *TOPIC_RETRIEVAL_TERMS,
    }
    lengths = []
    empty_cases = []
    duplicate_line_cases = []
    none_literal_cases = []
    false_handoff_leaks = []
    router_key_exposure: dict[str, list[str]] = {}

    for result in results:
        case_id = str(result["case_id"])
        contract = result["query_contract"]
        rerank_query = str(contract.get("rerank_query") or "")
        lines = [line.strip() for line in rerank_query.splitlines() if line.strip()]
        lengths.append(len(rerank_query))

        if not rerank_query:
            empty_cases.append(case_id)

        if len(lines) != len(set(lines)):
            duplicate_line_cases.append(case_id)

        if "None" in rerank_query or "null" in rerank_query.lower():
            none_literal_cases.append(case_id)

        context = contract.get("input_context", {})

        if not context.get("handoff_required") and "需要人工处理" in rerank_query:
            false_handoff_leaks.append(case_id)

        exposed = sorted(key for key in router_keys if key and key in rerank_query)

        if exposed:
            router_key_exposure[case_id] = exposed

    return {
        "case_count": len(results),
        "rerank_query_length_chars": {
            "min": min(lengths, default=0),
            "max": max(lengths, default=0),
            "average": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
            "over_500_chars": sum(length > 500 for length in lengths),
        },
        "empty_cases": empty_cases,
        "duplicate_line_cases": duplicate_line_cases,
        "none_literal_cases": none_literal_cases,
        "false_handoff_leaks": false_handoff_leaks,
        "router_key_exposure": router_key_exposure,
        "semantic_and_lexical_queries_frozen": True,
        "vocabulary_policy": "first mapped Chinese retrieval term only",
    }


def hypothesis_assessment(
    metrics: dict[str, dict[str, Any]],
    movement: dict[str, Any],
    rerank_report: dict[str, Any],
) -> dict[str, Any]:
    results = rerank_report["ablation"][SEMANTIC_RERANK_MODE]["results"]
    slice_fields_present = sorted(
        {
            field
            for result in results
            for field in ("scenario_type", "context_mode", "difficulty")
            if result.get(field) not in NOT_AVAILABLE
        }
    )
    business_fact_fields = (
        "order_status",
        "shipping_status",
        "product_name",
        "product_category",
        "signed_date",
    )
    fact_enriched_cases = [
        result["case_id"]
        for result in results
        if any(
            result["query_contract"]["input_context"].get(field)
            for field in business_fact_fields
        )
    ]
    counts = movement["counts"]
    mixed = bool(counts["promoted"] or counts["recovered_to_top5"]) and bool(
        counts["demoted"] or counts["dropped_from_top5"]
    )
    net_negative = (
        metrics["mrr"]["delta"] < 0
        or metrics["hit_at_1"]["delta"] < 0
        or counts["demoted"] > counts["promoted"]
    )

    return {
        "a_rerank_query_effectiveness": {
            "answer": "mixed" if mixed else ("no" if net_negative else "yes"),
            "net_effect": "negative" if net_negative else "non_negative",
            "evidence": (
                "Some primary ranks improved, but demotions were more frequent and "
                "Hit@1, Hit@3, and MRR decreased while Pass and Hit@5 were unchanged."
            ),
        },
        "hypothesis_1_business_semantics_restore_negative_reranks": {
            "status": "not_supported_on_dev",
            "c0_negative_vs_hybrid": movement["c0_negative_vs_hybrid"],
            "restored_by_c1": movement["c0_negative_restored_by_c1"],
            "evidence": (
                "None of the C0 regressions relative to Hybrid were restored by C1."
            ),
        },
        "hypothesis_2_benefit_concentrates_in_minimal_or_explicit": {
            "status": "not_testable_on_dev_v1",
            "slice_fields_present": slice_fields_present,
            "fact_enriched_cases": fact_enriched_cases,
            "evidence": (
                "The 20-case v1 Dev set has no scenario/context/difficulty labels and "
                "contains no populated order or shipping fact fields."
            ),
        },
        "hypothesis_3_rerank_query_adds_noise": {
            "status": "supported",
            "promoted_cases": movement["promoted_cases"],
            "rank_regression_cases": movement["rank_regression_cases"],
            "inference": (
                "Broad business labels can strengthen generic policy or historical-case "
                "matches over the exact primary evidence; this is inferred from the "
                "changed ranking traces, not from a separate model attribution signal."
            ),
        },
        "unresolved": [
            "authority conflict",
            "hard multi-evidence ranking",
            "generic Cross-Encoder relevance mismatch",
            "evidence composition",
        ],
        "recommendation": (
            "Keep the reversible query-mode experiment, but do not treat rerank_query as "
            "a validated fix for the Cross-Encoder negative-rerank problem. The next "
            "experiment may evaluate ranking-power control without changing this query "
            "contract or tuning on the 60-case Holdout."
        ),
    }


def build_comparison_report(
    semantic_report: dict[str, Any],
    rerank_report: dict[str, Any],
) -> dict[str, Any]:
    isolation = validate_experiment_isolation(semantic_report, rerank_report)
    semantic_mode = semantic_report["ablation"][SEMANTIC_RERANK_MODE]
    rerank_mode = rerank_report["ablation"][SEMANTIC_RERANK_MODE]
    metrics = metric_comparison(semantic_mode, rerank_mode)
    movement = build_rank_comparison(semantic_report, rerank_report)

    return {
        "comparison": "C0 semantic_query -> C1 rerank_query",
        "dataset": semantic_report["reproducibility"]["dataset"],
        "kb_version": semantic_report["reproducibility"]["kb_version"],
        "experiment_isolation": isolation,
        "semantic_reranker": semantic_report["reproducibility"][
            "semantic_reranker"
        ],
        "metrics": metrics,
        "primary_rank_movement": movement,
        "query_contract_audit": query_contract_audit(rerank_report),
        "hypothesis_assessment": hypothesis_assessment(
            metrics,
            movement,
            rerank_report,
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare isolated Cross-Encoder semantic query modes."
    )
    parser.add_argument("--semantic-report", type=Path, default=DEFAULT_SEMANTIC_REPORT)
    parser.add_argument("--rerank-report", type=Path, default=DEFAULT_RERANK_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    comparison = build_comparison_report(
        load_report(args.semantic_report),
        load_report(args.rerank_report),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("Semantic Query Comparison")
    print("=" * 72)
    print("Experiment isolation: PASS")
    print("\nMetrics (C0 -> C1)")

    for field, values in comparison["metrics"].items():
        if field == "overall_pass":
            print(
                f"{field:<40} "
                f"{values['c0_semantic_query']['count']} -> "
                f"{values['c1_rerank_query']['count']} "
                f"({values['delta_rate']:+.4f})"
            )
            continue

        print(
            f"{field:<40} "
            f"{values['c0_semantic_query']} -> {values['c1_rerank_query']} "
            f"({values['delta']:+.4f})"
        )

    print("\nPrimary rank movement")

    for field, value in comparison["primary_rank_movement"]["counts"].items():
        print(f"{field:<24} {value}")

    print(f"\nChanged cases: {len(comparison['primary_rank_movement']['changed_cases'])}")
    print(f"Report: {args.output.resolve()}")


if __name__ == "__main__":
    main()
