"""Measure the recorded before/after retriever lifecycle on one frozen pool."""
import argparse
import gc
import json
import os
from pathlib import Path
from time import perf_counter
from types import ModuleType
from unittest.mock import patch

from app.rag.query_context import RetrievalQuery
from app.rag.retriever import HybridRetriever
from app.rag.semantic_reranker import build_semantic_reranker


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    output = args.directory / 'lifecycle_benchmark.json'
    if output.exists():
        raise RuntimeError('Do not overwrite recorded results')
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    frozen = json.loads(Path('reports/rag_dev_b01_optimization/candidates.json').read_text(encoding='utf8'))
    entry = frozen['datasets']['v2']['entries'][0]
    query = RetrievalQuery(**entry['query'])
    pool = entry['candidates']
    baseline = ModuleType('recorded_retriever')
    sources = json.loads((args.directory / 'before_sources.json').read_text(encoding='utf8'))
    exec(compile(sources['app/rag/retriever.py'], '<recorded_retriever>', 'exec'), baseline.__dict__)
    # Both variants exclude one-time library import and OS cold file-cache cost.
    warmup = build_semantic_reranker()
    warmup._load_model()
    identity = warmup.identity.to_dict()
    del warmup
    gc.collect()
    result = {'mode': 'hybrid_semantic_fusion', 'candidate_k': 20, 'top_k': 5,
              'case_id': entry['case_id'], 'model': identity,
              'note': 'Three sequential requests per variant; imports/file cache warmed. Not service p95.',
              'variants': {}}
    for name, cls in (('before', baseline.HybridRetriever), ('after', HybridRetriever)):
        builds = []
        def factory():
            model = build_semantic_reranker()
            builds.append(1)
            return model
        retriever = cls(index_manager=object())
        retriever.retrieve_candidates = lambda *a, **kw: pool
        calls = []
        with patch.object(baseline, 'build_semantic_reranker', side_effect=factory), \
             patch('app.rag.retriever.build_semantic_reranker', side_effect=factory), \
             patch('app.rag.ranking.build_semantic_reranker', side_effect=factory):
            for i in range(3):
                start = perf_counter()
                final = retriever.retrieve(query, top_k=5, candidate_k=20, mode='hybrid_semantic_fusion')
                calls.append({'elapsed_ms': (perf_counter()-start)*1000,
                              'final_chunk_ids': [c['chunk_id'] for c in final],
                              'semantic_scores': [c['semantic_rerank_score'] for c in final]})
                print(name, i+1, round(calls[-1]['elapsed_ms'], 2), flush=True)
        result['variants'][name] = {'model_instances': len(builds), 'calls': calls}
        del retriever
        gc.collect()
    before = result['variants']['before']['calls']
    after = result['variants']['after']['calls']
    result['outputs_identical'] = all(a['final_chunk_ids'] == b['final_chunk_ids'] and
        a['semantic_scores'] == b['semantic_scores'] for a, b in zip(before, after))
    assert result['outputs_identical']
    with output.open('x', encoding='utf8') as target:
        json.dump(result, target, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
