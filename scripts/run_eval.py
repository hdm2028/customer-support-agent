import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR
EVAL_PATH = BASE_DIR / "data" / "eval" / "customer_support_eval.jsonl"
REPORT_DIR = BASE_DIR / "data" / "eval_reports"


def load_eval_cases() -> list[dict]:
    """读取eval数据集，一行JSON代表一个测试用例"""
    cases =[]
    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))
    return cases
def get_tool_names(result: dict) -> list[str]:
    """从agent返回结果中提取实际调用的工具名"""
    tool_results = result.get("tool_results", [])
    return [item.get("tool_name") for item in tool_results if item.get("tool_name")]

def check_route_match(actual_route: dict, expected_route: dict) ->tuple[bool, list[str]]:
    """检查实际路由是否符合预期"""
    errors =[]
    for key,expected_value in expected_route.items():
        actual_value = actual_route.get(key)
        if actual_value != expected_value:
            errors.append(f"route.{key} expected={expected_value}, actual={actual_value}")
    return (len(errors) == 0, errors)
def check_tools_match(actual_tools: list[str], expected_tools: list[str]) -> tuple[bool, list[str]]:
    """检查实际工具调用列表是否符合预期。"""

    errors = []

    actual_set = set(actual_tools)
    expected_set = set(expected_tools)

    missing_tools = expected_set - actual_set
    extra_tools = actual_set - expected_set

    if missing_tools:
        errors.append(f"missing_tools={sorted(missing_tools)}")

    if extra_tools:
        errors.append(f"extra_tools={sorted(extra_tools)}")

    return len(errors) == 0, errors
def run_single_case(case: dict) -> dict:
    """执行单条 eval case，并返回评估结果"""
    result = run_customer_support_agent(
        user_message=case["user_message"],
        use_llm=False,
    )
    actual_route = result.get("route",{})
    actual_tools = get_tool_names(result)

    route_pass,route_errors = check_route_match(
         actual_route=actual_route,
        expected_route=case.get("expected_route", {}),
    )
    tools_pass, tools_errors = check_tools_match(
        actual_tools=actual_tools,
        expected_tools=case.get("expected_tools", []),
    )
    passed = route_pass and tools_pass
    return {
        "id": case["id"],
        "intent": case["intent"],
        "user_message": case["user_message"],
        "passed": passed,
        "route_pass": route_pass,
        "tools_pass": tools_pass,
        "actual_route": actual_route,
        "expected_route": case.get("expected_route", {}),
        "actual_tools": actual_tools,
        "expected_tools": case.get("expected_tools", []),
        "errors": route_errors + tools_errors,
        "reply": result.get("reply", ""),
        "notes": case.get("notes", ""),
    }
def build_report(results: list[dict]) -> dict:
    """汇总所有 eval case 的结果，生成整体报告。"""

    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    route_pass_count = sum(1 for item in results if item["route_pass"])
    tools_pass_count = sum(1 for item in results if item["tools_pass"])

    failed_cases = [
        item for item in results
        if not item["passed"]
    ]
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "overall_pass_rate": round(passed_count / total, 4) if total else 0,
        "route_pass_rate": round(route_pass_count / total, 4) if total else 0,
        "tools_pass_rate": round(tools_pass_count / total, 4) if total else 0,
        "failed_cases": failed_cases,
        "results": results,
    }
def save_report(report: dict) -> Path:
    """把 eval 报告保存到 data/eval_reports。"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"eval_report_{timestamp}.json"

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_report(report: dict, report_path: Path) -> None:
    """把核心评估结果打印到终端。"""

    print("=" * 60)
    print("Customer Support Agent Eval Report")
    print("=" * 60)
    print(f"总样本数：{report['total']}")
    print(f"通过数量：{report['passed_count']}")
    print(f"失败数量：{report['failed_count']}")
    print(f"总体通过率：{report['overall_pass_rate']}")
    print(f"路由通过率：{report['route_pass_rate']}")
    print(f"工具通过率：{report['tools_pass_rate']}")
    print(f"报告文件：{report_path}")

    if report["failed_cases"]:
        print("\n失败用例：")

        for item in report["failed_cases"]:
            print("-" * 60)
            print(f"id: {item['id']}")
            print(f"intent: {item['intent']}")
            print(f"user_message: {item['user_message']}")
            print(f"errors: {item['errors']}")
            print(f"actual_route: {item['actual_route']}")
            print(f"actual_tools: {item['actual_tools']}")
            print(f"expected_tools: {item['expected_tools']}")

    print("=" * 60)


def main() -> None:
    cases = load_eval_cases()

    results = []

    for case in cases:
        result = run_single_case(case)
        results.append(result)

    report = build_report(results)
    report_path = save_report(report)

    print_report(report, report_path)


if __name__ == "__main__":
    main()
