"""Opt-in execution mode for unchanged Dev evaluators, without altering scoring."""
from functools import wraps


def install(evaluator):
    if evaluator.__name__ not in {"scripts.eval.eval_answer", "scripts.eval.eval_e2e"}:
        raise ValueError("Live replies are restricted to the existing answer/e2e Dev suites")
    original = evaluator.run_customer_support_agent

    @wraps(original)
    def run(*args, **kwargs):
        kwargs["use_llm"] = True
        return original(*args, **kwargs)

    evaluator.run_customer_support_agent = run
