import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.entry.agent_core import run_customer_support_agent
from app.core.config import BASE_DIR


EVAL_PATH = BASE_DIR / "data" / "eval" / "e2e_eval.jsonl"
REPORT_DIR = BASE_DIR / "data" / "eval_reports"


def load_eval_cases() -> list[dict]:
    """读取端到端评估数据集；每一行 JSON 表示一条完整业务链路测试。"""

    cases = []

    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        cases.append(json.loads(line))

    return cases


def get_tool_results(result: dict) -> list[dict]:
    """从 Agent 返回结果里取出工具执行结果，兼容 Pydantic dict 和普通 dict。"""

    return result.get("tool_results", [])


def get_tool_names(result: dict) -> list[str]:
    """按实际执行顺序提取工具名，用来判断工具是否多调、少调或顺序错误。"""

    return [
        item.get("tool_name")
        for item in get_tool_results(result)
        if item.get("tool_name")
    ]


def find_tool_result(result: dict, tool_name: str) -> dict | None:
    """根据工具名查找某个工具的执行结果。"""

    for item in get_tool_results(result):
        if item.get("tool_name") == tool_name:
            return item

    return None


def check_route(result: dict, case: dict) -> tuple[bool, list[str]]:
    """检查 Router 输出是否符合预期，包括订单号和关键布尔字段。"""

    errors = []
    route = result.get("route", {})
    expected_order_id = case.get("expected_order_id")

    if route.get("order_id") != expected_order_id:
        errors.append(
            f"route.order_id expected={expected_order_id}, actual={route.get('order_id')}"
        )

    for key, expected_value in case.get("expected_route", {}).items():
        actual_value = route.get(key)

        if actual_value != expected_value:
            errors.append(
                f"route.{key} expected={expected_value}, actual={actual_value}"
            )

    return len(errors) == 0, errors


def check_tools(result: dict, case: dict) -> tuple[bool, list[str]]:
    """检查工具调用链路：工具名、顺序、工具成功状态，以及订单优先原则。"""

    errors = []
    actual_tools = get_tool_names(result)
    expected_tools = case.get("expected_tools", [])

    if actual_tools != expected_tools:
        errors.append(f"tools expected={expected_tools}, actual={actual_tools}")

    expected_tool_success = case.get("expected_tool_success", {})
    for tool_name, expected_success in expected_tool_success.items():
        tool_result = find_tool_result(result, tool_name)

        if not tool_result:
            errors.append(f"missing tool result: {tool_name}")
            continue

        if tool_result.get("success") != expected_success:
            errors.append(
                f"{tool_name}.success expected={expected_success}, "
                f"actual={tool_result.get('success')}"
            )

    if "order_lookup" in actual_tools and actual_tools[0] != "order_lookup":
        errors.append("order_lookup must run before policy_search or create_ticket")

    order_lookup_result = find_tool_result(result, "order_lookup")
    if order_lookup_result and not order_lookup_result.get("success"):
        downstream_tools = [
            tool_name
            for tool_name in actual_tools
            if tool_name in {"policy_search", "ticket_decision", "create_ticket"}
        ]

        if downstream_tools:
            errors.append(
                f"order lookup failed, but downstream tools still ran: {downstream_tools}"
            )

    return len(errors) == 0, errors


def infer_final_action(result: dict) -> str:
    """把完整执行结果归纳成一个业务动作，方便和标注答案做对比。"""

    route = result.get("route", {})
    tool_names = get_tool_names(result)
    create_ticket_result = find_tool_result(result, "create_ticket")
    ticket_decision_result = find_tool_result(result, "ticket_decision")
    order_lookup_result = find_tool_result(result, "order_lookup")
    refund_result = find_tool_result(result, "refund_apply")
    manual_review_result = find_tool_result(result, "create_manual_review")

    if route.get("blocked_by_guardrail"):
        return "blocked_by_guardrail"

    if route.get("need_clarification"):
        if route.get("order_id"):
            return "ask_for_missing_slot"
        return "ask_for_order_id"

    if route.get("handoff_required") and not route.get("order_id") and not tool_names:
        return "handoff_without_tools"

    if order_lookup_result and not order_lookup_result.get("success"):
        return "order_lookup_failed"

    if ticket_decision_result and not ticket_decision_result.get("success"):
        return "ticket_blocked"

    if refund_result and refund_result.get("success"):
        refund = refund_result.get("result", {})
        if refund.get("status") == "pending_manual_review":
            return "refund_pending_manual_review"
        return "refund_queued"

    if manual_review_result and manual_review_result.get("success"):
        return "manual_review_created"

    if create_ticket_result and create_ticket_result.get("success"):
        ticket = create_ticket_result.get("result", {})
        if ticket.get("status") == "pending_human_review":
            return "create_ticket_pending_review"
        return "create_ticket"

    if find_tool_result(result, "policy_search"):
        return "policy_answer"

    return "reply_only"


def check_final_action(result: dict, case: dict) -> tuple[bool, list[str]]:
    """检查最终业务动作是否符合预期。"""

    expected_action = case.get("expected_final_action")

    if not expected_action:
        return True, []

    actual_action = infer_final_action(result)

    if actual_action != expected_action:
        return False, [
            f"final_action expected={expected_action}, actual={actual_action}"
        ]

    return True, []


def get_policy_citations(result: dict) -> list[str]:
    """提取本轮 RAG 检索返回的 citation，用于检查最终回答是否使用了证据来源。"""

    policy_result = find_tool_result(result, "policy_search")

    if not policy_result or not policy_result.get("success"):
        return []

    citations = []

    for item in policy_result.get("result", []):
        citation = item.get("citation") or item.get("source")

        if citation:
            citations.append(citation)

    return citations


def check_reply(result: dict, case: dict) -> tuple[bool, list[str]]:
    """检查最终回复是否包含必要信息，并且没有出现危险承诺或业务幻觉。"""

    errors = []
    reply = result.get("reply", "")

    for phrase in case.get("must_include", []):
        if phrase not in reply:
            errors.append(f"reply missing phrase: {phrase}")

    for phrase in case.get("must_not_include", []):
        if phrase in reply:
            errors.append(f"reply contains forbidden phrase: {phrase}")

    if case.get("require_citation", False):
        citations = get_policy_citations(result)
        matched_citations = [
            citation
            for citation in citations
            if citation in reply
        ]

        if not matched_citations:
            errors.append(f"reply missing citation, expected one of {citations}")

    return len(errors) == 0, errors


def run_single_case(case: dict) -> dict[str, Any]:
    """执行一条端到端 case，并输出每个检查维度的结果。"""

    result = run_customer_support_agent(
        user_message=case["user_message"],
        use_llm=False,
    )

    route_pass, route_errors = check_route(result, case)
    tools_pass, tool_errors = check_tools(result, case)
    action_pass, action_errors = check_final_action(result, case)
    reply_pass, reply_errors = check_reply(result, case)
    passed = route_pass and tools_pass and action_pass and reply_pass

    return {
        "id": case["id"],
        "intent": case["intent"],
        "user_message": case["user_message"],
        "passed": passed,
        "route_pass": route_pass,
        "tools_pass": tools_pass,
        "action_pass": action_pass,
        "reply_pass": reply_pass,
        "expected_order_id": case.get("expected_order_id"),
        "actual_order_id": result.get("route", {}).get("order_id"),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": get_tool_names(result),
        "expected_final_action": case.get("expected_final_action"),
        "actual_final_action": infer_final_action(result),
        "errors": route_errors + tool_errors + action_errors + reply_errors,
        "route": result.get("route", {}),
        "tool_results": get_tool_results(result),
        "reply": result.get("reply", ""),
    }


def build_report(results: list[dict]) -> dict:
    """汇总所有端到端 case 的通过率和失败详情。"""

    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    route_pass_count = sum(1 for item in results if item["route_pass"])
    tools_pass_count = sum(1 for item in results if item["tools_pass"])
    action_pass_count = sum(1 for item in results if item["action_pass"])
    reply_pass_count = sum(1 for item in results if item["reply_pass"])
    failed_cases = [
        item
        for item in results
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
        "action_pass_rate": round(action_pass_count / total, 4) if total else 0,
        "reply_pass_rate": round(reply_pass_count / total, 4) if total else 0,
        "failed_cases": failed_cases,
        "results": results,
    }


def save_report(report: dict) -> Path:
    """保存评估报告，方便后续对比优化前后的效果。"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"e2e_eval_report_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_report(report: dict, report_path: Path) -> None:
    """把端到端评估结果打印到终端。"""

    print("=" * 60)
    print("Customer Support Agent E2E Eval Report")
    print("=" * 60)
    print(f"总样本数: {report['total']}")
    print(f"通过数量: {report['passed_count']}")
    print(f"失败数量: {report['failed_count']}")
    print(f"总体通过率: {report['overall_pass_rate']}")
    print(f"路由通过率: {report['route_pass_rate']}")
    print(f"工具链通过率: {report['tools_pass_rate']}")
    print(f"最终动作通过率: {report['action_pass_rate']}")
    print(f"回复约束通过率: {report['reply_pass_rate']}")
    print(f"报告文件: {report_path}")

    if report["failed_cases"]:
        print("\n失败用例:")

        for item in report["failed_cases"]:
            print("-" * 60)
            print(f"id: {item['id']}")
            print(f"intent: {item['intent']}")
            print(f"user_message: {item['user_message']}")
            print(f"errors: {item['errors']}")
            print(f"expected_tools: {item['expected_tools']}")
            print(f"actual_tools: {item['actual_tools']}")
            print(f"expected_final_action: {item['expected_final_action']}")
            print(f"actual_final_action: {item['actual_final_action']}")
            print(f"reply: {item['reply']}")

    print("=" * 60)


def main() -> None:
    """端到端评估主入口。"""

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
