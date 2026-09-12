"""Read-only full-corpus diagnosis of the separately recorded Dev recall gap."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from scripts.eval.rag_dev_b01_replay import (
    BASELINE, DATASETS, ROOT, BM25Index, KnowledgeIngestionService,
    build_retrieval_query, build_retrieval_text, cosine_similarity, ev,
    keyword_score, local_hash_embedding, normalize_score,
)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    case = next(c for c in ev.load_jsonl(DATASETS["v2"]) if c["case_id"] == "rag_dev_candidate_b01_003")
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    frozen = json.loads((ROOT / "reports/rag_dev_b01_optimization/candidates.json").read_text(encoding="utf-8"))
    entry = next(c for c in frozen["datasets"]["v2"]["entries"] if c["case_id"] == case["case_id"])
    build = KnowledgeIngestionService(chunk_strategy=baseline["chunk_strategy"]).build(save=False)
    assert build.manifest.kb_version == baseline["kb_version"] == frozen["kb_version"]
    query = build_retrieval_query(ev.case_query_context(case))
    assert asdict(query) == entry["query"]
    texts = [build_retrieval_text(c) for c in build.chunks]
    bm25 = BM25Index(texts)
    qvector = local_hash_embedding(query.semantic_query)
    raw = [(cosine_similarity(qvector, local_hash_embedding(text)), bm25.score(query.lexical_query, i),
            keyword_score(query.lexical_query, chunk.source, chunk.text))
           for i, (chunk, text) in enumerate(zip(build.chunks, texts))]
    maxima = [max(max(row[i], 0) for row in raw) for i in range(3)]
    configured = baseline["reproducibility"]["hybrid_retrieval"]
    weights = [configured[k] for k in ("semantic_weight", "bm25_weight", "keyword_weight")]
    weights = [value / sum(weights) for value in weights]
    scored = [{**chunk.to_dict(), "raw_scores": scores,
               "hybrid_score": round(sum(normalize_score(s, m) * w for s, m, w in zip(scores, maxima, weights)), 4)}
              for chunk, scores in zip(build.chunks, raw)]
    scored.sort(key=lambda row: row["hybrid_score"], reverse=True)
    assert [r["chunk_id"] for r in scored[:20]] == [r["chunk_id"] for r in entry["candidates"]]
    positions = [{r["chunk_id"]: i for i, r in enumerate(sorted(scored, key=lambda r: r["raw_scores"][channel], reverse=True), 1)}
                 for channel in range(3)]
    requirements = []
    for requirement in case["expected"]["evidence_requirements"]:
        matches = []
        for rank, chunk in enumerate(scored, 1):
            if ev.evidence_requirement_matches(chunk, requirement):
                matches.append({"chunk_id": chunk["chunk_id"], "source": chunk["source"], "section": chunk["section"],
                                "full_corpus_rank": rank, "in_top20": rank <= 20,
                                "hybrid_score": chunk["hybrid_score"], "raw_scores": chunk["raw_scores"],
                                "channel_ranks": [p[chunk["chunk_id"]] for p in positions], "text": chunk["text"]})
        requirements.append({"requirement": requirement, "matches": matches})
    report = {"case_id": case["case_id"], "query": asdict(query), "kb_version": build.manifest.kb_version,
              "corpus_chunk_count": len(scored), "weights": weights, "candidate_k": 20, "top_k": 5,
              "original_candidate_ids_and_order_verified": True, "top20_cutoff_score": scored[19]["hybrid_score"],
              "requirements": requirements, "diagnostic_only": True,
              "note": "Expected used only for offline matching after unmodified scoring; no new retrieval output or index write."}
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps([{r["requirement"]["evidence_type"]: [(m["full_corpus_rank"], m["channel_ranks"]) for m in r["matches"]]} for r in requirements], ensure_ascii=False))


if __name__ == "__main__":
    main()
