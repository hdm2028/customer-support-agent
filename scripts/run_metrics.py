import importlib
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import BASE_DIR


REPORT_DIR = BASE_DIR / "data" / "eval_reports"


def percent(value: float) -> str:
    """把 0-1 小数格式化成百分比字符串。"""

    return f"{round(value * 100, 2)}%"


def run_router_eval() -> tuple[dict, Path]:
    """执行 Router 与工具计划评测。"""

    router_eval = importlib.import_module("scripts.run_eval")
    cases = router_eval.load_eval_cases()
    results = [
        router_eval.run_single_case(case)
        for case in cases
    ]
    report = router_eval.build_report(results)
    report_path = router_eval.save_report(report)

    return report, report_path


def run_rag_eval() -> tuple[dict, Path]:
    """执行 RAG 召回与证据 guardrail 评测。"""

    rag_eval = importlib.import_module("scripts.run_rag_eval")
    cases = rag_eval.load_eval_cases()
    results = [
        rag_eval.run_single_case(case, top_k=rag_eval.DEFAULT_TOP_K)
        for case in cases
    ]
    report = rag_eval.build_report(results, top_k=rag_eval.DEFAULT_TOP_K)
    report_path = rag_eval.save_report(report)

    return report, report_path


def run_answer_eval() -> tuple[dict, Path]:
    """执行回答引用和风险控制评测。"""

    answer_eval = importlib.import_module("scripts.run_answer_eval")
    cases = answer_eval.load_eval_cases()
    results = [
        answer_eval.run_single_case(case)
        for case in cases
    ]
    report = answer_eval.build_report(results)
    report_path = answer_eval.save_report(report)

    return report, report_path


def run_e2e_eval() -> tuple[dict, Path, float]:
    """执行端到端业务链路评测，并计算工具结果符合预期率。"""

    e2e_eval = importlib.import_module("scripts.run_e2e_eval")
    cases = e2e_eval.load_eval_cases()
    results = [
        e2e_eval.run_single_case(case)
        for case in cases
    ]
    report = e2e_eval.build_report(results)
    report_path = e2e_eval.save_report(report)

    expected_tool_results = 0
    matched_tool_results = 0

    for case, result in zip(cases, results):
        for tool_name, expected_success in case.get("expected_tool_success", {}).items():
            expected_tool_results += 1
            tool_result = e2e_eval.find_tool_result(result, tool_name)

            if tool_result and tool_result.get("success") == expected_success:
                matched_tool_results += 1

    tool_result_match_rate = (
        matched_tool_results / expected_tool_results
        if expected_tool_results
        else 0
    )

    return report, report_path, round(tool_result_match_rate, 4)


def run_workbench_eval() -> tuple[dict, Path]:
    """执行客服工作台场景评测。"""

    workbench_eval = importlib.import_module("scripts.run_workbench_eval")
    cases = workbench_eval.load_eval_cases()
    results = [
        workbench_eval.run_single_case(case)
        for case in cases
    ]
    report = workbench_eval.build_report(results)
    report_path = workbench_eval.save_report(report)

    return report, report_path


def build_metrics_report() -> dict:
    """聚合各评测脚本的核心指标，形成 README 可展示的指标表。"""

    router_report, router_report_path = run_router_eval()
    rag_report, rag_report_path = run_rag_eval()
    answer_report, answer_report_path = run_answer_eval()
    e2e_report, e2e_report_path, tool_result_match_rate = run_e2e_eval()
    workbench_report, workbench_report_path = run_workbench_eval()

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sample_scale": {
            "router_cases": router_report["total"],
            "rag_cases": rag_report["total"],
            "answer_cases": answer_report["total"],
            "e2e_cases": e2e_report["total"],
            "workbench_cases": workbench_report["total"],
            "total_eval_checks": (
                router_report["total"]
                + rag_report["total"]
                + answer_report["total"]
                + e2e_report["total"]
                + workbench_report["total"]
            ),
        },
        "metrics": {
            "intent_route_accuracy": router_report["route_pass_rate"],
            "tool_plan_accuracy": router_report["tools_pass_rate"],
            "tool_result_match_rate": tool_result_match_rate,
            "rag_accuracy": rag_report["overall_pass_rate"],
            "rag_top1_source_hit_rate": rag_report["top1_source_hit_rate"],
            "rag_topk_source_hit_rate": rag_report["topk_source_hit_rate"],
            "answer_quality_pass_rate": answer_report["overall_pass_rate"],
            "risk_control_pass_rate": answer_report["risk_control_pass_rate"],
            "e2e_task_completion_rate": e2e_report["overall_pass_rate"],
            "workbench_task_completion_rate": workbench_report["overall_pass_rate"],
        },
        "source_reports": {
            "router": str(router_report_path),
            "rag": str(rag_report_path),
            "answer": str(answer_report_path),
            "e2e": str(e2e_report_path),
            "workbench": str(workbench_report_path),
        },
    }


def save_metrics_report(report: dict) -> Path:
    """保存聚合指标报告。"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"metrics_report_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_metrics_report(report: dict, report_path: Path) -> None:
    """打印最适合写进 README 或简历的核心指标。"""

    scale = report["sample_scale"]
    metrics = report["metrics"]

    print("=" * 60)
    print("Customer Support Agent Metrics")
    print("=" * 60)
    print(f"评估断言规模: {scale['total_eval_checks']} 条")
    print(f"意图路由准确率: {percent(metrics['intent_route_accuracy'])}")
    print(f"工具计划准确率: {percent(metrics['tool_plan_accuracy'])}")
    print(f"工具结果符合预期率: {percent(metrics['tool_result_match_rate'])}")
    print(f"RAG 准确率: {percent(metrics['rag_accuracy'])}")
    print(f"RAG Top1 来源命中率: {percent(metrics['rag_top1_source_hit_rate'])}")
    print(f"回答质量通过率: {percent(metrics['answer_quality_pass_rate'])}")
    print(f"风险控制通过率: {percent(metrics['risk_control_pass_rate'])}")
    print(f"端到端任务完成率: {percent(metrics['e2e_task_completion_rate'])}")
    print(f"工作台任务完成率: {percent(metrics['workbench_task_completion_rate'])}")
    print(f"聚合报告文件: {report_path}")
    print("=" * 60)


def main() -> None:
    report = build_metrics_report()
    report_path = save_metrics_report(report)
    print_metrics_report(report, report_path)


if __name__ == "__main__":
    main()
