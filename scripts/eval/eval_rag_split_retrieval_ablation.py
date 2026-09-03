from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, KNOWLEDGE_DIR
from app.rag.embedding_client import keyword_score
from app.rag.hybrid_index import HybridRAGIndex, normalize_score
from app.rag.index_manager import RAGIndexManager
from app.rag.ingestion.chunker import CHUNK_STRATEGIES
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.query_builder import build_retrieval_query
from app.rag.query_context import RetrievalQuery
from app.rag.ranking import (
    HYBRID_MODE,
    SEMANTIC_FUSION_MODE,
    SEMANTIC_FUSION_RETRIEVAL_WEIGHT,
    SEMANTIC_FUSION_SEMANTIC_WEIGHT,
    SEMANTIC_QUERY_MODE,
    SEMANTIC_RERANK_MODE,
    _final_ranked,
    _retrieval_ranked,
    _semantic_fusion_ranked,
    build_evidence_constraint,
)
from app.rag.semantic_reranker import SemanticReranker, build_semantic_reranker
from scripts.eval.common import load_jsonl, now_iso
from scripts.eval.eval_rag import (
    CANDIDATE_K,
    case_query_context,
    compare_mode_rank_movement,
    dataset_identity,
    reproducibility_metadata,
    score_mode,
    validate_cases,
    validate_dataset_ground_truth,
    build_mode_report,
)


DATASET_PATH = BASE_DIR / "data" / "eval" / "rag_eval_holdout.jsonl"
OUTPUT_DIR = BASE_DIR / "reports" / "rag_split_retrieval_ablation"
CHUNK_STRATEGY = "fixed_512"
TOP_K = 5
VECTOR_TOP_K = 10
LEXICAL_TOP_K = 10
EXPANDED_VECTOR_TOP_K = 15
EXPANDED_LEXICAL_TOP_K = 15
EXPECTED_CASES = 60

SPLIT_SEMANTIC_MODE = "split_union_semantic"
SPLIT_FUSION_MODE = "split_union_semantic_fusion"
EXPANDED_SPLIT_SEMANTIC_MODE = "expanded_split_union_semantic"
EXPANDED_SPLIT_FUSION_MODE = "expanded_split_union_semantic_fusion"
EXPERIMENT_MODES = (
    HYBRID_MODE,
    SEMANTIC_RERANK_MODE,
    SEMANTIC_FUSION_MODE,
    SPLIT_SEMANTIC_MODE,
    SPLIT_FUSION_MODE,
    EXPANDED_SPLIT_SEMANTIC_MODE,
    EXPANDED_SPLIT_FUSION_MODE,
)
MODE_LABELS = {
    HYBRID_MODE: "A",
    SEMANTIC_RERANK_MODE: "C",
    SEMANTIC_FUSION_MODE: "E",
    SPLIT_SEMANTIC_MODE: "F",
    SPLIT_FUSION_MODE: "G",
    EXPANDED_SPLIT_SEMANTIC_MODE: "H",
    EXPANDED_SPLIT_FUSION_MODE: "I",
}
METRIC_FIELDS = (
    "passed_count",
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "mrr",
    "candidate_evidence_recall_at_20",
    "evidence_coverage_rate",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


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


def _lexical_weights(index: HybridRAGIndex) -> tuple[float, float]:
    _, bm25_weight, keyword_weight = index.normalized_weights()
    total = bm25_weight + keyword_weight

    if total <= 0:
        return 0.5, 0.5

    return bm25_weight / total, keyword_weight / total


def build_split_union_candidates(
    index: HybridRAGIndex,
    query: RetrievalQuery,
    *,
    vector_top_k: int = VECTOR_TOP_K,
    lexical_top_k: int = LEXICAL_TOP_K,
) -> tuple[list[dict], dict[str, Any]]:
    """Build the experiment-only Vector/Lexical union without ground truth."""

    if vector_top_k <= 0 or lexical_top_k <= 0:
        raise ValueError("vector_top_k and lexical_top_k must be positive")

    query_vector = index.embedding_provider.embed_query(query.semantic_query)
    vector_results = index.vector_store.search(
        query_vector=query_vector,
        top_k=index.vector_store.size(),
    )
    vector_scores = {
        record.chunk.chunk_id: float(score)
        for record, score in vector_results
    }

    raw_results = []
    for item_index, item in enumerate(index.items):
        chunk = item["chunk"]
        raw_results.append(
            {
                "chunk": chunk,
                "semantic_score": vector_scores.get(chunk.chunk_id, 0.0),
                "bm25_score": float(
                    index.bm25_index.score(query.lexical_query, item_index)
                ),
                "keyword_score": float(
                    keyword_score(
                        query.lexical_query,
                        chunk.source,
                        chunk.text,
                    )
                ),
            }
        )

    max_semantic = max(
        (max(item["semantic_score"], 0.0) for item in raw_results),
        default=0.0,
    )
    max_bm25 = max(
        (item["bm25_score"] for item in raw_results),
        default=0.0,
    )
    max_keyword = max(
        (item["keyword_score"] for item in raw_results),
        default=0.0,
    )
    semantic_weight, bm25_weight, keyword_weight = index.normalized_weights()
    lexical_bm25_weight, lexical_keyword_weight = _lexical_weights(index)

    scored_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        chunk = item["chunk"]
        semantic_norm = normalize_score(item["semantic_score"], max_semantic)
        bm25_norm = normalize_score(item["bm25_score"], max_bm25)
        keyword_norm = normalize_score(item["keyword_score"], max_keyword)
        lexical_score = (
            lexical_bm25_weight * bm25_norm
            + lexical_keyword_weight * keyword_norm
        )
        hybrid_score = (
            semantic_weight * semantic_norm
            + bm25_weight * bm25_norm
            + keyword_weight * keyword_norm
        )
        scored_by_id[chunk.chunk_id] = {
            "chunk": chunk,
            "semantic_score": item["semantic_score"],
            "semantic_norm_score": semantic_norm,
            "bm25_score": item["bm25_score"],
            "bm25_norm_score": bm25_norm,
            "keyword_score": item["keyword_score"],
            "keyword_norm_score": keyword_norm,
            "lexical_score": lexical_score,
            "hybrid_score": hybrid_score,
        }

    vector_ids = [
        record.chunk.chunk_id
        for record, _ in vector_results[:vector_top_k]
    ]
    lexical_ranked = sorted(
        (
            item
            for item in scored_by_id.values()
            if item["lexical_score"] > 0
        ),
        key=lambda item: item["lexical_score"],
        reverse=True,
    )
    lexical_ids = [
        item["chunk"].chunk_id
        for item in lexical_ranked[:lexical_top_k]
    ]
    vector_ranks = {
        chunk_id: rank
        for rank, chunk_id in enumerate(vector_ids, start=1)
    }
    lexical_ranks = {
        chunk_id: rank
        for rank, chunk_id in enumerate(lexical_ids, start=1)
    }

    union_ids = list(dict.fromkeys(vector_ids + lexical_ids))
    union_candidates = []
    for chunk_id in union_ids:
        item = scored_by_id[chunk_id]
        chunk_data = item["chunk"].to_dict()
        branches = []
        if chunk_id in vector_ranks:
            branches.append("vector")
        if chunk_id in lexical_ranks:
            branches.append("lexical")

        chunk_data.update(
            {
                "retrieval_mode": (
                    f"vector{vector_top_k}_lexical{lexical_top_k}_union"
                ),
                "score": round(item["hybrid_score"], 6),
                "retrieval_score": round(item["hybrid_score"], 6),
                "hybrid_score": round(item["hybrid_score"], 6),
                "vector_score": round(item["semantic_score"], 6),
                "semantic_score": round(item["semantic_score"], 6),
                "semantic_norm_score": round(item["semantic_norm_score"], 6),
                "bm25_score": round(item["bm25_score"], 6),
                "bm25_norm_score": round(item["bm25_norm_score"], 6),
                "keyword_score": round(item["keyword_score"], 6),
                "keyword_norm_score": round(item["keyword_norm_score"], 6),
                "lexical_score": round(item["lexical_score"], 6),
                "vector_rank": vector_ranks.get(chunk_id),
                "lexical_rank": lexical_ranks.get(chunk_id),
                "union_branches": branches,
                "retrieval_weights": {
                    "semantic": round(semantic_weight, 6),
                    "bm25": round(bm25_weight, 6),
                    "keyword": round(keyword_weight, 6),
                },
                "lexical_weights": {
                    "bm25": round(lexical_bm25_weight, 6),
                    "keyword": round(lexical_keyword_weight, 6),
                },
            }
        )
        union_candidates.append(chunk_data)

    union_candidates.sort(
        key=lambda item: item["retrieval_score"],
        reverse=True,
    )
    union_candidates = [
        {**candidate, "retrieval_rank": rank}
        for rank, candidate in enumerate(union_candidates, start=1)
    ]
    diagnostics = {
        "vector_top_k": vector_top_k,
        "lexical_top_k": lexical_top_k,
        "vector_candidate_count": len(vector_ids),
        "lexical_candidate_count": len(lexical_ids),
        "candidate_count": len(union_candidates),
        "overlap_count": len(set(vector_ids) & set(lexical_ids)),
        "vector_chunk_ids": vector_ids,
        "lexical_chunk_ids": lexical_ids,
        "union_chunk_ids": [item["chunk_id"] for item in union_candidates],
    }
    return union_candidates, diagnostics


def _project_semantic_ranking(
    retrieval_candidates: list[dict],
    semantic_by_id: dict[str, dict],
) -> list[dict]:
    projected = []
    for candidate in retrieval_candidates:
        semantic = semantic_by_id[candidate["chunk_id"]]
        projected.append(
            {
                **candidate,
                "semantic_rerank_score": semantic["semantic_rerank_score"],
                "semantic_reranker": semantic.get("semantic_reranker"),
            }
        )

    projected.sort(
        key=lambda item: (
            float(item["semantic_rerank_score"]),
            float(item.get("retrieval_score", 0.0)),
        ),
        reverse=True,
    )
    return _final_ranked(
        [
            {**candidate, "semantic_rank": rank}
            for rank, candidate in enumerate(projected, start=1)
        ]
    )


def build_shared_semantic_rankings(
    query: RetrievalQuery,
    hybrid_candidates: list[dict],
    union_candidates: list[dict],
    *,
    semantic_reranker: SemanticReranker,
    expanded_union_candidates: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Score every experiment candidate pool in one Cross-Encoder call."""

    hybrid_retrieval = _retrieval_ranked(hybrid_candidates)
    union_retrieval = _retrieval_ranked(union_candidates)
    expanded_union_retrieval = _retrieval_ranked(
        expanded_union_candidates or []
    )
    combined = []
    seen_chunk_ids = set()

    for candidate in (
        hybrid_retrieval + union_retrieval + expanded_union_retrieval
    ):
        chunk_id = candidate["chunk_id"]
        if chunk_id not in seen_chunk_ids:
            combined.append(candidate)
            seen_chunk_ids.add(chunk_id)

    semantic_all = semantic_reranker.rerank(query.semantic_query, combined)
    semantic_by_id = {
        candidate["chunk_id"]: candidate
        for candidate in semantic_all
    }
    hybrid_semantic = _project_semantic_ranking(
        hybrid_retrieval,
        semantic_by_id,
    )
    union_semantic = _project_semantic_ranking(
        union_retrieval,
        semantic_by_id,
    )
    rankings = {
        HYBRID_MODE: _final_ranked(hybrid_retrieval),
        SEMANTIC_RERANK_MODE: hybrid_semantic,
        SEMANTIC_FUSION_MODE: _final_ranked(
            _semantic_fusion_ranked(hybrid_retrieval, hybrid_semantic)
        ),
        SPLIT_SEMANTIC_MODE: union_semantic,
        SPLIT_FUSION_MODE: _final_ranked(
            _semantic_fusion_ranked(union_retrieval, union_semantic)
        ),
    }
    if expanded_union_candidates is not None:
        expanded_union_semantic = _project_semantic_ranking(
            expanded_union_retrieval,
            semantic_by_id,
        )
        rankings[EXPANDED_SPLIT_SEMANTIC_MODE] = expanded_union_semantic
        rankings[EXPANDED_SPLIT_FUSION_MODE] = _final_ranked(
            _semantic_fusion_ranked(
                expanded_union_retrieval,
                expanded_union_semantic,
            )
        )

    return rankings


def _query_contract(context, query: RetrievalQuery) -> dict[str, Any]:
    return {
        "raw_query": context.raw_query,
        "semantic_query": query.semantic_query,
        "lexical_query": query.lexical_query,
        "rerank_query": query.rerank_query,
        "semantic_query_mode": SEMANTIC_QUERY_MODE,
        "semantic_reranker_input": query.semantic_query,
        "input_context": {
            "primary_intent": context.primary_intent,
            "action_type": context.action_type,
            "topic": context.topic,
            "related_topics": list(context.related_topics),
            "order_status": context.order_status,
            "shipping_status": context.shipping_status,
            "product_name": context.product_name,
            "product_category": context.product_category,
            "signed_date": context.signed_date,
            "handoff_required": context.handoff_required,
        },
    }


def _candidate_count_stats(results: list[dict]) -> dict[str, float | int]:
    counts = [int(result["candidate_count"]) for result in results]
    return {
        "average": round(sum(counts) / len(counts), 4) if counts else 0.0,
        "minimum": min(counts, default=0),
        "maximum": max(counts, default=0),
    }


def build_new_primary_evidence_analysis(
    mode_reports: dict[str, dict],
    *,
    semantic_mode: str = SPLIT_SEMANTIC_MODE,
    fusion_mode: str = SPLIT_FUSION_MODE,
) -> dict[str, Any]:
    hybrid_by_case = {
        result["case_id"]: result
        for result in mode_reports[HYBRID_MODE]["results"]
    }
    f_by_case = {
        result["case_id"]: result
        for result in mode_reports[semantic_mode]["results"]
    }
    g_by_case = {
        result["case_id"]: result
        for result in mode_reports[fusion_mode]["results"]
    }
    case_ids = [
        case_id
        for case_id, hybrid in hybrid_by_case.items()
        if not isinstance(hybrid.get("candidate_expected_rank"), (int, float))
        and isinstance(f_by_case[case_id].get("candidate_expected_rank"), (int, float))
    ]
    f_top5 = [
        case_id
        for case_id in case_ids
        if isinstance(f_by_case[case_id].get("expected_rank"), (int, float))
    ]
    g_top5 = [
        case_id
        for case_id in case_ids
        if isinstance(g_by_case[case_id].get("expected_rank"), (int, float))
    ]
    semantic_label = MODE_LABELS[semantic_mode].lower()
    fusion_label = MODE_LABELS[fusion_mode].lower()
    return {
        "primary_absent_hybrid_top20_entered_union_count": len(case_ids),
        "primary_absent_hybrid_top20_entered_union_case_ids": case_ids,
        f"entered_{semantic_label}_top5_count": len(f_top5),
        f"entered_{semantic_label}_top5_case_ids": f_top5,
        f"entered_{fusion_label}_top5_count": len(g_top5),
        f"entered_{fusion_label}_top5_case_ids": g_top5,
    }


def run_experiment(
    cases: list[dict],
    index: HybridRAGIndex,
    *,
    semantic_reranker: SemanticReranker,
) -> dict[str, dict]:
    mode_results = {mode: [] for mode in EXPERIMENT_MODES}

    for case in cases:
        context = case_query_context(case)
        query = build_retrieval_query(context)
        constraint = build_evidence_constraint(context)
        hybrid_candidates = index.search(query, candidate_k=CANDIDATE_K)
        union_candidates, union_diagnostics = build_split_union_candidates(
            index,
            query,
        )
        expanded_union_candidates, expanded_union_diagnostics = (
            build_split_union_candidates(
                index,
                query,
                vector_top_k=EXPANDED_VECTOR_TOP_K,
                lexical_top_k=EXPANDED_LEXICAL_TOP_K,
            )
        )
        rankings = build_shared_semantic_rankings(
            query,
            hybrid_candidates,
            union_candidates,
            semantic_reranker=semantic_reranker,
            expanded_union_candidates=expanded_union_candidates,
        )

        for mode in EXPERIMENT_MODES:
            if mode in {SPLIT_SEMANTIC_MODE, SPLIT_FUSION_MODE}:
                candidates = union_candidates
                candidate_diagnostics = union_diagnostics
            elif mode in {
                EXPANDED_SPLIT_SEMANTIC_MODE,
                EXPANDED_SPLIT_FUSION_MODE,
            }:
                candidates = expanded_union_candidates
                candidate_diagnostics = expanded_union_diagnostics
            else:
                candidates = hybrid_candidates
                candidate_diagnostics = None
            scored = score_mode(
                case,
                candidates,
                rankings[mode],
                constraint,
                mode=mode,
                top_k=TOP_K,
            )
            scored["query_contract"] = _query_contract(context, query)
            scored["candidate_count"] = len(candidates)
            if candidate_diagnostics is not None:
                scored["union_candidate_diagnostics"] = candidate_diagnostics
            mode_results[mode].append(scored)

    reports = {
        mode: build_mode_report(results, mode=mode, top_k=TOP_K)
        for mode, results in mode_results.items()
    }
    for mode, report in reports.items():
        report["label"] = MODE_LABELS[mode]
        report["candidate_evidence_recall"] = report[
            "candidate_evidence_recall_at_20"
        ]
        report["candidate_count"] = _candidate_count_stats(report["results"])

    return reports


def _compact_metrics(report: dict) -> dict[str, Any]:
    return {
        "pass": f"{report['passed_count']}/{report['total_cases']}",
        "passed_count": report["passed_count"],
        "total_cases": report["total_cases"],
        "hit_at_1": report["hit_at_1"],
        "hit_at_3": report["hit_at_3"],
        "hit_at_5": report["hit_at_5"],
        "mrr": report["mrr"],
        "recall": report["candidate_evidence_recall_at_20"],
        "coverage": report["evidence_coverage_rate"],
    }


def _metric_deltas(before: dict, after: dict) -> dict[str, float]:
    fields = {
        "pass_count": "passed_count",
        "hit_at_1": "hit_at_1",
        "hit_at_3": "hit_at_3",
        "hit_at_5": "hit_at_5",
        "mrr": "mrr",
        "recall": "candidate_evidence_recall_at_20",
        "coverage": "evidence_coverage_rate",
    }
    return {
        label: round(float(after[field]) - float(before[field]), 4)
        for label, field in fields.items()
    }


def build_summary(
    *,
    cases: list[dict],
    dataset_path: Path,
    dataset_validation: dict,
    manager: RAGIndexManager,
    refresh,
    semantic_reranker: CountingSemanticReranker,
    mode_reports: dict[str, dict],
) -> dict[str, Any]:
    movements = {
        name: compare_mode_rank_movement(
            before_mode=HYBRID_MODE,
            after_mode=after_mode,
            mode_reports=mode_reports,
        )
        for name, after_mode in {
            "A_to_C": SEMANTIC_RERANK_MODE,
            "A_to_E": SEMANTIC_FUSION_MODE,
            "A_to_F": SPLIT_SEMANTIC_MODE,
            "A_to_G": SPLIT_FUSION_MODE,
            "A_to_H": EXPANDED_SPLIT_SEMANTIC_MODE,
            "A_to_I": EXPANDED_SPLIT_FUSION_MODE,
        }.items()
    }
    f_counts = mode_reports[SPLIT_SEMANTIC_MODE]["candidate_count"]
    g_counts = mode_reports[SPLIT_FUSION_MODE]["candidate_count"]
    h_counts = mode_reports[EXPANDED_SPLIT_SEMANTIC_MODE]["candidate_count"]
    i_counts = mode_reports[EXPANDED_SPLIT_FUSION_MODE]["candidate_count"]
    if f_counts != g_counts:
        raise ValueError("F and G did not share identical candidate counts")
    if h_counts != i_counts:
        raise ValueError("H and I did not share identical candidate counts")

    hybrid_results = mode_reports[HYBRID_MODE]["results"]
    c_results = mode_reports[SEMANTIC_RERANK_MODE]["results"]
    e_results = mode_reports[SEMANTIC_FUSION_MODE]["results"]
    f_results = mode_reports[SPLIT_SEMANTIC_MODE]["results"]
    g_results = mode_reports[SPLIT_FUSION_MODE]["results"]
    h_results = mode_reports[EXPANDED_SPLIT_SEMANTIC_MODE]["results"]
    i_results = mode_reports[EXPANDED_SPLIT_FUSION_MODE]["results"]
    for a_result, c_result, e_result, f_result, g_result, h_result, i_result in zip(
        hybrid_results,
        c_results,
        e_results,
        f_results,
        g_results,
        h_results,
        i_results,
    ):
        case_id = a_result["case_id"]
        if any(
            result["case_id"] != case_id
            for result in (
                c_result,
                e_result,
                f_result,
                g_result,
                h_result,
                i_result,
            )
        ):
            raise ValueError("Ranking modes produced different case order")
        if not (
            a_result["candidate_chunk_ids"]
            == c_result["candidate_chunk_ids"]
            == e_result["candidate_chunk_ids"]
        ):
            raise ValueError(f"A/C/E candidate pool differs for {case_id}")
        if f_result["candidate_chunk_ids"] != g_result["candidate_chunk_ids"]:
            raise ValueError(f"F/G candidate pool differs for {case_id}")
        if h_result["candidate_chunk_ids"] != i_result["candidate_chunk_ids"]:
            raise ValueError(f"H/I candidate pool differs for {case_id}")

    if semantic_reranker.calls != len(cases):
        raise ValueError(
            "Cross-Encoder must be called exactly once per case; "
            f"expected {len(cases)}, got {semantic_reranker.calls}"
        )

    return {
        "schema_version": "rag-split-retrieval-ablation-v2",
        "created_at": now_iso(),
        "dataset": dataset_identity(dataset_path),
        "dataset_validation": dataset_validation,
        "dataset_cases": len(cases),
        "chunk_strategy": {
            "name": CHUNK_STRATEGY,
            "max_tokens": CHUNK_STRATEGIES[CHUNK_STRATEGY].max_chars,
            "overlap_tokens": CHUNK_STRATEGIES[CHUNK_STRATEGY].overlap,
            "fixed_for_experiment": True,
        },
        "kb_version": refresh.kb_version,
        "chunk_count": refresh.chunk_count,
        "experiment": {
            "hybrid_candidate_k": CANDIDATE_K,
            "vector_top_k": VECTOR_TOP_K,
            "lexical_top_k": LEXICAL_TOP_K,
            "expanded_vector_top_k": EXPANDED_VECTOR_TOP_K,
            "expanded_lexical_top_k": EXPANDED_LEXICAL_TOP_K,
            "top_k": TOP_K,
            "semantic_query": "semantic_query",
            "lexical_query": "lexical_query",
            "lexical_signal": "BM25 + keyword",
            "union_padding": False,
            "f_g_share_candidate_pool": True,
            "candidate_pool_identity_validated": True,
            "cross_encoder_calls": semantic_reranker.calls,
            "cross_encoder_calls_per_case": 1,
            "fusion": {
                "retrieval_weight": SEMANTIC_FUSION_RETRIEVAL_WEIGHT,
                "semantic_weight": SEMANTIC_FUSION_SEMANTIC_WEIGHT,
            },
        },
        "reproducibility": reproducibility_metadata(
            manager=manager,
            semantic_reranker=semantic_reranker,
            top_k=TOP_K,
            dataset_path=dataset_path,
            semantic_query_mode=SEMANTIC_QUERY_MODE,
        ),
        "metrics": {
            MODE_LABELS[mode]: _compact_metrics(mode_reports[mode])
            for mode in EXPERIMENT_MODES
        },
        "metric_deltas": {
            "F_minus_C": _metric_deltas(
                mode_reports[SEMANTIC_RERANK_MODE],
                mode_reports[SPLIT_SEMANTIC_MODE],
            ),
            "G_minus_E": _metric_deltas(
                mode_reports[SEMANTIC_FUSION_MODE],
                mode_reports[SPLIT_FUSION_MODE],
            ),
            "H_minus_C": _metric_deltas(
                mode_reports[SEMANTIC_RERANK_MODE],
                mode_reports[EXPANDED_SPLIT_SEMANTIC_MODE],
            ),
            "I_minus_E": _metric_deltas(
                mode_reports[SEMANTIC_FUSION_MODE],
                mode_reports[EXPANDED_SPLIT_FUSION_MODE],
            ),
            "H_minus_F": _metric_deltas(
                mode_reports[SPLIT_SEMANTIC_MODE],
                mode_reports[EXPANDED_SPLIT_SEMANTIC_MODE],
            ),
            "I_minus_G": _metric_deltas(
                mode_reports[SPLIT_FUSION_MODE],
                mode_reports[EXPANDED_SPLIT_FUSION_MODE],
            ),
        },
        "rank_movement": movements,
        "union_candidate_count": {
            "F": f_counts,
            "G": g_counts,
            "H": h_counts,
            "I": i_counts,
        },
        "new_primary_evidence": {
            "vector10_lexical10": build_new_primary_evidence_analysis(
                mode_reports,
            ),
            "vector15_lexical15": build_new_primary_evidence_analysis(
                mode_reports,
                semantic_mode=EXPANDED_SPLIT_SEMANTIC_MODE,
                fusion_mode=EXPANDED_SPLIT_FUSION_MODE,
            ),
        },
        "ablation": mode_reports,
    }


def _metric(value: Any) -> str:
    return f"{float(value):.4f}"


def print_summary(report: dict[str, Any], report_path: Path) -> None:
    print("\nFixed512 Split Retrieval Ablation")
    print("=" * 100)
    print(
        f"{'Mode':<8}{'Pass':>9}{'H@1':>9}{'H@3':>9}{'H@5':>9}"
        f"{'MRR':>9}{'Recall':>11}{'Coverage':>11}"
    )
    for mode in ("A", "C", "E", "F", "G", "H", "I"):
        row = report["metrics"][mode]
        print(
            f"{mode:<8}{row['pass']:>9}{_metric(row['hit_at_1']):>9}"
            f"{_metric(row['hit_at_3']):>9}{_metric(row['hit_at_5']):>9}"
            f"{_metric(row['mrr']):>9}{_metric(row['recall']):>11}"
            f"{_metric(row['coverage']):>11}"
        )

    print("\nRank Movement")
    print("=" * 100)
    print(
        f"{'Move':<9}{'Prom':>8}{'Same':>8}{'Demo':>8}{'Recovered':>11}"
        f"{'Dropped':>9}{'Outside':>9}{'NetTop5':>10}"
    )
    for name in (
        "A_to_C",
        "A_to_E",
        "A_to_F",
        "A_to_G",
        "A_to_H",
        "A_to_I",
    ):
        movement = report["rank_movement"][name]
        counts = movement["counts"]
        print(
            f"{name:<9}{counts['promoted']:>8}{counts['unchanged']:>8}"
            f"{counts['demoted']:>8}{counts['recovered_to_top5']:>11}"
            f"{counts['dropped_from_top5']:>9}"
            f"{counts['outside_top5_both']:>9}{movement['net_top5_change']:>10}"
        )

    for pool_name, semantic_label, fusion_label in (
        ("vector10_lexical10", "F", "G"),
        ("vector15_lexical15", "H", "I"),
    ):
        counts = report["union_candidate_count"][semantic_label]
        evidence = report["new_primary_evidence"][pool_name]
        print(
            f"\n{pool_name} candidate_count: "
            f"avg={counts['average']}, min={counts['minimum']}, "
            f"max={counts['maximum']}"
        )
        print(
            "New primary evidence: "
            f"union={evidence['primary_absent_hybrid_top20_entered_union_count']}, "
            f"{semantic_label} Top5="
            f"{evidence[f'entered_{semantic_label.lower()}_top5_count']}, "
            f"{fusion_label} Top5="
            f"{evidence[f'entered_{fusion_label.lower()}_top5_count']}"
        )
    print(f"\nReport: {report_path.resolve()}")


def main() -> None:
    dataset_path = DATASET_PATH.resolve()
    output_dir = OUTPUT_DIR.resolve()
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
        knowledge_dir=KNOWLEDGE_DIR.resolve(),
    )
    if not dataset_validation["passed"]:
        raise ValueError("Holdout dataset or ground truth validation failed")

    ingestion = KnowledgeIngestionService(
        KNOWLEDGE_DIR,
        manifest_path=output_dir / "knowledge_manifest.json",
        chunk_strategy=CHUNK_STRATEGY,
    )
    manager = RAGIndexManager(ingestion=ingestion)
    refresh = manager.refresh()
    semantic_reranker = CountingSemanticReranker(build_semantic_reranker())
    mode_reports = run_experiment(
        cases,
        refresh.active_index,
        semantic_reranker=semantic_reranker,
    )
    report = build_summary(
        cases=cases,
        dataset_path=dataset_path,
        dataset_validation=dataset_validation,
        manager=manager,
        refresh=refresh,
        semantic_reranker=semantic_reranker,
        mode_reports=mode_reports,
    )
    report_path = output_dir / "summary.json"
    write_json(report_path, report)
    print_summary(report, report_path)


if __name__ == "__main__":
    main()
