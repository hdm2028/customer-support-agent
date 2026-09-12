"""Compare actual local rerankers on existing frozen Dev pools only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

from app.rag.query_context import RetrievalQuery
from app.rag.ranking import build_evidence_constraint, rank_candidates
from app.rag.semantic_reranker import build_semantic_reranker
from scripts.eval import eval_rag as ev

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    # Use the pinned cached model; this experiment must not download another revision.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    source = ROOT / 'reports/rag_dev_b01_optimization/candidates.json'
    frozen = json.loads(source.read_text(encoding='utf8'))
    reranker = build_semantic_reranker()
    start = perf_counter()
    reranker._load_model()
    result = {
        'candidate_k': 20, 'top_k': 5,
        'candidate_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'kb_version': frozen['kb_version'],
        'model': reranker.identity.to_dict(),
        'model_load_ms': (perf_counter() - start) * 1000,
        'datasets': {},
    }
    modes = {
        'rule': {'mode': 'hybrid_rule'},
        'rule_complementary': {'mode': 'hybrid_rule', 'evidence_selection': 'complementary'},
        'cross_encoder': {'mode': 'hybrid_semantic'},
    }
    for label in ('v2', 'v1'):
        data = frozen['datasets'][label]
        dataset = ROOT / 'data/eval' / ('rag_eval_dev_v2_batch01.jsonl' if label == 'v2' else 'rag_eval.jsonl')
        assert hashlib.sha256(dataset.read_bytes()).hexdigest() == data['sha256']
        cases = ev.load_jsonl(dataset)
        assert len(cases) == len(data['entries']) == (6 if label == 'v2' else 20)
        reports = {name: {'results': [], 'traces': []} for name in modes}
        for case, entry in zip(cases, data['entries']):
            assert case['case_id'] == entry['case_id']
            query = RetrievalQuery(**entry['query'])
            pool = entry['candidates']
            before = json.dumps(pool, sort_keys=True)
            for name, config in modes.items():
                start = perf_counter()
                ranked = rank_candidates(query, pool, top_k=5, semantic_reranker=reranker, **config)
                elapsed = (perf_counter() - start) * 1000
                assert len(ranked) == 20
                assert {c['chunk_id'] for c in ranked} == {c['chunk_id'] for c in pool}
                assert json.dumps(pool, sort_keys=True) == before
                # Ground truth is accessed only after ranking by the unchanged scorer.
                constraint = build_evidence_constraint(ev.case_query_context(case))
                reports[name]['results'].append(ev.score_mode(case, pool, ranked, constraint, mode=config['mode'], top_k=5))
                reports[name]['traces'].append({'case_id': case['case_id'], 'ranking_ms': elapsed,
                                               'ranking': ranked, 'final_chunk_ids': [c['chunk_id'] for c in ranked[:5]]})
            print(label, case['case_id'], 'completed', flush=True)
        result['datasets'][label] = {
            name: {'report': ev.build_mode_report(value['results'], mode=modes[name]['mode'], top_k=5),
                   'traces': value['traces']} for name, value in reports.items()
        }
        (output / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    for label, variants in result['datasets'].items():
        for name, value in variants.items():
            print(label, name, {k: value['report'][k] for k in ('passed_count', 'mrr', 'evidence_coverage_rate', 'hit_at_5')})


if __name__ == '__main__':
    main()
