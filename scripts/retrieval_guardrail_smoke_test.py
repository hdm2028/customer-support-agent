import sys
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app.agent.entry.agent_core as agent_core
import app.tools.registry as tool_registry
from app.core.schemas import ToolResult


def tool_names(result: dict) -> list[str]:
    """提取本轮实际调用的工具名。"""

    return [
        item["tool_name"]
        for item in result.get("tool_results", [])
    ]


def run_source_mismatch_case() -> dict:
    """模拟 RAG 召回了错误业务文档，验证不会继续自动建单。"""

    original_policy_search = tool_registry.TOOL_HANDLERS["policy_search"]

    def mismatched_policy_search(*args, **kwargs):
        return ToolResult(
            tool_name="policy_search",
            success=True,
            result=[
                {
                    "source": "物流配送政策.md",
                    "section": "物流查询",
                    "citation": "物流配送政策.md - 物流查询",
                    "score": 0.91,
                    "text": "物流超过 48 小时没有更新时，可以创建物流异常工单。",
                }
            ],
        )

    tool_registry.TOOL_HANDLERS["policy_search"] = mismatched_policy_search

    try:
        return agent_core.run_customer_support_agent(
            user_message="订单 10001 耳机坏了，还在保修期内吗？",
            conversation_id=f"rag-mismatch-{uuid4().hex}",
            use_llm=False,
        )
    finally:
        tool_registry.TOOL_HANDLERS["policy_search"] = original_policy_search


def run_low_confidence_case() -> dict:
    """模拟 RAG 只召回了低分证据，验证系统会进入证据不足兜底。"""

    original_policy_search = tool_registry.TOOL_HANDLERS["policy_search"]

    def low_confidence_policy_search(*args, **kwargs):
        return ToolResult(
            tool_name="policy_search",
            success=True,
            result=[
                {
                    "source": "保修政策.md",
                    "section": "保修范围",
                    "citation": "保修政策.md - 保修范围",
                    "score": 0.12,
                    "text": "保修政策片段。",
                }
            ],
        )

    tool_registry.TOOL_HANDLERS["policy_search"] = low_confidence_policy_search

    try:
        return agent_core.run_customer_support_agent(
            user_message="订单 10001 耳机坏了，还在保修期内吗？",
            conversation_id=f"rag-low-confidence-{uuid4().hex}",
            use_llm=False,
        )
    finally:
        tool_registry.TOOL_HANDLERS["policy_search"] = original_policy_search


def main() -> None:
    """验证 RAG 召回不全或召回错误时的兜底策略。"""

    source_mismatch_result = run_source_mismatch_case()
    low_confidence_result = run_low_confidence_case()

    print("=" * 60)
    print("Retrieval Guardrail Smoke Test")
    print("=" * 60)
    print("来源不匹配工具链:", tool_names(source_mismatch_result))
    print("来源不匹配回复:", source_mismatch_result["reply"])
    print("\n低置信证据工具链:", tool_names(low_confidence_result))
    print("低置信证据回复:", low_confidence_result["reply"])
    print("=" * 60)

    assert tool_names(source_mismatch_result) == ["order_lookup", "policy_search"]
    assert source_mismatch_result["tool_results"][-1]["success"] is False
    assert source_mismatch_result["tool_results"][-1]["result"]["error_type"] == "LowConfidenceEvidence"
    assert "create_ticket" not in tool_names(source_mismatch_result)
    assert "没有检索到足够匹配" in source_mismatch_result["reply"]

    assert tool_names(low_confidence_result) == ["order_lookup", "policy_search"]
    assert low_confidence_result["tool_results"][-1]["success"] is False
    assert low_confidence_result["tool_results"][-1]["result"]["error_type"] == "LowConfidenceEvidence"
    assert "create_ticket" not in tool_names(low_confidence_result)
    assert "没有检索到足够匹配" in low_confidence_result["reply"]


if __name__ == "__main__":
    main()
