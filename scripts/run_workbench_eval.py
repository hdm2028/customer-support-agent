import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR


EVAL_PATH = BASE_DIR / "data" / "eval" / "workbench_eval.jsonl"
REPORT_DIR = BASE_DIR / "data" / "eval_reports"


def load_eval_cases() -> list[dict]:
    """读取客服工作台评测集。"""

    cases = []

    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if line:
            cases.append(json.loads(line))

    return cases


def get_tool_results(result: dict) -> list[dict]:
    """提取工具结果。"""

    return result.get("tool_results", [])


def get_tool_names(result: dict) -> list[str]:
    """提取工具调用顺序。"""

    return [
        item.get("tool_name")
        for item in get_tool_results(result)
        if item.get("tool_name")
    ]


def find_tool_result(result: dict, tool_name: str) -> dict | None:
    """按工具名查找工具结果。"""

    for item in get_tool_results(result):
        if item.get("tool_name") == tool_name:
            return item

    return None


def infer_final_action(result: dict) -> str:
    """归纳客服工作台最终动作。"""

    if find_tool_result(result, "transfer_to_human"):
        return "handoff"

    if find_tool_result(result, "send_goods_link"):
        return "send_goods_link"

    if find_tool_result(result, "get_quick_reply"):
        return "quick_reply"

    if find_tool_result(result, "get_shop_products"):
        return "product_answer"

    return "reply_only"


def check_tools(result: dict, case: dict) -> tuple[bool, list[str]]:
    """检查工具调用是否符合预期。"""

    actual_tools = get_tool_names(result)
    expected_tools = case.get("expected_tools", [])

    if actual_tools != expected_tools:
        return False, [f"tools expected={expected_tools}, actual={actual_tools}"]

    return True, []


def check_reply(result: dict, case: dict) -> tuple[bool, list[str]]:
    """检查回复约束。"""

    reply = result.get("reply", "")
    errors = []

    for phrase in case.get("must_include", []):
        if phrase not in reply:
            errors.append(f"reply missing phrase: {phrase}")

    for phrase in case.get("must_not_include", []):
        if phrase in reply:
            errors.append(f"reply contains forbidden phrase: {phrase}")

    return len(errors) == 0, errors


def run_single_case(case: dict) -> dict[str, Any]:
    """执行单条工作台评测用例。"""

    result = run_customer_support_agent(
        user_message=case["user_message"],
        use_llm=False,
    )
    tools_pass, tool_errors = check_tools(result, case)
    reply_pass, reply_errors = check_reply(result, case)
    actual_final_action = infer_final_action(result)
    action_pass = actual_final_action == case.get("expected_final_action")
    errors = tool_errors + reply_errors

    if not action_pass:
        errors.append(
            f"final_action expected={case.get('expected_final_action')}, actual={actual_final_action}"
        )

    return {
        "id": case["id"],
        "scenario": case["scenario"],
        "user_message": case["user_message"],
        "passed": tools_pass and reply_pass and action_pass,
        "tools_pass": tools_pass,
        "reply_pass": reply_pass,
        "action_pass": action_pass,
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": get_tool_names(result),
        "expected_final_action": case.get("expected_final_action"),
        "actual_final_action": actual_final_action,
        "route": result.get("route", {}),
        "reply": result.get("reply", ""),
        "errors": errors,
    }


def build_report(results: list[dict]) -> dict:
    """汇总工作台评测指标。"""

    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    tools_pass_count = sum(1 for item in results if item["tools_pass"])
    action_pass_count = sum(1 for item in results if item["action_pass"])
    reply_pass_count = sum(1 for item in results if item["reply_pass"])

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "overall_pass_rate": round(passed_count / total, 4) if total else 0,
        "tools_pass_rate": round(tools_pass_count / total, 4) if total else 0,
        "action_pass_rate": round(action_pass_count / total, 4) if total else 0,
        "reply_pass_rate": round(reply_pass_count / total, 4) if total else 0,
        "failed_cases": [item for item in results if not item["passed"]],
        "results": results,
    }


def save_report(report: dict) -> Path:
    """保存工作台评测报告。"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"workbench_eval_report_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_report(report: dict, report_path: Path) -> None:
    """打印工作台评测报告。"""

    print("=" * 60)
    print("Customer Support Workbench Eval Report")
    print("=" * 60)
    print(f"总样本数: {report['total']}")
    print(f"通过数量: {report['passed_count']}")
    print(f"失败数量: {report['failed_count']}")
    print(f"总体通过率: {report['overall_pass_rate']}")
    print(f"工具通过率: {report['tools_pass_rate']}")
    print(f"最终动作通过率: {report['action_pass_rate']}")
    print(f"回复约束通过率: {report['reply_pass_rate']}")
    print(f"报告文件: {report_path}")

    if report["failed_cases"]:
        print("\n失败用例:")
        for item in report["failed_cases"]:
            print("-" * 60)
            print(f"id: {item['id']}")
            print(f"scenario: {item['scenario']}")
            print(f"errors: {item['errors']}")
            print(f"actual_tools: {item['actual_tools']}")
            print(f"reply: {item['reply']}")

    print("=" * 60)


def main() -> None:
    cases = load_eval_cases()
    results = [
        run_single_case(case)
        for case in cases
    ]
    report = build_report(results)
    report_path = save_report(report)

    print_report(report, report_path)


if __name__ == "__main__":
    main()
