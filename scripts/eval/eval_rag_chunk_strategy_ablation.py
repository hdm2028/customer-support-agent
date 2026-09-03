from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, KNOWLEDGE_DIR
from app.rag.embedding_client import EmbeddingProvider, get_embedding_provider
from app.rag.index_manager import RAGIndexManager
from app.rag.ingestion.chunker import (
    CHUNKER_VERSION,
    CHUNK_STRATEGIES,
    MAX_STRUCTURED_TOKENS,
)
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.query_context import RetrievalQuery
from app.rag.ranking import (
    HYBRID_MODE,
    RANKING_MODES,
    RULE_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
    SEMANTIC_FUSION_MODE,
    SEMANTIC_FUSION_RETRIEVAL_WEIGHT,
    SEMANTIC_FUSION_SEMANTIC_WEIGHT,
    SEMANTIC_QUERY_MODE,
    SEMANTIC_RERANK_MODE,
)
from app.rag.retriever import HybridRetriever
from app.rag.semantic_reranker import SemanticReranker, build_semantic_reranker
from scripts.eval.common import load_jsonl
from scripts.eval.eval_rag import (
    CANDIDATE_K,
    build_comparison,
    build_rank_movement_report,
    build_slice_summary,
    dataset_identity,
    reproducibility_metadata,
    run_ablation,
    validate_cases,
    validate_dataset_ground_truth,
)


DATASET_PATH = BASE_DIR / "data" / "eval" / "rag_eval_holdout.jsonl"
OUTPUT_DIR = BASE_DIR / "reports" / "rag_chunk_strategy_ablation"
TOP_K = 5
EXPECTED_CASES = 60
EXPECTED_KNOWLEDGE_SOURCES = 15
STRATEGY_ORDER = tuple(CHUNK_STRATEGIES)
MODE_LABELS = {
    HYBRID_MODE: "A",
    RULE_RERANK_MODE: "B",
    SEMANTIC_RERANK_MODE: "C",
    SEMANTIC_CONSTRAINT_MODE: "D",
    SEMANTIC_FUSION_MODE: "E",
}
METRIC_FIELDS = (
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "mrr",
    "candidate_evidence_recall_at_20",
    "evidence_coverage_rate",
)
MOVEMENT_NAMES = (
    "promoted",
    "unchanged",
    "demoted",
    "recovered_to_top5",
    "dropped_from_top5",
    "outside_top5_both",
)
STRATEGY_IMPLEMENTATIONS = {
    "fixed_128": {
        "method": "section-aware fixed token windows",
        "behavior": "128-token windows with 16-token overlap within each source section",
        "section_behavior": (
            "preserves RawDocument.section or uses each Markdown heading title as "
            "chunk.section"
        ),
        "implementation": "app/rag/ingestion/chunker.py:_fixed_token_chunks",
    },
    "fixed_256": {
        "method": "section-aware fixed token windows",
        "behavior": "256-token windows with 32-token overlap within each source section",
        "section_behavior": (
            "preserves RawDocument.section or uses each Markdown heading title as "
            "chunk.section"
        ),
        "implementation": "app/rag/ingestion/chunker.py:_fixed_token_chunks",
    },
    "fixed_512": {
        "method": "section-aware fixed token windows",
        "behavior": "512-token windows with 64-token overlap within each source section",
        "section_behavior": (
            "preserves RawDocument.section or uses each Markdown heading title as "
            "chunk.section"
        ),
        "implementation": "app/rag/ingestion/chunker.py:_fixed_token_chunks",
    },
    "markdown": {
        "method": "Markdown heading sections",
        "behavior": (
            "split on Markdown headings at all levels; oversized sections split by "
            "paragraph/sentence up to 700 tokens"
        ),
        "section_behavior": "uses each Markdown heading title as chunk.section",
        "implementation": "app/rag/ingestion/chunker.py:_section_blocks",
    },
    "type_aware": {
        "method": "top-level structured sections",
        "behavior": (
            "split on level-2 Markdown headings; oversized sections split by "
            "paragraph/sentence up to 700 tokens"
        ),
        "section_behavior": "uses each level-2 Markdown heading title as chunk.section",
        "implementation": "app/rag/ingestion/chunker.py:_section_blocks",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


class UncachedExperimentRetriever(HybridRetriever):
    """Use the production active index while bypassing external candidate caches."""

    def retrieve_candidates(
        self,
        query: RetrievalQuery,
        *,
        candidate_k: int,
    ) -> list[dict]:
        index = self.index_manager.get_active_index()

        if index is None or candidate_k <= 0:
            return []

        return index.search(query, candidate_k=candidate_k)


@dataclass
class CountingSemanticReranker:
    delegate: SemanticReranker
    calls: int = 0

    @property
    def identity(self):
        return self.delegate.identity

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        self.calls += 1
        return self.delegate.rerank(query, candidates)


def strategy_definition(name: str) -> dict[str, Any]:
    registered = CHUNK_STRATEGIES[name]
    definition = {
        "chunk_strategy": name,
        "registry": "app/rag/ingestion/chunker.py:CHUNK_STRATEGIES",
        "call": (
            "KnowledgeIngestionService(chunk_strategy=<name>) -> "
            "chunk_documents(..., chunk_strategy=<name>)"
        ),
        "registered_limit": registered.max_chars,
        "registered_overlap": registered.overlap,
        "limit_unit": "tokens",
        **STRATEGY_IMPLEMENTATIONS[name],
    }

    if name in {"markdown", "type_aware"}:
        definition["oversized_section_limit_tokens"] = MAX_STRUCTURED_TOKENS

    return definition


def compact_mode_metrics(mode_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "overall_pass": (
            f"{mode_report['passed_count']}/{mode_report['total_cases']}"
        ),
        "passed_count": mode_report["passed_count"],
        "total_cases": mode_report["total_cases"],
        **{field: mode_report[field] for field in METRIC_FIELDS},
    }


def compact_movement(movement: dict[str, Any]) -> dict[str, int]:
    counts = movement["counts"]
    return {
        name: int(counts.get(name, 0))
        for name in MOVEMENT_NAMES
    } | {"net_top5": int(movement["net_top5_change"])}


def validate_candidate_pool_identity(mode_reports: dict[str, dict]) -> None:
    baseline = {
        result["case_id"]: result["candidate_chunk_ids"]
        for result in mode_reports[HYBRID_MODE]["results"]
    }

    for mode in RANKING_MODES:
        results = mode_reports[mode]["results"]

        if len(results) != len(baseline):
            raise ValueError(f"Case count differs for ranking mode {mode}")

        for result in results:
            case_id = result["case_id"]

            if baseline.get(case_id) != result["candidate_chunk_ids"]:
                raise ValueError(
                    f"Candidate Top{CANDIDATE_K} differs for {mode}/{case_id}"
                )


def source_snapshot(service: KnowledgeIngestionService) -> tuple[tuple[str, str], ...]:
    discovery = service.scan()
    snapshot = tuple(
        (source.source, source.content_hash)
        for source in discovery.sources
    )

    if len(snapshot) != EXPECTED_KNOWLEDGE_SOURCES:
        raise ValueError(
            "Chunk ablation requires exactly "
            f"{EXPECTED_KNOWLEDGE_SOURCES} knowledge sources; got {len(snapshot)}"
        )

    return snapshot


def run_strategy(
    *,
    strategy_name: str,
    cases: list[dict],
    dataset_path: Path,
    knowledge_dir: Path,
    output_dir: Path,
    embedding_provider: EmbeddingProvider,
    semantic_reranker: CountingSemanticReranker,
    expected_sources: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    strategy_dir = output_dir / strategy_name
    service = KnowledgeIngestionService(
        knowledge_dir,
        manifest_path=strategy_dir / "knowledge_manifest.json",
        chunk_strategy=strategy_name,
    )
    current_sources = source_snapshot(service)

    if current_sources != expected_sources:
        raise ValueError(f"Knowledge source set changed for {strategy_name}")

    manager = RAGIndexManager(
        ingestion=service,
        embedding_provider=embedding_provider,
    )
    refresh = manager.refresh()
    index = refresh.active_index

    if refresh.chunk_count != index.vector_store.size():
        raise ValueError(f"Index size mismatch for {strategy_name}")

    if refresh.reused_count != 0 or refresh.embedded_count != refresh.chunk_count:
        raise ValueError(f"Index was not rebuilt from a fresh active store for {strategy_name}")

    if any(
        chunk.metadata.get("chunk_strategy") != strategy_name
        for chunk in index.chunks
    ):
        raise ValueError(f"Mixed chunk strategies found in {strategy_name} index")

    calls_before = semantic_reranker.calls
    mode_reports = run_ablation(
        cases,
        UncachedExperimentRetriever(manager),
        top_k=TOP_K,
        semantic_reranker=semantic_reranker,
        semantic_query_mode=SEMANTIC_QUERY_MODE,
    )
    semantic_calls = semantic_reranker.calls - calls_before

    if semantic_calls != len(cases):
        raise ValueError(
            f"Expected one Cross-Encoder call per case for {strategy_name}; "
            f"got {semantic_calls}"
        )

    validate_candidate_pool_identity(mode_reports)
    comparison, deltas = build_comparison(mode_reports)
    report = {
        "schema_version": "rag-chunk-strategy-ablation-v1",
        "created_at": utc_now(),
        "chunk_strategy": strategy_name,
        "chunk_strategy_definition": strategy_definition(strategy_name),
        "chunker_version": CHUNKER_VERSION,
        "chunk_count": refresh.chunk_count,
        "knowledge_source_count": len(current_sources),
        "knowledge_sources": [
            {"source": source, "content_hash": content_hash}
            for source, content_hash in current_sources
        ],
        "kb_version": refresh.kb_version,
        "index_build": {
            "manifest_path": str(service.manifest_path.resolve()),
            "embedded_count": refresh.embedded_count,
            "reused_from_active_index": refresh.reused_count,
            "vector_store_size": index.vector_store.size(),
            "new_manager_per_strategy": True,
            "candidate_cache_bypassed": True,
            "candidate_pool_shared_across_modes": True,
            "chunk_ids": [chunk.chunk_id for chunk in index.chunks],
        },
        "dataset": dataset_identity(dataset_path),
        "dataset_cases": len(cases),
        "candidate_k": CANDIDATE_K,
        "top_k": TOP_K,
        "semantic_query_mode": SEMANTIC_QUERY_MODE,
        "semantic_calls": semantic_calls,
        "semantic_fusion": {
            "retrieval_weight": SEMANTIC_FUSION_RETRIEVAL_WEIGHT,
            "semantic_weight": SEMANTIC_FUSION_SEMANTIC_WEIGHT,
            "reuses_c_semantic_scores": True,
        },
        "reproducibility": reproducibility_metadata(
            manager=manager,
            semantic_reranker=semantic_reranker,
            top_k=TOP_K,
            dataset_path=dataset_path,
            semantic_query_mode=SEMANTIC_QUERY_MODE,
        ),
        "comparison": comparison,
        "deltas": deltas,
        "ablation": mode_reports,
        "slice_summary": build_slice_summary(mode_reports),
        "rank_movement": build_rank_movement_report(mode_reports),
    }
    write_json(strategy_dir / "result.json", report)
    return report


def best_strategies(rows: list[dict[str, Any]], metric: str) -> list[str]:
    best_value = max(float(row[metric]) for row in rows)
    return [
        row["chunk_strategy"]
        for row in rows
        if float(row[metric]) == best_value
    ]


def build_summary(
    reports: list[dict[str, Any]],
    *,
    dataset_validation: dict[str, Any],
) -> dict[str, Any]:
    chunk_rows = []
    ranking_rows = []
    movement_summary = {}

    for report in reports:
        strategy = report["chunk_strategy"]
        hybrid = compact_mode_metrics(report["ablation"][HYBRID_MODE])
        hybrid_results = report["ablation"][HYBRID_MODE]["results"]
        candidate_primary_hits = sum(
            result.get("candidate_expected_rank") not in {None, "N/A"}
            for result in hybrid_results
        )
        chunk_rows.append(
            {
                "chunk_strategy": strategy,
                "chunk_count": report["chunk_count"],
                "kb_version": report["kb_version"],
                "candidate_primary_target_hits_at_20": candidate_primary_hits,
                **hybrid,
            }
        )

        for mode in RANKING_MODES:
            ranking_rows.append(
                {
                    "chunk_strategy": strategy,
                    "mode": MODE_LABELS[mode],
                    "mode_name": mode,
                    **compact_mode_metrics(report["ablation"][mode]),
                }
            )

        movement_summary[strategy] = {
            comparison: compact_movement(report["rank_movement"][comparison])
            for comparison in ("A_to_B", "A_to_C", "C_to_D", "A_to_E", "C_to_E")
        }

    leaders = {
        metric: best_strategies(chunk_rows, metric)
        for metric in (
            "candidate_evidence_recall_at_20",
            "hit_at_1",
            "hit_at_3",
            "hit_at_5",
            "mrr",
            "evidence_coverage_rate",
            "passed_count",
        )
    }
    common_leaders = set(STRATEGY_ORDER)

    for strategy_names in leaders.values():
        common_leaders.intersection_update(strategy_names)

    c_rows = {row["chunk_strategy"]: row for row in ranking_rows if row["mode"] == "C"}
    e_rows = {row["chunk_strategy"]: row for row in ranking_rows if row["mode"] == "E"}
    evaluable_strategies = [
        row["chunk_strategy"]
        for row in chunk_rows
        if row["candidate_primary_target_hits_at_20"] > 0
    ]
    fusion_consistency = {}

    for strategy in STRATEGY_ORDER:
        a_to_c = movement_summary[strategy]["A_to_C"]
        a_to_e = movement_summary[strategy]["A_to_E"]
        c_negative = a_to_c["demoted"] + a_to_c["dropped_from_top5"]
        e_negative = a_to_e["demoted"] + a_to_e["dropped_from_top5"]
        fusion_consistency[strategy] = {
            "a_to_c_negative_movements": c_negative,
            "a_to_e_negative_movements": e_negative,
            "negative_movement_reduction": c_negative - e_negative,
            "reduces_negative_movements": e_negative < c_negative,
            "e_minus_c_mrr": round(e_rows[strategy]["mrr"] - c_rows[strategy]["mrr"], 4),
            "e_minus_c_hit_at_5": round(
                e_rows[strategy]["hit_at_5"] - c_rows[strategy]["hit_at_5"],
                4,
            ),
        }

    c_metric_ranges = {
        metric: round(
            max(float(row[metric]) for row in c_rows.values())
            - min(float(row[metric]) for row in c_rows.values()),
            4,
        )
        for metric in METRIC_FIELDS
    }
    evaluable_c_metric_ranges = (
        {
            metric: round(
                max(float(c_rows[strategy][metric]) for strategy in evaluable_strategies)
                - min(float(c_rows[strategy][metric]) for strategy in evaluable_strategies),
                4,
            )
            for metric in METRIC_FIELDS
        }
        if evaluable_strategies
        else {}
    )
    section_contract = {
        strategy: {
            "status": "compatible",
            "candidate_primary_target_hits_at_20": next(
                row["candidate_primary_target_hits_at_20"]
                for row in chunk_rows
                if row["chunk_strategy"] == strategy
            ),
            "reason": (
                "The strategy emits named source sections used by Holdout v2 targets; "
                "candidate misses are measured retrieval outcomes."
            ),
        }
        for strategy in STRATEGY_ORDER
    }

    return {
        "schema_version": "rag-chunk-strategy-ablation-summary-v1",
        "created_at": utc_now(),
        "dataset_validation": dataset_validation,
        "experiment": {
            "chunk_strategies": list(STRATEGY_ORDER),
            "ranking_modes": MODE_LABELS,
            "candidate_k": CANDIDATE_K,
            "top_k": TOP_K,
            "semantic_query_mode": SEMANTIC_QUERY_MODE,
            "semantic_fusion": {
                "retrieval_weight": SEMANTIC_FUSION_RETRIEVAL_WEIGHT,
                "semantic_weight": SEMANTIC_FUSION_SEMANTIC_WEIGHT,
            },
        },
        "chunk_strategy_definitions": [
            strategy_definition(name)
            for name in STRATEGY_ORDER
        ],
        "chunk_strategy_summary_a_hybrid": chunk_rows,
        "ranking_summary": ranking_rows,
        "rank_movement": movement_summary,
        "objective_analysis": {
            "a_hybrid_metric_leaders": leaders,
            "one_strategy_leads_all_a_metrics": sorted(common_leaders),
            "recall_ranking_coverage_tradeoff": not bool(common_leaders),
            "cross_encoder_metric_ranges_across_chunk_strategies": c_metric_ranges,
            "section_aware_ground_truth_compatibility": section_contract,
            "evaluable_strategies": evaluable_strategies,
            "cross_encoder_metric_ranges_across_evaluable_strategies": (
                evaluable_c_metric_ranges
            ),
            "fusion_consistency": fusion_consistency,
            "fusion_reduces_negative_movements_for_all_evaluable_strategies": all(
                item["reduces_negative_movements"]
                for strategy, item in fusion_consistency.items()
                if strategy in evaluable_strategies
            ),
            "fusion_reduces_negative_movements_for_all_registered_strategies": all(
                item["reduces_negative_movements"]
                for item in fusion_consistency.values()
            ),
        },
    }


def metric_text(value: Any) -> str:
    return f"{float(value):.4f}"


def print_results(summary: dict[str, Any]) -> None:
    print("\nChunk Strategy Summary (A = Hybrid)")
    print("=" * 116)
    print(
        f"{'Strategy':<14}{'Chunks':>8}{'Pass':>9}{'H@1':>9}{'H@3':>9}"
        f"{'H@5':>9}{'MRR':>9}{'Recall@20':>12}{'Coverage@5':>13}"
    )

    for row in summary["chunk_strategy_summary_a_hybrid"]:
        print(
            f"{row['chunk_strategy']:<14}{row['chunk_count']:>8}"
            f"{row['overall_pass']:>9}{metric_text(row['hit_at_1']):>9}"
            f"{metric_text(row['hit_at_3']):>9}{metric_text(row['hit_at_5']):>9}"
            f"{metric_text(row['mrr']):>9}"
            f"{metric_text(row['candidate_evidence_recall_at_20']):>12}"
            f"{metric_text(row['evidence_coverage_rate']):>13}"
        )

    print("\nChunk Strategy x Ranking Mode")
    print("=" * 112)
    print(
        f"{'Strategy':<14}{'Mode':<6}{'Pass':>9}{'H@1':>9}{'H@3':>9}"
        f"{'H@5':>9}{'MRR':>9}{'Recall@20':>12}{'Coverage@5':>13}"
    )

    for row in summary["ranking_summary"]:
        print(
            f"{row['chunk_strategy']:<14}{row['mode']:<6}{row['overall_pass']:>9}"
            f"{metric_text(row['hit_at_1']):>9}{metric_text(row['hit_at_3']):>9}"
            f"{metric_text(row['hit_at_5']):>9}{metric_text(row['mrr']):>9}"
            f"{metric_text(row['candidate_evidence_recall_at_20']):>12}"
            f"{metric_text(row['evidence_coverage_rate']):>13}"
        )

    print("\nRank Movement")
    print("=" * 116)
    print(
        f"{'Strategy':<14}{'Move':<8}{'Prom':>7}{'Same':>7}{'Demo':>7}"
        f"{'Recov':>8}{'Drop':>7}{'Outside':>9}{'NetTop5':>9}"
    )

    for strategy, comparisons in summary["rank_movement"].items():
        for comparison in ("A_to_C", "A_to_E"):
            movement = comparisons[comparison]
            print(
                f"{strategy:<14}{comparison:<8}{movement['promoted']:>7}"
                f"{movement['unchanged']:>7}{movement['demoted']:>7}"
                f"{movement['recovered_to_top5']:>8}"
                f"{movement['dropped_from_top5']:>7}"
                f"{movement['outside_top5_both']:>9}{movement['net_top5']:>9}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run all registered chunk strategies against A/B/C/D/E on Holdout."
    )
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--knowledge-dir", type=Path, default=KNOWLEDGE_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="Rebuild compact summary.json from existing per-strategy result files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset_path = args.dataset.resolve()
    knowledge_dir = args.knowledge_dir.resolve()
    output_dir = args.output_dir.resolve()
    cases = load_jsonl(dataset_path)
    validate_cases(cases)

    if len(cases) != EXPECTED_CASES or {case.get("split") for case in cases} != {
        "holdout"
    }:
        raise ValueError(
            f"Experiment requires exactly {EXPECTED_CASES} Holdout cases"
        )

    dataset_validation = validate_dataset_ground_truth(
        cases=cases,
        dataset_path=dataset_path,
        knowledge_dir=knowledge_dir,
    )

    if not dataset_validation["passed"]:
        raise ValueError("Holdout dataset or ground truth validation failed")

    if args.summarize_only:
        reports = [
            json.loads(
                (output_dir / strategy / "result.json").read_text(encoding="utf-8")
            )
            for strategy in STRATEGY_ORDER
        ]
        summary = build_summary(
            reports,
            dataset_validation=dataset_validation,
        )
        write_json(output_dir / "summary.json", summary)
        print_results(summary)
        print(f"\nSummary: {(output_dir / 'summary.json').resolve()}")
        return

    baseline_service = KnowledgeIngestionService(
        knowledge_dir,
        manifest_path=output_dir / "source_validation_manifest.json",
        chunk_strategy=STRATEGY_ORDER[0],
    )
    expected_sources = source_snapshot(baseline_service)
    embedding_provider = get_embedding_provider()
    semantic_reranker = CountingSemanticReranker(build_semantic_reranker())
    reports = []
    seen_kb_versions = set()
    seen_chunk_ids = set()

    for strategy_name in STRATEGY_ORDER:
        print(f"Running {strategy_name} ...", flush=True)
        report = run_strategy(
            strategy_name=strategy_name,
            cases=cases,
            dataset_path=dataset_path,
            knowledge_dir=knowledge_dir,
            output_dir=output_dir,
            embedding_provider=embedding_provider,
            semantic_reranker=semantic_reranker,
            expected_sources=expected_sources,
        )
        strategy_chunk_ids = set(report["index_build"]["chunk_ids"])

        if report["kb_version"] in seen_kb_versions:
            raise ValueError("Chunk strategies produced a duplicate kb_version")

        if seen_chunk_ids & strategy_chunk_ids:
            raise ValueError("Chunk strategies reused chunk identities")

        seen_kb_versions.add(report["kb_version"])
        seen_chunk_ids.update(strategy_chunk_ids)
        reports.append(report)

    summary = build_summary(
        reports,
        dataset_validation=dataset_validation,
    )
    write_json(output_dir / "summary.json", summary)
    print_results(summary)
    print(f"\nSummary: {(output_dir / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
