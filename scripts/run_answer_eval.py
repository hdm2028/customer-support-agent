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
    """读取客服 Agent eval 数据集，复用其中的用户问题作为回答质量测试。"""

    cases = []

    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        cases.append(json.loads(line))

    return cases


def get_policy_citations(tool_results: list[dict]) -> list[str]:
    """从 policy_search 工具结果里提取本轮可引用的知识来源。"""

    citations = []

    for tool_result in tool_results:
        if tool_result.get("tool_name") != "policy_search":
            continue

        if not tool_result.get("success"):
            continue

        for item in tool_result.get("result", []):
            citation = item.get("citation") or item.get("source")

            if citation:
                citations.append(citation)

    return citations


def check_citation(reply: str, citations: list[str]) -> tuple[bool, list[str]]:
    """检查最终回复是否引用了本轮 RAG 检索出来的至少一个来源。"""

    if not citations:
        return True, []

    matched_citations = [
        citation for citation in citations
        if citation in reply
    ]

    return len(matched_citations) > 0, matched_citations


def check_risk_control(reply: str, route: dict) -> tuple[bool, list[str]]:
    """检查高风险请求是否有人工审核或不能直接执行的提示。"""

    if not route.get("handoff_required") and not route.get("blocked_by_guardrail"):
        return True, []

    required_phrases = [
        "人工",
        "审核",
        "不能",
        "无法",
        "拒绝",
    ]
    matched_phrases = [
        phrase for phrase in required_phrases
        if phrase in reply
    ]

    return len(matched_phrases) > 0, matched_phrases


def run_single_case(case: dict) -> dict:
    """执行单条回答质量评估：检查引用来源和高风险控制。"""

    result = run_customer_support_agent(
        user_message=case["user_message"],
        use_llm=False,
    )
    route = result.get("route", {})
    tool_results = result.get("tool_results", [])
    reply = result.get("reply", "")
    citations = get_policy_citations(tool_results)

    citation_pass, matched_citations = check_citation(reply, citations)
    risk_pass, matched_risk_phrases = check_risk_control(reply, route)
    passed = citation_pass and risk_pass
    errors = []

    if not citation_pass:
        errors.append(
            f"missing_citation expected_one_of={citations}"
        )

    if not risk_pass:
        errors.append(
            "missing_risk_control_phrase expected one of ['人工', '审核', '不能', '无法', '拒绝']"
        )

    return {
        "id": case["id"],
        "intent": case["intent"],
        "user_message": case["user_message"],
        "passed": passed,
        "citation_pass": citation_pass,
        "risk_control_pass": risk_pass,
        "expected_citations": citations,
        "matched_citations": matched_citations,
        "matched_risk_phrases": matched_risk_phrases,
        "reply": reply,
        "errors": errors,
        "notes": case.get("notes", ""),
    }


def build_report(results: list[dict]) -> dict:
    """汇总回答质量评估结果。"""

    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    citation_pass_count = sum(1 for item in results if item["citation_pass"])
    risk_pass_count = sum(1 for item in results if item["risk_control_pass"])
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
        "citation_pass_rate": round(citation_pass_count / total, 4) if total else 0,
        "risk_control_pass_rate": round(risk_pass_count / total, 4) if total else 0,
        "failed_cases": failed_cases,
        "results": results,
    }


def save_report(report: dict) -> Path:
    """保存回答质量评估报告，方便对比优化前后效果。"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"answer_eval_report_{timestamp}.json"

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_report(report: dict, report_path: Path) -> None:
    """把回答质量评估结果打印到终端。"""

    print("=" * 60)
    print("Customer Support Answer Eval Report")
    print("=" * 60)
    print(f"总样本数：{report['total']}")
    print(f"通过数量：{report['passed_count']}")
    print(f"失败数量：{report['failed_count']}")
    print(f"总体通过率：{report['overall_pass_rate']}")
    print(f"Citation 通过率：{report['citation_pass_rate']}")
    print(f"风险控制通过率：{report['risk_control_pass_rate']}")
    print(f"报告文件：{report_path}")

    if report["failed_cases"]:
        print("\n失败用例：")

        for item in report["failed_cases"]:
            print("-" * 60)
            print(f"id: {item['id']}")
            print(f"intent: {item['intent']}")
            print(f"user_message: {item['user_message']}")
            print(f"errors: {item['errors']}")
            print(f"expected_citations: {item['expected_citations']}")
            print(f"reply: {item['reply']}")

    print("=" * 60)


def main() -> None:
    """回答质量评估主入口。"""

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
