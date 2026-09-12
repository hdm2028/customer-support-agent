"""Run the existing five Dev evaluation layers from one frozen private copy.

Actual semantic routing and generated replies are enabled. The existing
deterministic scoring functions and business-forced fallbacks are preserved.
No holdout/validation/final_test data or production database is copied.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ('routing_eval.jsonl', 'rag_eval.jsonl', 'rag_eval_dev_v2_batch01.jsonl',
            'tool_eval.jsonl', 'answer_eval.jsonl', 'e2e_eval.jsonl')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def rag_child(output, profile):
    from app.rag.index_manager import RAGIndexManager
    from app.rag.retriever import HybridRetriever
    from app.rag.query_builder import build_retrieval_query
    from app.rag.ranking import rank_candidates, build_evidence_constraint
    from scripts.eval import eval_rag as ev
    manager = RAGIndexManager(chunk_strategy='fixed_256')
    refresh = manager.refresh()
    retriever = HybridRetriever(manager)
    for name, count in (('rag_eval.jsonl', 20), ('rag_eval_dev_v2_batch01.jsonl', 6)):
        path = ROOT / 'data/eval' / name
        cases = ev.load_jsonl(path)
        assert len(cases) == count
        ev.validate_cases(cases)
        results, traces = [], []
        for case in cases:
            context = ev.case_query_context(case)
            query = build_retrieval_query(context)
            started = perf_counter()
            pool = retriever.retrieve_candidates(query, candidate_k=20)
            selection = 'complementary' if profile == 'optimized' else None
            ranked = rank_candidates(query, pool, mode='hybrid_rule', top_k=5, evidence_selection=selection)
            elapsed = (perf_counter() - started) * 1000
            result = ev.score_mode(case, pool, ranked, build_evidence_constraint(context), mode='hybrid_rule', top_k=5)
            results.append(result)
            traces.append({'case_id': case['case_id'], 'query': query.__dict__, 'duration_ms': elapsed,
                           'candidates': pool, 'ranking': ranked, 'final': ranked[:5]})
        label = 'v1_dev20' if count == 20 else 'v2_dev6'
        write_json(output / (label + '.json'), {'dataset': name, 'dataset_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'kb_version': refresh.kb_version, 'chunk_count': refresh.chunk_count,
                   'candidate_k': 20, 'top_k': 5, 'ranking_mode': 'hybrid_rule', 'evidence_selection': selection,
                   'report': ev.build_mode_report(results, mode='hybrid_rule', top_k=5), 'traces': traces})
        print(label, sum(r['passed'] for r in results), '/', len(results), flush=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rag-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--profile', choices=('baseline', 'optimized'), default='baseline')
    args = parser.parse_args()
    output = args.output.resolve()
    if args.rag_child:
        rag_child(output, args.profile)
        return
    output.mkdir(parents=True, exist_ok=False)
    from app.core.config import get_settings
    settings = get_settings()
    files = {}
    for folder in ('app', 'scripts', 'data/knowledge'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                files[path.relative_to(ROOT)] = path.read_bytes()
    for relative in (Path('data/orders.json'), *(Path('data/eval') / name for name in DATASETS)):
        files[relative] = (ROOT / relative).read_bytes()
    manifest = {'profile': 'five_layer_dev_live_routing_live_replies', 'model': settings.zhipu_model,
                'llm_key_configured': settings.has_llm_key, 'scoring': 'existing evaluators unchanged',
                'source_files': {p.as_posix(): hashlib.sha256(b).hexdigest() for p, b in files.items()},
                'runs': {}, 'experiment_profile': args.profile, 'configuration': {'chunk_strategy': 'fixed_256', 'overlap': 32,
                    'rag_ranking_mode': 'hybrid_rule', 'candidate_k': 20, 'rag_top_k': 5,
                    'evidence_selection': 'complementary' if args.profile == 'optimized' else None,
                    'semantic_related_topics': 'discard_invalid' if args.profile == 'optimized' else 'strict',
                    'database': 'isolated SQLite', 'cache': 'process-local',
                    'memory_context_max_chars': 8000, 'memory_context_ttl_seconds': 1800}}
    with zipfile.ZipFile(output / 'source_snapshot.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path.as_posix(), data)
    env = {**os.environ, 'DATABASE_BACKEND': 'sqlite', 'MYSQL_DSN': '', 'MYSQL_USER': '', 'MYSQL_DATABASE': '',
           'REDIS_URL': '', 'REDIS_HOST': '', 'AUTH_TOKENS': '', 'PAYMENT_ADAPTER': '',
           'RAG_EMBEDDING_PROVIDER': 'local', 'EMBEDDING_DIMENSIONS': '256', 'MQ_BACKEND': 'sqlite',
           'RAG_SEMANTIC_WEIGHT': '0.62', 'RAG_BM25_WEIGHT': '0.28', 'RAG_KEYWORD_WEIGHT': '0.10',
           'RAG_CANDIDATE_MULTIPLIER': '4', 'RAG_RANKING_MODE': 'hybrid_rule', 'RAG_EVIDENCE_SELECTION': '',
           'RAG_SEMANTIC_CONTEXT': '', 'SEMANTIC_ROUTE_REPAIR': '', 'SEED_DEMO_DATA': 'true',
           'MEMORY_CONTEXT_MAX_CHARS': '8000', 'MEMORY_CONTEXT_TTL_SECONDS': '1800', 'PYTHONIOENCODING': 'utf8'}
    write_json(output / 'manifest.json', manifest)
    with tempfile.TemporaryDirectory(prefix='support-five-layer-') as tmp:
        sandbox = Path(tmp)
        for path, data in files.items():
            target = sandbox / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        env.update(PYTHONPATH=str(sandbox), DATABASE_PATH=str(sandbox / 'data/evaluation.db'))
        commands = {
            'routing_tools': ['scripts.eval.run_isolated_baseline', '--suites', 'routing_v2', 'tools',
                              '--record-semantic-responses', '--record-case-results'],
            'answer_e2e': ['scripts.eval.run_isolated_baseline', '--suites', 'answer', 'e2e', '--live-replies',
                           '--record-semantic-responses', '--record-case-results'],
            'rag': ['scripts.eval.run_five_layer_eval', '--rag-child', '--profile', args.profile],
        }
        if args.profile == 'optimized':
            for name in ('routing_tools', 'answer_e2e'):
                commands[name].extend(['--semantic-related-topics', 'discard_invalid', '--policy-evidence', 'complementary'])
        def run(name):
            start = perf_counter()
            with (output / (name + '.log')).open('x', encoding='utf8') as log:
                process = subprocess.run([sys.executable, '-X', 'utf8', '-m', *commands[name],
                                          '--output', str(output / name)], cwd=sandbox, env=env,
                                         stdout=log, stderr=subprocess.STDOUT)
            return {'returncode': process.returncode, 'duration_seconds': perf_counter() - start}
        # Independent fresh DBs; only two network streams run concurrently.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {name: pool.submit(run, name) for name in commands}
            for name, future in futures.items():
                manifest['runs'][name] = future.result()
                write_json(output / 'manifest.json', manifest)
                print(name, manifest['runs'][name], flush=True)
    manifest['original_sources_unchanged'] = all((ROOT / p).read_bytes() == b for p, b in files.items())
    write_json(output / 'manifest.json', manifest)
    print('Artifacts:', output, flush=True)


if __name__ == '__main__':
    main()
