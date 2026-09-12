"""One fixed Dev experiment: contextual query embedding, unchanged downstream selection.

Uses production hybrid search over a read-only reconstruction of existing chunks
and deterministic baseline vectors; never refreshes or writes the online index.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from app.rag.hybrid_index import HybridRAGIndex
from app.rag.query_context import resolve_embedding_query
from app.rag.vector_store import InMemoryVectorStore
from scripts.eval.rag_dev_b01_replay import (
    BASELINE, DATASETS, ROOT, KnowledgeIngestionService, build_retrieval_query,
    build_retrieval_text, build_evidence_constraint, ev, local_hash_embedding, rank_candidates,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BaselineQueryEmbedding:
    def embed_query(self, text):
        return local_hash_embedding(text)

    def document_embedding_identity(self):
        return "local|local_hash_v1|256|document"


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    frozen = json.loads((ROOT / "reports/rag_dev_b01_optimization/candidates.json").read_text(encoding="utf-8"))
    previous = json.loads((ROOT / "reports/rag_dev_b01_followup/r13_guardrail_compatible.json").read_text(encoding="utf-8"))
    protected = [*DATASETS.values(), BASELINE, ROOT / "data/cache/knowledge_manifest.json",
                 ROOT / "scripts/eval/eval_rag.py", *sorted((ROOT / "data/knowledge").glob("*.md"))]
    before = {str(p.relative_to(ROOT)): digest(p) for p in protected}
    build = KnowledgeIngestionService(chunk_strategy=baseline["chunk_strategy"]).build(save=False)
    assert build.manifest.kb_version == baseline["kb_version"] == frozen["kb_version"]
    assert baseline["reproducibility"]["embedding_identity"] == BaselineQueryEmbedding().document_embedding_identity()
    saved_manifest = json.loads((ROOT / "data/cache/knowledge_manifest.json").read_text(encoding="utf-8"))
    store = InMemoryVectorStore()
    for chunk in build.chunks:
        assert saved_manifest["documents"][chunk.document_id]["chunk_hashes"][chunk.chunk_id] == chunk.content_hash
        text = build_retrieval_text(chunk)
        store.upsert(chunk, local_hash_embedding(text), embedding_text_hash=hashlib.sha256(text.encode()).hexdigest(),
                     embedding_identity=BaselineQueryEmbedding().document_embedding_identity())
    weights = baseline["reproducibility"]["hybrid_retrieval"]
    settings = SimpleNamespace(**{"rag_" + k: weights[k] for k in ("semantic_weight", "bm25_weight", "keyword_weight")})
    with patch("app.rag.hybrid_index.get_settings", return_value=settings):
        index = HybridRAGIndex(build.chunks, store, kb_version=build.manifest.kb_version, embedding_provider=BaselineQueryEmbedding())
    output = {"experiment": "query embedding receives existing structured rerank query only",
              "config": {"A": {"RAG_SEMANTIC_CONTEXT": ""}, "B": {"RAG_SEMANTIC_CONTEXT": "route"},
                         "weights": weights, "candidate_k": 20, "top_k": 5, "ranking_mode": "hybrid_rule",
                         "evidence_selection": "complementary", "embedding_identity": index.embedding_identity},
              "scope": "read-only corpus replay through production hybrid search; recall pools intentionally may differ; no LLM calls",
              "kb_version": build.manifest.kb_version, "chunk_strategy": baseline["chunk_strategy"],
              "corpus_chunk_count": len(build.chunks), "datasets": {}}
    for label, path in DATASETS.items():
        cases = ev.load_jsonl(path)
        assert len(cases) == (6 if label == "v2" else 20)
        assert digest(path) == frozen["datasets"][label]["sha256"]
        if label == "v2":
            assert all(c["split"] == "dev" for c in cases)
            assert sum(len(c["expected"]["evidence_requirements"]) for c in cases) == 19
        scores, traces = {"A": [], "B": []}, []
        for case, saved, old_trace in zip(cases, frozen["datasets"][label]["entries"], previous["datasets"][label]["traces"]):
            assert case["case_id"] == saved["case_id"] == old_trace["case_id"]
            query = build_retrieval_query(ev.case_query_context(case))
            assert asdict(query) == saved["query"]
            trace = {"case_id": case["case_id"], "query_contract": asdict(query)}
            for arm, mode in (("A", ""), ("B", "route")):
                with patch.dict("os.environ", {"RAG_SEMANTIC_CONTEXT": mode}):
                    start = perf_counter()
                    candidates = index.search(query, candidate_k=20)
                    retrieval_ms = (perf_counter() - start) * 1000
                    start = perf_counter()
                    ranked = rank_candidates(query, candidates, mode="hybrid_rule", top_k=5, evidence_selection="complementary")
                    ranking_ms = (perf_counter() - start) * 1000
                    final = ranked[:5]
                    trace[arm] = {"embedding_query": resolve_embedding_query(query), "candidates": candidates,
                                  "ranking": ranked, "final_chunk_ids": [c["chunk_id"] for c in final],
                                  "retrieval_ms": retrieval_ms, "ranking_ms": ranking_ms}
                if arm == "A":
                    assert [c["chunk_id"] for c in candidates] == [c["chunk_id"] for c in saved["candidates"]]
                    assert trace[arm]["final_chunk_ids"] == old_trace["final_chunk_ids"]
                    for actual, prior in zip(candidates, saved["candidates"]):
                        for field in ("hybrid_score", "vector_score", "bm25_score"):
                            assert actual[field] == prior[field]
                # Ground truth is read only after production search and ranking.
                scores[arm].append(ev.score_mode(case, candidates, ranked, build_evidence_constraint(ev.case_query_context(case)), mode="hybrid_rule", top_k=5))
            a_ids = [c["chunk_id"] for c in trace["A"]["candidates"]]
            b_ids = [c["chunk_id"] for c in trace["B"]["candidates"]]
            trace["candidate_pool_same_set"] = set(a_ids) == set(b_ids)
            trace["candidate_order_identical"] = a_ids == b_ids
            trace["candidate_added"] = [x for x in b_ids if x not in a_ids]
            trace["candidate_removed"] = [x for x in a_ids if x not in b_ids]
            traces.append(trace)
        output["datasets"][label] = {"path": str(path), "sha256": digest(path), "traces": traces,
                                     **{arm: ev.build_mode_report(rows, mode="hybrid_rule", top_k=5) for arm, rows in scores.items()}}
    assert before == {str(p.relative_to(ROOT)): digest(p) for p in protected}
    output.update(protected_inputs_unchanged=True, protected_inputs=before, all_A_pools_and_final_ids_verified=True)
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(output, out, ensure_ascii=False, indent=2)
    for label, data in output["datasets"].items():
        print(label, {arm: {k: data[arm][k] for k in ("hit_at_1", "hit_at_5", "mrr", "candidate_evidence_recall_at_20", "evidence_coverage_rate", "passed_count")} for arm in ("A", "B")})


if __name__ == "__main__":
    main()
