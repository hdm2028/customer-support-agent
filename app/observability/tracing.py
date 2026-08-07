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
def start_trace(user_message: str, conversation_id: str | None = None) -> str:
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
        "_start_perf":perf_counter(),
    }

# 往trace里追加事件
def add_trace_event(trace: dict, event_type: str, data: dict) -> None:
    """往trace中追加事件，比如route_tools，execute_tools，fallback_answer，run_customer_support_agent"""
    trace["events"].append({"event_type": event_type, "message": data, "timestamp": now_iso()})

# 标记结束时间，计算总耗时
def finish_trace(trace: dict, reply:str,success: bool) -> dict:
    """结束trace，记录总耗时和最终回复"""
    duration_ms = int ((perf_counter() - trace["_start_perf"]))
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