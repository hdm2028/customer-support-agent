import json
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import BASE_DIR


TRACE_PATH = BASE_DIR / "data" / "traces" / "agent_trace.jsonl"


def load_traces() -> list[dict]:
    """读取所有 trace"""
    traces = []
    if not TRACE_PATH.exists():
        return traces
    for line in TRACE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        traces.append(json.loads(line))
    return traces

def get_event(trace: dict, event_type: str) -> dict | None:
    """获取 trace 中指定类型的事件"""
    for event in trace.get("events",[]):
        if event.get("event_type") == event_type:
            return event
    return None

def get_event_payload(event: dict) -> dict:
    """兼容不同 trace 字段名：有的事件内容叫 data，有的叫 message。"""
    return event.get("data") or event.get("message") or {}

def analyze_basic_stats(traces:list[dict]) -> dict:
    """统计请求数量、成功率、平均耗时。"""
    total = len(traces)
    success_count = sum(1 for trace in traces if trace.get("success") is True)
    failed_count = total - success_count
    durations=[
        trace.get("duration_ms",0)
        for trace in traces
        if isinstance(trace.get("duration_ms"), int | float)
    ]
    avg_duration = sum(durations) / len(durations) if durations else 0
    return {
        "total": total,
        "success_count": success_count,
        "failed_count": failed_count,
        "success_rate": round(success_count / total, 4) if total else 0,
        "avg_duration_ms": round(avg_duration, 2),
    }

def analyze_route_stats(traces:list[dict]) -> dict:
    """统计路由使用情况。"""
    route_counts = Counter()
    for trace in traces:
        route_event = get_event(trace, "route")
        if not route_event:
            continue
        route = get_event_payload(route_event)
        if route.get("need_order"):
            route_counts["need_order"] += 1
        if route.get("need_policy"):
            route_counts["need_policy"] += 1
        if route.get("need_ticket"):
            route_counts["need_ticket"] += 1
        if route.get("blocked_by_guardrail"):
            route_counts["blocked_by_guardrail"] += 1
    return dict(route_counts)

def analyze_tool_stats(traces:list[dict]) -> dict:
    """统计工具调用次数和工具成功次数。"""
    tool_call_counts = Counter()
    tool_success_counts = Counter()
    for trace in traces:
        tool_event= get_event(trace, "tool_results")
        if not tool_event:
            continue
        items = get_event_payload(tool_event).get("items", [])

        for item in items:
            tool_name = item.get("tool_name", "unknown")
            tool_call_counts[tool_name] += 1

            if item.get("success") is True:
                tool_success_counts[tool_name] += 1

    return {
        "tool_calls": dict(tool_call_counts),
        "tool_success": dict(tool_success_counts),
    }

def analyze_reply_modes(traces: list[dict]) -> dict:
    """统计回复是 fallback 生成还是 LLM 生成。"""

    counter = Counter()

    for trace in traces:
        reply_event = get_event(trace, "reply")

        if not reply_event:
            continue

        reply_mode = get_event_payload(reply_event).get("reply_mode", "unknown")
        counter[reply_mode] += 1

    return dict(counter)


def analyze_timing_stats(traces: list[dict]) -> dict:
    """按阶段统计平均耗时，帮助定位 Agent 慢在哪个步骤。"""

    timing_values = {}

    for trace in traces:
        for step, timing in trace.get("timings", {}).items():
            duration_ms = timing.get("duration_ms")

            if not isinstance(duration_ms, int | float):
                continue

            timing_values.setdefault(step, []).append(duration_ms)

    return {
        step: {
            "count": len(values),
            "avg_ms": round(sum(values) / len(values), 2),
            "max_ms": round(max(values), 2),
        }
        for step, values in sorted(timing_values.items())
    }


def print_report(report: dict) -> None:
    """把分析结果打印成方便阅读的文本报告。"""

    print("=" * 60)
    print("Agent Trace 分析报告")
    print("=" * 60)

    basic = report["basic"]
    print(f"请求总数：{basic['total']}")
    print(f"成功请求：{basic['success_count']}")
    print(f"失败请求：{basic['failed_count']}")
    print(f"成功率：{basic['success_rate']}")
    print(f"平均耗时：{basic['avg_duration_ms']} ms")

    print("\n路由触发次数：")
    for key, value in report["routes"].items():
        print(f"- {key}: {value}")

    print("\n工具调用次数：")
    for key, value in report["tools"]["tool_calls"].items():
        success = report["tools"]["tool_success"].get(key, 0)
        print(f"- {key}: 调用 {value} 次，成功 {success} 次")

    print("\n回复模式：")
    for key, value in report["reply_modes"].items():
        print(f"- {key}: {value}")

    print("\n阶段耗时：")
    for key, value in report["timings"].items():
        print(f"- {key}: 平均 {value['avg_ms']} ms，最大 {value['max_ms']} ms，样本 {value['count']} 次")

    print("=" * 60)
def main() -> None:
    traces = load_traces()

    if not traces:
        print(f"没有找到 trace 文件：{TRACE_PATH}")
        return

    report = {
        "basic": analyze_basic_stats(traces),
        "routes": analyze_route_stats(traces),
        "tools": analyze_tool_stats(traces),
        "reply_modes": analyze_reply_modes(traces),
        "timings": analyze_timing_stats(traces),
    }

    print_report(report)


if __name__ == "__main__":
    main()
