"""Recorded follow-up experiments on the first batch's frozen Dev candidates."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from app.rag.query_context import RetrievalQuery
from app.rag.query_builder import build_retrieval_query
from app.rag.retriever import HybridRetriever
from app.rag.ranking import build_evidence_constraint, rank_candidates
from scripts.eval import eval_rag as ev

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / 'reports/rag_dev_b01_optimization'
OUT = ROOT / 'reports/rag_dev_b01_followup'
CONFIGS = {
    'r02_complete_primary': {'evidence_selection': 'complete_primary'},
    'r03_complementary': {'evidence_selection': 'complementary'},
    'r04_stable_affinity': {'evidence_selection': 'complementary'},
    'r05_multitopic_scope': {'evidence_selection': 'complementary'},
    'r06_keyword_groups': {'evidence_selection': 'complementary'},
    'r07_section_tiebreak': {'evidence_selection': 'complementary'},
    'r08_primary_concepts': {'evidence_selection': 'complementary'},
    'r09_normative_evidence': {'evidence_selection': 'complementary'},
    'r10_covered_sections': {'evidence_selection': 'complementary'},
    'r11_bound_dependencies': {'evidence_selection': 'complementary'},
    'r12_explicit_topics_first': {'evidence_selection': 'complementary'},
    'r13_guardrail_compatible': {'evidence_selection': 'complementary'},
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(name):
    path = OUT / (name + '.json')
    if path.exists():
        raise RuntimeError('Recorded experiment already exists; do not overwrite')
    frozen = json.loads((ORIGINAL / 'candidates.json').read_text(encoding='utf8'))
    original = json.loads((ORIGINAL / 'ab_results.json').read_text(encoding='utf8'))
    output = {'experiment': name, 'config': CONFIGS[name], 'candidate_k': 20, 'top_k': 5,
              'baseline': 'original hybrid_rule, metadata_priors=True',
              'kb_version': frozen['kb_version'], 'frozen_sha256': digest(ORIGINAL / 'candidates.json'),
              'implementation': {p: (ROOT / p).read_text(encoding='utf8') for p in
                  ('app/rag/evidence_selection.py', 'app/rag/ranking.py', 'app/rag/reranker.py')},
              'datasets': {}}
    for label in ('v2', 'v1'):
        data = frozen['datasets'][label]
        dataset = Path(data['path'])
        assert dataset.name == ('rag_eval_dev_v2_batch01.jsonl' if label == 'v2' else 'rag_eval.jsonl')
        assert digest(dataset) == data['sha256']
        cases = ev.load_jsonl(dataset)
        assert len(cases) == (6 if label == 'v2' else 20)
        results = []
        traces = []
        for case, entry, a in zip(cases, data['entries'], original['datasets'][label]['A']['results']):
            assert case['case_id'] == entry['case_id'] == a['case_id']
            pool = entry['candidates']
            before = json.dumps(pool, sort_keys=True)
            query = RetrievalQuery(**entry['query'])
            constraint = build_evidence_constraint(ev.case_query_context(case))
            start = perf_counter()
            ranked = rank_candidates(query, pool, mode='hybrid_rule', top_k=5, **CONFIGS[name])
            elapsed = (perf_counter() - start)*1000
            assert before == json.dumps(pool, sort_keys=True)
            assert len(ranked) == 20 and {c['chunk_id'] for c in pool} == {c['chunk_id'] for c in ranked}
            # Expected enters only the unchanged evaluator, after selection.
            b = ev.score_mode(case, pool, ranked, constraint, mode='hybrid_rule', top_k=5)
            results.append(b)
            traces.append({'case_id': case['case_id'], 'ranking_ms': elapsed,
                           'ranking': ranked, 'candidate_pool_identical': True,
                           'final_chunk_ids': [c['chunk_id'] for c in ranked[:5]]})
        report = ev.build_mode_report(results, mode='hybrid_rule', top_k=5)
        regressions = []
        for a,b in zip(original['datasets'][label]['A']['results'], results):
            reasons = []
            if b['reciprocal_rank'] < a['reciprocal_rank']: reasons.append('primary_rank')
            if b['evidence_coverage_rate'] < a['evidence_coverage_rate']: reasons.append('evidence_coverage')
            if a['passed'] and not b['passed']: reasons.append('previously_passed')
            if reasons: regressions.append({'case_id': a['case_id'], 'reasons': reasons})
        output['datasets'][label] = {'dataset_sha256': data['sha256'], 'report': report,
                                    'traces': traces, 'regressions_vs_original': regressions}
    OUT.mkdir(exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf8')
    for label,data in output['datasets'].items():
        r = data['report']
        print(label, {k:r[k] for k in ('hit_at_1','hit_at_3','hit_at_5','mrr','evidence_coverage_rate','passed_count')})
        print('regressions',data['regressions_vs_original'])
        for a,b in zip(original['datasets'][label]['A']['results'],r['results']):
            if label == 'v2' or b['passed'] != a['passed']:
                print(b['case_id'], a['expected_rank'], b['expected_rank'],a['evidence_coverage_rate'],b['evidence_coverage_rate'],a['passed'],b['passed'])


def verify_final():
    frozen = json.loads((ORIGINAL / 'candidates.json').read_text(encoding='utf8'))
    original = json.loads((ORIGINAL / 'ab_results.json').read_text(encoding='utf8'))
    selected = json.loads((OUT / 'r13_guardrail_compatible.json').read_text(encoding='utf8'))
    summary = {'selected_experiment': 'r13_guardrail_compatible', 'checks': [],
               'implementation': {p: (ROOT / p).read_text(encoding='utf8') for p in (
                   'app/rag/evidence_selection.py', 'app/rag/ranking.py', 'app/rag/retriever.py')},
               'datasets': {}}
    for label in ('v2', 'v1'):
        data = frozen['datasets'][label]
        assert digest(Path(data['path'])) == data['sha256']
        cases = ev.load_jsonl(Path(data['path']))
        verified = []
        for case, entry, a, b, saved in zip(cases, data['entries'],
                original['datasets'][label]['A']['results'],
                selected['datasets'][label]['report']['results'], selected['datasets'][label]['traces']):
            query = build_retrieval_query(ev.case_query_context(case))
            assert asdict(query) == entry['query']
            pool = entry['candidates']
            before = json.dumps(pool, sort_keys=True)
            retriever = HybridRetriever(index_manager=object())
            retriever.retrieve_candidates = lambda query, *, candidate_k: pool
            start = perf_counter()
            final = retriever.retrieve(query,top_k=5,candidate_k=20,mode='hybrid_rule',
                                       evidence_selection='complementary')
            elapsed = (perf_counter() - start)*1000
            assert [c['chunk_id'] for c in final] == saved['final_chunk_ids']
            default = rank_candidates(query,pool,mode='hybrid_rule',top_k=5)
            constraint = build_evidence_constraint(ev.case_query_context(case))
            rescored = ev.score_mode(case,pool,final,constraint,mode='hybrid_rule',top_k=5)
            assert rescored == b
            assert ev.score_mode(case,pool,default,constraint,mode='hybrid_rule',top_k=5) == a
            assert before == json.dumps(pool, sort_keys=True)
            assert b['reciprocal_rank'] >= a['reciprocal_rank']
            assert b['evidence_coverage_rate'] >= a['evidence_coverage_rate']
            assert not a['passed'] or b['passed']
            assert not a['evidence_guardrail_pass'] or b['evidence_guardrail_pass']
            assert set(a['keyword_report'].get('matched_terms',[])) <= set(b['keyword_report'].get('matched_terms',[]))
            verified.append({'case_id':case['case_id'], 'verified':True,
                             'selector_and_wrapper_ms':elapsed, 'final':final})
        summary['datasets'][label] = verified
    manifest = json.loads((ROOT/'data/cache/knowledge_manifest.json').read_text(encoding='utf8'))
    assert manifest['kb_version'] == frozen['kb_version']
    for doc in manifest['documents'].values():
        assert digest(ROOT/'data/knowledge'/doc['source']) == doc['content_hash']
    summary['checks'] = ['unchanged_dev_bytes', 'unchanged_kb_bytes_and_manifest_version',
                         'unchanged_query_contract', 'frozen_pools_unchanged',
                         'public_retriever_equals_selected_experiment', 'default_equals_original_baseline',
                         'no_per_case_rank_evidence_pass_guardrail_or_keyword_regressions']
    (OUT/'final_verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    print('Verified final public retriever on 6 V2 + 20 V1 Dev cases; defaults unchanged; no regressions.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', choices=[*CONFIGS, 'verify_final'])
    name = parser.parse_args().experiment
    verify_final() if name == 'verify_final' else run(name)
