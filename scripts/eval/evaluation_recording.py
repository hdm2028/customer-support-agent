"""Record unchanged evaluator case results, including successful routing cases."""
from functools import wraps
import json
from pathlib import Path


def install(evaluator):
    original = evaluator.run_single_case
    target = Path('reports/case_results.jsonl')
    target.parent.mkdir(parents=True, exist_ok=True)

    @wraps(original)
    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        with target.open('a', encoding='utf8') as log:
            log.write(json.dumps(result, ensure_ascii=False) + '\n')
        return result

    evaluator.run_single_case = run
