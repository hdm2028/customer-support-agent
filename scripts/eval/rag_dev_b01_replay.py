"""One-round Dev replay. Capture missing trace without refreshing any live index.

Only the two explicitly named Dev files are read. Capture is a read-only static
evaluation of the existing hybrid formula; replay never performs retrieval.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from app.rag.embedding_client import keyword_score, local_hash_embedding
from app.rag.hybrid_index import BM25Index, normalize_score
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.query_builder import build_retrieval_query
from app.rag.query_context import RetrievalQuery
from app.rag.ranking import build_evidence_constraint, rank_candidates
from app.rag.retrieval_text import build_retrieval_text
from app.rag.vector_store import cosine_similarity
from scripts.eval import eval_rag as ev

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/rag_dev_b01_optimization'
DATASETS = {'v2': ROOT / 'data/eval/rag_eval_dev_v2_batch01.jsonl',
            'v1': ROOT / 'data/eval/rag_eval.jsonl'}
BASELINE = ROOT / 'reports/eval_rag_dev_v2_batch01_baseline.json'


def write(name, value):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def capture():
    if (OUT / 'candidates.json').exists():
        raise RuntimeError('Frozen capture already exists; do not overwrite it')
    baseline = json.loads(BASELINE.read_text(encoding='utf8'))
    manifest = json.loads((ROOT / 'data/cache/knowledge_manifest.json').read_text(encoding='utf8'))
    build = KnowledgeIngestionService(chunk_strategy=baseline['chunk_strategy']).build(save=False)
    assert build.manifest.kb_version == manifest['kb_version'] == baseline['kb_version']
    for chunk in build.chunks:
        assert manifest['documents'][chunk.document_id]['chunk_hashes'][chunk.chunk_id] == chunk.content_hash
    assert baseline['reproducibility']['embedding_identity'] == 'local|local_hash_v1|256|document'
    weights = baseline['reproducibility']['hybrid_retrieval']
    w = [weights[k] for k in ('semantic_weight', 'bm25_weight', 'keyword_weight')]
    w = [x / sum(w) for x in w]
    texts = [build_retrieval_text(c) for c in build.chunks]
    # Static corpus statistics only. No RAGIndexManager, VectorStore or index writes.
    bm25 = BM25Index(texts)
    vectors = [local_hash_embedding(t) for t in texts]
    frozen = {'baseline': str(BASELINE), 'kb_version': build.manifest.kb_version,
              'reproducibility': baseline['reproducibility'], 'chunk_strategy': baseline['chunk_strategy'],
              'capture_method': 'static hybrid formula; verified against saved candidate IDs and Top5',
              'datasets': {}}
    for label, path in DATASETS.items():
        cases = ev.load_jsonl(path)
        assert len(cases) == (6 if label == 'v2' else 20)
        if label == 'v2':
            assert all(c['split'] == 'dev' for c in cases)
            assert sum(len(c['expected']['evidence_requirements']) for c in cases) == 19
        entries = []
        for case in cases:
            start = perf_counter()
            query = build_retrieval_query(ev.case_query_context(case))
            qv = local_hash_embedding(query.semantic_query)
            raw = [(cosine_similarity(qv, v), bm25.score(query.lexical_query, i),
                    keyword_score(query.lexical_query, c.source, c.text))
                   for i, (c, v) in enumerate(zip(build.chunks, vectors))]
            maxima = [max(max(row[i], 0) for row in raw) for i in range(3)]
            candidates = []
            for chunk, scores in zip(build.chunks, raw):
                norm = [normalize_score(s, m) for s, m in zip(scores, maxima)]
                score = sum(a * b for a, b in zip(norm, w))
                if score <= 0:
                    continue
                candidates.append({**chunk.to_dict(), 'score': round(score, 4),
                    'hybrid_score': round(score, 4), 'vector_score': round(scores[0], 4),
                    'semantic_score': round(scores[0], 4), 'bm25_score': round(scores[1], 4),
                    'semantic_norm_score': round(norm[0], 4), 'bm25_norm_score': round(norm[1], 4),
                    'keyword_norm_score': round(norm[2], 4),
                    'retrieval_mode': 'hybrid_vector_bm25_keyword',
                    'retrieval_weights': dict(zip(('semantic', 'bm25', 'keyword'), w))})
            candidates.sort(key=lambda c: c['hybrid_score'], reverse=True)
            candidates = candidates[:20]
            ranking = rank_candidates(query, candidates, mode='hybrid_rule', top_k=5)
            verified = None
            if label == 'v2':
                old = next(r for r in baseline['results'] if r['case_id'] == case['case_id'])
                assert asdict(query)['semantic_query'] == old['query_contract']['semantic_query']
                assert asdict(query)['lexical_query'] == old['query_contract']['lexical_query']
                assert [c['chunk_id'] for c in candidates] == old['candidate_chunk_ids']
                assert [c['chunk_id'] for c in ranking[:5]] == [c['chunk_id'] for c in old['ranking_trace']]
                for actual, saved in zip(ranking[:5], old['ranking_trace']):
                    for field in ('retrieval_score', 'rule_score', 'rule_boost', 'vector_score', 'bm25_score'):
                        assert actual[field] == saved[field], (case['case_id'], field)
                scored = ev.score_mode(case, candidates, ranking, build_evidence_constraint(ev.case_query_context(case)), mode='hybrid_rule', top_k=5)
                assert scored['candidate_evidence_report'] == old['candidate_evidence_report']
                assert scored['evidence_report'] == old['evidence_report']
                verified = True
            entries.append({'case_id': case['case_id'], 'query': asdict(query), 'candidates': candidates,
                            'baseline_verified': verified, 'diagnostic_capture_ms': (perf_counter()-start)*1000})
        frozen['datasets'][label] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'entries': entries}
    write('candidates.json', frozen)
    print('Captured 6 V2 + 20 V1 Dev pools; all V2 candidate IDs/order, Top5 scores and evidence match baseline.')


def diagnose():
    frozen = json.loads((OUT / 'candidates.json').read_text(encoding='utf8'))
    cases = ev.load_jsonl(DATASETS['v2'])
    rows = []
    for case, entry in zip(cases, frozen['datasets']['v2']['entries']):
        pool = entry['candidates']
        ranked = rank_candidates(RetrievalQuery(**entry['query']), pool, mode='hybrid_rule', top_k=5)
        reqs = case['expected']['evidence_requirements']
        masks = [{i for i, r in enumerate(reqs) if ev.evidence_requirement_matches(c, r)} for c in pool]
        cover = None
        for n in range(1, 6):
            cover = next((combo for combo in itertools.combinations(range(len(pool)), n)
                          if set().union(*(masks[i] for i in combo)) == set(range(len(reqs)))), None)
            if cover is not None:
                break
        positions = {c['chunk_id']: i for i, c in enumerate(ranked, 1)}
        details = []
        for i, req in enumerate(reqs):
            matches = [{'chunk_id': c['chunk_id'], 'candidate_rank': j+1,
                        'rerank_rank': positions[c['chunk_id']], 'in_top5': positions[c['chunk_id']] <= 5,
                        'stage': 'selected' if positions[c['chunk_id']] <= 5 else 'top5_truncation',
                        'score': next(x['rule_score'] for x in ranked if x['chunk_id'] == c['chunk_id'])}
                       for j, c in enumerate(pool) if i in masks[j]]
            details.append({'requirement_index': i+1, 'requirement': req, 'matches': matches})
        rows.append({'case_id': case['case_id'], 'requirements': details,
                     'oracle_diagnostic_only': {'minimum_chunks': len(cover) if cover is not None else None,
                       'chunk_ids': [pool[i]['chunk_id'] for i in cover] if cover is not None else [],
                       'never_system_output': True}, 'ranking': ranked})
    write('diagnosis.json', rows)
    for row in rows:
        print(row['case_id'], 'oracle size',row['oracle_diagnostic_only']['minimum_chunks'])
        for d in row['requirements']:
            print(' E',d['requirement_index'],[(m['candidate_rank'],m['rerank_rank']) for m in d['matches']])
        if row['case_id'].endswith(('002','004','005')):
            for c in row['ranking']:
                print(c['final_rank'], c['retrieval_rank'], c['source'],c['section'],c['rule_score'],c['rule_boost'],c['rule_reason'])


def replay():
    if (OUT / 'ab_results.json').exists():
        raise RuntimeError('A/B already recorded; this round must not try again')
    frozen = json.loads((OUT / 'candidates.json').read_text(encoding='utf8'))
    output = {'experiment': 'disable source/first-section fixed bonuses only',
              'config': {'A': {'rule_metadata_priors': True}, 'B': {'rule_metadata_priors': False},
                         'mode': 'hybrid_rule', 'candidate_k': 20, 'top_k': 5},
              'kb_version': frozen['kb_version'], 'datasets': {}}
    for label, path in DATASETS.items():
        data = frozen['datasets'][label]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == data['sha256']
        results = {'A': [], 'B': []}
        traces = []
        for case, entry in zip(ev.load_jsonl(path), data['entries']):
            assert case['case_id'] == entry['case_id']
            candidates = entry['candidates']
            snapshot = json.dumps(candidates, sort_keys=True)
            query = RetrievalQuery(**entry['query'])
            constraint = build_evidence_constraint(ev.case_query_context(case))
            trace = {'case_id': case['case_id'], 'candidate_pool_identical': True}
            for arm in ('A', 'B'):
                start = perf_counter()
                ranked = rank_candidates(query, candidates, mode='hybrid_rule', top_k=5,
                                         rule_metadata_priors=(arm == 'A'))
                elapsed_ms = (perf_counter() - start) * 1000
                scored = ev.score_mode(case, candidates, ranked, constraint, mode='hybrid_rule', top_k=5)
                results[arm].append(scored)
                trace[arm] = {'ranking_ms': elapsed_ms, 'ranking': ranked,
                              'final_chunk_ids': [c['chunk_id'] for c in ranked[:5]]}
                assert json.dumps(candidates, sort_keys=True) == snapshot
            traces.append(trace)
        output['datasets'][label] = {
            'path': str(path), 'sha256': data['sha256'],
            'A': ev.build_mode_report(results['A'], mode='hybrid_rule', top_k=5),
            'B': ev.build_mode_report(results['B'], mode='hybrid_rule', top_k=5), 'traces': traces}
    write('ab_results.json', output)
    for label, data in output['datasets'].items():
        print(label)
        for arm in ('A', 'B'):
            print(arm, {k: data[arm][k] for k in ('hit_at_1','hit_at_3','hit_at_5','mrr','evidence_coverage_rate','passed_count')})
        for a,b in zip(data['A']['results'], data['B']['results']):
            print(a['case_id'], a['expected_rank'], b['expected_rank'], a['evidence_coverage_rate'],b['evidence_coverage_rate'],a['passed'],b['passed'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['capture', 'diagnose', 'replay'])
    args = parser.parse_args()
    {'capture': capture, 'diagnose': diagnose, 'replay': replay}[args.stage]()
