"""Provider-reported LLM usage is distinct from local text estimates."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_trace = ContextVar("llm_usage_trace", default=None)


@contextmanager
def usage_scope(trace):
    token = _trace.set(trace)
    try:
        yield
    finally:
        _trace.reset(token)


def begin_call(model, messages, *, trace=None):
    trace = trace if trace is not None else _trace.get()
    if trace is None:
        return None
    from app.observability.tracing import estimate_tokens
    call = {"model": model, "provider_usage": None, "success": False,
            "prompt_tokens_estimated": sum(estimate_tokens(m.get("content", "")) for m in messages),
            "completion_tokens_estimated": 0, "_start_perf": perf_counter()}
    trace.setdefault("token_usage", {}).setdefault("llm_calls", []).append(call)
    return call


def provider_usage(call, usage):
    if call is None or not isinstance(usage, dict):
        return
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys):
        # Streaming chunks may repeat cumulative usage: retain the last report.
        call["provider_usage"] = {key: usage[key] for key in keys}


def end_call(call, text, *, success, error_type=None):
    if call is None:
        return
    from app.observability.tracing import estimate_tokens
    call.update(success=success, completion_tokens_estimated=estimate_tokens(text),
                duration_ms=round((perf_counter() - call.pop("_start_perf")) * 1000, 2))
    if error_type:
        call["error_type"] = error_type


def summarize_usage(trace):
    usage = trace.setdefault("token_usage", {})
    calls = usage.get("llm_calls", [])
    reported = [call["provider_usage"] for call in calls if call.get("provider_usage") is not None]
    usage["provider_reported"] = {
        "scope": "chat_completions_in_this_request_excluding_embeddings",
        "call_count": len(calls), "reported_calls": len(reported),
        "unreported_calls": len(calls) - len(reported),
        "complete": bool(calls) and len(calls) == len(reported),
        "tokens": {key: sum(item[key] for item in reported)
                   for key in ("prompt_tokens", "completion_tokens", "total_tokens")} if reported else None,
    }
    usage["estimate_scope"] = "final_answer_context_and_reply_not_billable_usage"
