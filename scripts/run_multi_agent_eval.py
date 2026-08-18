import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.entry.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR


EVAL_PATH = BASE_DIR / "data" / "eval" / "multi_agent_eval_100.jsonl"
REPORT_DIR = BASE_DIR / "data" / "eval_reports"


def load_eval_cases() -> list[dict]:
    cases = []

    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if line:
            cases.append(json.loads(line))

    return cases


def get_tool_names(result: dict) -> list[str]:
    return [
        item.get("tool_name")
        for item in result.get("tool_results", [])
        if item.get("tool_name")
    ]


def contains_all(actual: list[str], expected: list[str]) -> tuple[bool, list[str]]:
    missing = [
        item
        for item in expected
        if item not in actual
    ]

    return len(missing) == 0, missing


def check_route(route: dict, expected_route: dict) -> tuple[bool, list[str]]:
    errors = []

    for key, expected in expected_route.items():
        actual = route.get(key)

        if actual != expected:
            errors.append(f"route.{key} expected={expected}, actual={actual}")

    return len(errors) == 0, errors


def run_single_case(case: dict) -> dict[str, Any]:
    result = run_customer_support_agent(
        user_message=case["user_message"],
        conversation_id=f"multi-agent-eval-{case['id']}-{uuid4().hex}",
        use_llm=False,
    )
    route = result.get("route", {})
    tools = get_tool_names(result)
    agents = route.get("agent_plan", [])

    route_pass, route_errors = check_route(route, case.get("expected_route", {}))
    agent_pass, missing_agents = contains_all(agents, case.get("expected_agents", []))
    tool_pass, missing_tools = contains_all(tools, case.get("expected_tools_contains", []))
    passed = route_pass and agent_pass and tool_pass
    errors = list(route_errors)

    if missing_agents:
        errors.append(f"missing_agents={missing_agents}")

    if missing_tools:
        errors.append(f"missing_tools={missing_tools}")

    return {
        "id": case["id"],
        "user_message": case["user_message"],
        "passed": passed,
        "route_pass": route_pass,
        "agent_pass": agent_pass,
        "tool_pass": tool_pass,
        "expected_agents": case.get("expected_agents", []),
        "actual_agents": agents,
        "expected_tools_contains": case.get("expected_tools_contains", []),
        "actual_tools": tools,
        "errors": errors,
        "reply": result.get("reply", ""),
    }


def build_report(results: list[dict]) -> dict:
    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "overall_pass_rate": round(passed_count / total, 4) if total else 0,
        "agent_dispatch_pass_rate": round(
            sum(1 for item in results if item["agent_pass"]) / total,
            4,
        )
        if total
        else 0,
        "tool_trigger_pass_rate": round(
            sum(1 for item in results if item["tool_pass"]) / total,
            4,
        )
        if total
        else 0,
        "failed_cases": [item for item in results if not item["passed"]],
        "results": results,
    }


def save_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"multi_agent_eval_report_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_report(report: dict, report_path: Path) -> None:
    print("=" * 60)
    print("Multi-Agent Eval Report")
    print("=" * 60)
    print(f"总样本数: {report['total']}")
    print(f"通过数量: {report['passed_count']}")
    print(f"失败数量: {report['failed_count']}")
    print(f"总体通过率: {report['overall_pass_rate']}")
    print(f"Agent 分派通过率: {report['agent_dispatch_pass_rate']}")
    print(f"工具触发通过率: {report['tool_trigger_pass_rate']}")
    print(f"报告文件: {report_path}")

    if report["failed_cases"]:
        print("\n失败用例:")
        for item in report["failed_cases"][:10]:
            print(f"- {item['id']}: {item['errors']}")

    print("=" * 60)


def main() -> None:
    cases = load_eval_cases()
    results = [run_single_case(case) for case in cases]
    report = build_report(results)
    report_path = save_report(report)
    print_report(report, report_path)


if __name__ == "__main__":
    main()
