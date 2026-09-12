"""Compare preserved evaluator outputs without rescoring or model calls."""
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
A = ROOT / 'reports/five_layer_eval_20260909'
B = HERE / 'live_final'


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line]


def compare_cases(a, b):
    left = {r['case_id']: r for r in a}
    right = {r['case_id']: r for r in b}
    assert len(left) == len(a) and len(right) == len(b) and left.keys() == right.keys()
    return {'total': len(left),
            'recovered': [k for k in left if not left[k]['passed'] and right[k]['passed']],
            'regressed': [k for k in left if left[k]['passed'] and not right[k]['passed']],
            'cases': [{'case_id': k, 'a': left[k], 'b': right[k]} for k in left]}


def main():
    global B
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--b', type=Path, default=B)
    parser.add_argument('--output', type=Path, default=HERE / 'comparison.json')
    args = parser.parse_args()
    B = args.b.resolve()
    ma, mb = read(A / 'manifest.json'), read(B / 'manifest.json')
    frozen = {k: v for k, v in ma['source_files'].items() if k.startswith('data/')}
    assert frozen == {k: v for k, v in mb['source_files'].items() if k.startswith('data/')}
    output = {'a': str(A), 'b': str(B), 'dataset_knowledge_order_bytes_equal': True,
              'interpretation': 'Live model responses can vary. Frozen response replay isolates the routing/tool intervention.',
              'layers': {}, 'frozen_response_control': {}}
    for layer, relative in {
        'routing': 'routing_tools/routing_v2/reports/case_results.jsonl',
        'tool': 'routing_tools/tools/reports/case_results.jsonl',
        'answer': 'answer_e2e/answer/reports/case_results.jsonl',
        'e2e': 'answer_e2e/e2e/reports/case_results.jsonl',
    }.items():
        output['layers'][layer] = compare_cases(rows(A / relative), rows(B / relative))
    for label in ('v1_dev20', 'v2_dev6'):
        a, b = read(A / f'rag/{label}.json'), read(B / f'rag/{label}.json')
        assert a['kb_version'] == b['kb_version']
        compared = compare_cases(a['report']['results'], b['report']['results'])
        ta = {t['case_id']: t for t in a['traces']}
        tb = {t['case_id']: t for t in b['traces']}
        for case in compared['cases']:
            k = case['case_id']
            # Check full ordered candidate records, not merely matching sources.
            case['candidate_pool_equal'] = ta[k]['candidates'] == tb[k]['candidates']
            case['query_equal'] = ta[k]['query'] == tb[k]['query']
            case['final_a'] = [c['chunk_id'] for c in ta[k]['final']]
            case['final_b'] = [c['chunk_id'] for c in tb[k]['final']]
            case['duration_ms'] = {'a': ta[k]['duration_ms'], 'b': tb[k]['duration_ms']}
            assert case['candidate_pool_equal'] and case['query_equal']
        output['layers']['rag_' + label] = compared
    for layer, suite in (('routing', 'routing_v2'), ('tool', 'tools')):
        compared = compare_cases(rows(A / f'routing_tools/{suite}/reports/case_results.jsonl'),
                                 rows(HERE / f'frozen_final/{suite}/reports/case_results.jsonl'))
        verification = read(HERE / f'frozen_final/{suite}/reports/semantic_replay_verification.json')
        assert verification['valid']
        compared['replay_verification'] = verification
        if suite == 'tools':
            compared['dangerous_misuse_a'] = sum(bool(c['a']['dangerous_misuse']) for c in compared['cases'])
            compared['dangerous_misuse_b'] = sum(bool(c['b']['dangerous_misuse']) for c in compared['cases'])
        output['frozen_response_control'][layer] = compared
    with args.output.open('x', encoding='utf8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    for label, layer in output['layers'].items():
        print(label, 'recovered', layer['recovered'], 'regressed', layer['regressed'])


if __name__ == '__main__':
    main()
