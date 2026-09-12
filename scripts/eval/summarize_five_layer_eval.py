"""Summarize saved five-layer results without changing any evaluator score."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

from scripts.eval.run_isolated_baseline import diagnostics


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()] if path.exists() else []


def first_check(case, layer):
    if case.get('passed'):
        return 'passed'
    if case.get('failure_stage'):
        return case['failure_stage']
    if layer == 'routing':
        return 'semantic_route' if not case['metrics']['semantic_correct'] else 'route_derivation'
    if layer == 'tool':
        if case.get('dangerous_misuse'):
            return 'forbidden_tool'
        for field in ('selection_pass', 'argument_pass', 'execution_pass'):
            if case.get(field) is False:
                return field.removesuffix('_pass')
    return 'answer_checks'


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--fail-on-quality', action='store_true', help='Return exit code 1 if any existing evaluator case fails')
    args = parser.parse_args()
    root = args.directory.resolve()
    summary = {'layers': {}, 'failures': [], 'scope': 'Dev only; existing scores unchanged'}
    layout = {'routing': ('routing_tools', 'routing_v2'), 'tool': ('routing_tools', 'tools'),
              'answer': ('answer_e2e', 'answer'), 'e2e': ('answer_e2e', 'e2e')}
    for layer, (run, suite) in layout.items():
        folder = root / run / suite
        report = read(folder / 'reports' / f'eval_{suite}.json')
        cases = lines(folder / 'reports/case_results.jsonl')
        traces = lines(folder / 'trace.jsonl')
        by_id = {t['trace_id']: (i, t) for i, t in enumerate(traces, 1)}
        raw = lines(folder / 'reports/semantic_responses.jsonl')
        assert len(cases) == report['total_cases']
        stage_counts = Counter()
        dependency_cases, contract_cases, linked = [], [], 0
        for case in cases:
            number, trace = by_id.get(case.get('trace_id'), (None, {}))
            linked += bool(trace)
            observed = diagnostics({'results': [case]}, [trace] if trace else [])['fallbacks']
            calls = trace.get('token_usage', {}).get('llm_calls', [])
            failed_calls = [c for c in calls if not c.get('success')]
            dependency = any(f['category'] == 'dependency_unavailable' for f in observed) or any(
                any(term in c.get('error_type', '').lower() for term in ('timeout', 'connection', 'http', 'ratelimit'))
                for c in failed_calls)
            contract = any(f['category'] == 'model_output_contract' for f in observed)
            if dependency:
                dependency_cases.append(case['case_id'])
            if contract:
                contract_cases.append(case['case_id'])
            if case['passed']:
                continue
            stage = first_check(case, layer)
            stage_counts[stage] += 1
            summary['failures'].append({'layer': layer, 'case_id': case['case_id'],
                'first_failed_check': stage, 'reason': case.get('reason') or case.get('errors_by_stage'),
                'dependency_affected': dependency, 'model_contract_fallback': contract,
                'fallback_records': observed, 'failed_llm_calls': failed_calls,
                'trace_id': case.get('trace_id'), 'trace_line': number,
                'trace_file': (folder / 'trace.jsonl').relative_to(root).as_posix() if trace else None,
                'case_records': (folder / 'reports/case_results.jsonl').relative_to(root).as_posix()})
        metrics = {k: v for k, v in report.items() if k.endswith(('accuracy', '_rate'))}
        if 'deterministic_metrics' in report:
            metrics['deterministic_metrics'] = report['deterministic_metrics']
        calls = [c for t in traces for c in t.get('token_usage', {}).get('llm_calls', [])]
        reported = [c['provider_usage'] for c in calls if c.get('provider_usage') is not None]
        durations = [t['duration_ms'] for t in traces if isinstance(t.get('duration_ms'), (int, float))]
        summary['layers'][layer] = {
            'passed': report['passed_count'], 'total': report['total_cases'], 'metrics': metrics,
            'first_failed_checks': dict(stage_counts), 'dependency_affected_cases': dependency_cases,
            'model_contract_fallback_cases': contract_cases, 'trace_linked_cases': linked,
            'trace_records': len(traces), 'mean_traced_request_ms': statistics.mean(durations) if durations else None,
            'semantic_calls_recorded': len(raw), 'semantic_call_errors': dict(Counter(r['error'] for r in raw if r.get('error'))),
            'traced_chat_calls': len(calls), 'traced_chat_failed_calls': sum(not c.get('success') for c in calls),
            'traced_provider_tokens': {k: sum(r[k] for r in reported) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} if reported else None,
            'provider_usage_scope': 'Traced calls only; direct routing/permission calls may lack usage. Not total billing.',
            'dangerous_tool_misuse_count': report.get('dangerous_tool_misuse_count'),
            'report': (folder / 'reports' / f'eval_{suite}.json').relative_to(root).as_posix()}
    for label in ('v1_dev20', 'v2_dev6'):
        saved = read(root / 'rag' / (label + '.json'))
        report = saved['report']
        summary['layers']['rag_' + label] = {'passed': report['passed_count'], 'total': report['total_cases'],
            'metrics': {k:report[k] for k in ('hit_at_1','hit_at_3','hit_at_5','mrr','candidate_evidence_recall_at_20','evidence_coverage_rate')},
            'mean_retrieval_ranking_ms': statistics.mean(t['duration_ms'] for t in saved['traces']),
            'kb_version': saved['kb_version'], 'report': 'rag/' + label + '.json'}
        if label == 'v2_dev6':
            summary['layers']['rag_' + label]['requirements'] = {
                'hit': sum(r['evidence_report']['satisfied_requirements'] for r in report['results']),
                'total': sum(r['evidence_report']['total_requirements'] for r in report['results'])}
        for case in report['results']:
            if not case['passed']:
                summary['failures'].append({'layer': 'rag_' + label, 'case_id': case['case_id'],
                    'first_failed_check': case.get('failure_stage') or case.get('failure_type'),
                    'reason': case.get('reason'), 'expected_rank': case.get('expected_rank'),
                    'candidate_expected_rank': case.get('candidate_expected_rank'),
                    'coverage': case.get('evidence_coverage_rate'),
                    'candidate_coverage': case.get('candidate_evidence_coverage_rate'),
                    'report': 'rag/' + label + '.json'})
    summary['all_evaluations_passed'] = all(v['passed'] == v['total'] for v in summary['layers'].values())
    summary['evaluation_completed'] = True
    (root / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')
    for layer, value in summary['layers'].items():
        print(layer, value['passed'], '/', value['total'], value['metrics'])
    if args.fail_on_quality and not summary['all_evaluations_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
