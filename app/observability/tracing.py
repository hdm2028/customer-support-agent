import json
from datetime import datetime
from time import perf_counter
from uuid import uuid4

from app.core.config import BASE_DIR
TRACE_PATH = BASE_DIR / "data" / "traces" / "agent_trace.jsonl"

def now_iso() -> str:
    """返回当前时间为字符串，方便trace中记录每个事件发生时间"""
    return datetime.now().isoformat(timespec="milliseconds")
#创建一次trace
def start_trace(user_message: str, conversation_id: str | None = None) -> dict:
    """创建一次Agent请求的trace记录"""
    return{
        "trace_id": str(uuid4()),
        "conversation_id": conversation_id,
        "user_message": user_message,
        "start_at": now_iso(),
        "finish_at": None,
        "duration_ms": None,
        "success": None,
        "events": [],
        "timings": {},
        "token_usage": {
            "prompt_tokens_estimated": 0,
            "completion_tokens_estimated": 0,
            "total_tokens_estimated": 0,
            "source": "local_estimate",
        },
        "_start_perf":perf_counter(),
    }

# 往trace里追加事件
def add_trace_event(trace: dict, event_type: str, data: dict) -> None:
    """往 trace 中追加事件，比如 route、agent_dispatch、tool_result 和 reply。"""
    trace["events"].append(
        {
            "trace_id": trace.get("trace_id"),
            "event_type": event_type,
            "message": data,
            "timestamp": now_iso(),
        }
    )


def add_trace_timing(trace: dict, step_name: str, duration_ms: float, data: dict | None = None) -> None:
    """记录某个执行阶段的耗时，并同步写入 events，方便前端和脚本统一读取。"""

    timing = {
        "step": step_name,
        "duration_ms": round(duration_ms, 2),
        **(data or {}),
    }
    trace.setdefault("timings", {})[step_name] = timing
    add_trace_event(trace, event_type="timing", data=timing)


def estimate_tokens(text: str) -> int:
    """粗略估算 token，适合无真实 LLM usage 时做成本趋势分析。"""

    if not text:
        return 0

    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_chars = max(len(text) - chinese_chars, 0)

    return chinese_chars + max(1, other_chars // 4)


def set_prompt_token_usage(trace: dict, messages: list[dict]) -> None:
    prompt_tokens = sum(estimate_tokens(item.get("content", "")) for item in messages)
    trace.setdefault("token_usage", {})["prompt_tokens_estimated"] = prompt_tokens
    trace["token_usage"]["total_tokens_estimated"] = (
        prompt_tokens + trace["token_usage"].get("completion_tokens_estimated", 0)
    )
    add_trace_event(
        trace,
        event_type="token_usage",
        data=trace["token_usage"],
    )


def set_completion_token_usage(trace: dict, reply: str) -> None:
    completion_tokens = estimate_tokens(reply)
    trace.setdefault("token_usage", {})["completion_tokens_estimated"] = completion_tokens
    trace["token_usage"]["total_tokens_estimated"] = (
        trace["token_usage"].get("prompt_tokens_estimated", 0) + completion_tokens
    )
    add_trace_event(
        trace,
        event_type="token_usage",
        data=trace["token_usage"],
    )


def timed_step(trace: dict, step_name: str, callback, data: dict | None = None):
    """执行一个步骤并记录耗时。"""

    start = perf_counter()

    try:
        result = callback()
    except Exception as error:
        add_trace_timing(
            trace,
            step_name,
            (perf_counter() - start) * 1000,
            {
                "success": False,
                "error_type": type(error).__name__,
                "error_message": str(error),
                **(data or {}),
            },
        )
        raise

    add_trace_timing(
        trace,
        step_name,
        (perf_counter() - start) * 1000,
        {
            "success": True,
            **(data or {}),
        },
    )

    return result


def build_timing_event(trace: dict, step_name: str, conversation_id: str) -> dict | None:
    """把 trace 中某个步骤的耗时包装成前端可以展示的 SSE 事件。"""

    timing = trace.get("timings", {}).get(step_name)

    if not timing:
        return None

    return {
        "type": "timing",
        "content": timing,
        "conversation_id": conversation_id,
    }

# 标记结束时间，计算总耗时
def finish_trace(trace: dict, reply:str,success: bool) -> dict:
    """结束trace，记录总耗时和最终回复"""
    duration_ms = round((perf_counter() - trace["_start_perf"]) * 1000, 2)
    trace["finish_at"] = now_iso()
    trace["duration_ms"] = duration_ms
    trace["success"] = success
    trace["reply"] = reply

    trace.pop("_start_perf",None)
    return trace

# 写入jsonl文件 
def save_trace(trace: dict) -> None:
    """将trace写入jsonl文件"""
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_PATH.open("a",encoding= "utf-8") as f:
         f.write(json.dumps(trace, ensure_ascii=False) + "\n")

    try:
        from app.storage.database import save_agent_metric_to_db

        save_agent_metric_to_db(trace)
    except Exception:
        pass
