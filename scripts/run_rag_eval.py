import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import BASE_DIR
from app.rag.rag import search_documents


EVAL_PATH = BASE_DIR / "data" / "eval" / "rag_eval.jsonl"
REPORT_DIR = BASE_DIR / "data" / "eval_reports"
DEFAULT_TOP_K = 3


def load_eval_cases() -> list[dict]:
    """读取 RAG eval 数据集：每一行 JSON 代表一个检索测试用例。"""

    cases = []

    for line in EVAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        cases.append(json.loads(line))

    return cases


def normalize_text(text: str) -> str:
    """做一个轻量文本归一化，降低空格和大小写对关键词匹配的影响。"""

    return text.lower().replace(" ", "")


def source_hit(results: list[dict], expected_sources: list[str], top_n: int) -> bool:
    """检查前 top_n 条检索结果里，是否命中了期望来源文档。"""

    top_results = results[:top_n]
    actual_sources = {item.get("source") for item in top_results}

    return any(source in actual_sources for source in expected_sources)


def keyword_hit(results: list[dict], expected_keywords: list[str]) -> tuple[bool, list[str]]:
    """检查 top_k 召回文本里是否包含业务关键规则。"""

    combined_text = "\n".join(
        f"{item.get('section', '')}\n{item.get('text', '')}"
        for item in results
    )
    normalized_text = normalize_text(combined_text)
    missing_keywords = []

    for keyword in expected_keywords:
        if normalize_text(keyword) not in normalized_text:
            missing_keywords.append(keyword)

    return len(missing_keywords) == 0, missing_keywords


def simplify_result(result: dict) -> dict:
    """只保留报告里最需要看的检索字段，避免报告太长。"""

    return {
        "source": result.get("source"),
        "section": result.get("section"),
        "citation": result.get("citation"),
        "score": result.get("score"),
        "vector_score": result.get("vector_score"),
        "keyword_score": result.get("keyword_score"),
        "retrieval_score": result.get("retrieval_score"),
        "rerank_bonus": result.get("rerank_bonus"),
        "rerank_score": result.get("rerank_score"),
        "rerank_reasons": result.get("rerank_reasons", []),
        "text_preview": result.get("text", "")[:160],
    }


def run_single_case(case: dict, top_k: int = DEFAULT_TOP_K) -> dict:
    """执行单条 RAG eval：检索、判断来源命中、判断关键词命中。"""

    results = search_documents(case["query"], top_k=top_k)
    expected_sources = case.get("expected_sources", [])
    expected_keywords = case.get("expected_keywords", [])

    top1_source_hit = source_hit(results, expected_sources, top_n=1)
    topk_source_hit = source_hit(results, expected_sources, top_n=top_k)
    keywords_pass, missing_keywords = keyword_hit(results, expected_keywords)

    passed = topk_source_hit and keywords_pass
    errors = []

    if not topk_source_hit:
        actual_sources = [item.get("source") for item in results]
        errors.append(
            f"source_miss expected={expected_sources}, actual_top{top_k}={actual_sources}"
        )

    if missing_keywords:
        errors.append(f"missing_keywords={missing_keywords}")

    return {
        "id": case["id"],
        "query": case["query"],
        "passed": passed,
        "top1_source_hit": top1_source_hit,
        "topk_source_hit": topk_source_hit,
        "keywords_pass": keywords_pass,
        "expected_sources": expected_sources,
        "expected_keywords": expected_keywords,
        "missing_keywords": missing_keywords,
        "results": [simplify_result(item) for item in results],
        "errors": errors,
        "notes": case.get("notes", ""),
    }


def build_report(results: list[dict], top_k: int) -> dict:
    """汇总 RAG eval 结果，生成整体指标。"""

    total = len(results)
    passed_count = sum(1 for item in results if item["passed"])
    top1_hit_count = sum(1 for item in results if item["top1_source_hit"])
    topk_hit_count = sum(1 for item in results if item["topk_source_hit"])
    keyword_pass_count = sum(1 for item in results if item["keywords_pass"])

    failed_cases = [
        item for item in results
        if not item["passed"]
    ]

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "top_k": top_k,
        "total": total,
        "passed_count": passed_count,
        "failed_count": total - passed_count,
        "overall_pass_rate": round(passed_count / total, 4) if total else 0,
        "top1_source_hit_rate": round(top1_hit_count / total, 4) if total else 0,
        "topk_source_hit_rate": round(topk_hit_count / total, 4) if total else 0,
        "keyword_pass_rate": round(keyword_pass_count / total, 4) if total else 0,
        "failed_cases": failed_cases,
        "results": results,
    }


def save_report(report: dict) -> Path:
    """把 RAG eval 报告保存到 data/eval_reports，方便后续对比优化前后效果。"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"rag_eval_report_{timestamp}.json"

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report_path


def print_report(report: dict, report_path: Path) -> None:
    """把核心 RAG 指标打印到终端，失败用例只打印关键定位信息。"""

    print("=" * 60)
    print("Customer Support RAG Eval Report")
    print("=" * 60)
    print(f"总样本数：{report['total']}")
    print(f"通过数量：{report['passed_count']}")
    print(f"失败数量：{report['failed_count']}")
    print(f"总体通过率：{report['overall_pass_rate']}")
    print(f"Top1 来源命中率：{report['top1_source_hit_rate']}")
    print(f"Top{report['top_k']} 来源命中率：{report['topk_source_hit_rate']}")
    print(f"关键词命中率：{report['keyword_pass_rate']}")
    print(f"报告文件：{report_path}")

    if report["failed_cases"]:
        print("\n失败用例：")

        for item in report["failed_cases"]:
            print("-" * 60)
            print(f"id: {item['id']}")
            print(f"query: {item['query']}")
            print(f"errors: {item['errors']}")
            print("actual_results:")

            for result in item["results"]:
                print(
                    f"  - {result['source']} / {result['section']} "
                    f"score={result['score']} keyword={result['keyword_score']}"
                )

    print("=" * 60)


def main() -> None:
    """RAG eval 主入口：加载用例、执行检索、生成报告。"""

    cases = load_eval_cases()
    results = [
        run_single_case(case, top_k=DEFAULT_TOP_K)
        for case in cases
    ]
    report = build_report(results, top_k=DEFAULT_TOP_K)
    report_path = save_report(report)

    print_report(report, report_path)


if __name__ == "__main__":
    main()
