"""Versioned diagnostic rescore of explicit denials; original reports untouched.

Only unambiguous local negation is recognized. Questions, quotations and
ambiguous/double negatives retain the conservative literal-match result.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

from scripts.eval.eval_answer import build_report

VERSION = 'explicit-denial-v1'
NEGATION = re.compile(r'不会|不能|不可以|不得|不代表|不意味着|不保证|不承诺|无法保证|无法承诺|尚未|并未|并非|不是')
BOUNDARY = re.compile(r'[。！？!?；;，,\n]')


def forbidden_occurrences(reply, phrases):
    observations = []
    for phrase in phrases:
        if not phrase:
            continue
        for found in re.finditer(re.escape(phrase), reply):
            prefix = BOUNDARY.split(reply[:found.start()])[-1]
            negatives = list(NEGATION.finditer(prefix))
            denied = False
            if len(negatives) == 1:
                marker = negatives[0]
                gap = prefix[marker.end():]
                # Reject double negation, intervening propositions, quotes and
                # punctuation; only a short local scope can exempt a match.
                denied = (len(gap) <= 12 and not re.search(r'[不未非无否难"“”‘’\'：:]', gap)
                          and not re.search(r'[不未非无否难]', prefix[:marker.start()])
                          and not re.search(r'["“”‘’\']|如果|假设|假如|若', prefix)
                          and not re.match(r'[^。！!；;\n]*[？?]', reply[found.end():]))
            observations.append({'phrase': phrase, 'start': found.start(), 'end': found.end(),
                                 'explicitly_denied': denied})
    return observations


def rescore(original):
    results = []
    for row in original['results']:
        result = deepcopy(row)
        observed = forbidden_occurrences(row['reply'], row['forbidden_keywords'])
        present = list(dict.fromkeys(o['phrase'] for o in observed if not o['explicitly_denied']))
        result['literal_passed'] = row['passed']
        result['forbidden_occurrences'] = observed
        result['hallucinated_keywords'] = present
        result['deterministic_metrics']['hallucination_free'] = not present
        metrics = result['deterministic_metrics']
        result['passed'] = all(metrics[k] for k in ('correctness', 'relevance', 'completeness', 'hallucination_free')) and metrics['faithfulness_groundedness'] is not False
        reasons = [] if row['reason'] == 'passed' else [r for r in row['reason'].split('; ') if not r.startswith('forbidden_keywords_present=')]
        if present:
            reasons.append(f'forbidden_keywords_present={present}')
        result['reason'] = '; '.join(reasons) or 'passed'
        results.append(result)
    report = build_report(results)
    report.update(scoring_version=VERSION, original_passed_count=original['passed_count'],
                  interpretation='Scoring correction on the same saved replies, not a system quality improvement')
    return report


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.input.read_text(encoding='utf8'))
    report = rescore(original)
    report['original_report'] = str(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print('Same replies, literal:', original['passed_count'], 'negation-aware:', report['passed_count'], '/', report['total_cases'])


if __name__ == '__main__':
    main()
