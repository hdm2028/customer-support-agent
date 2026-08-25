import sys
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app.agent.entry.agent_core as agent_core
import app.tools.registry as tool_registry


def tool_names(result: dict) -> list[str]:
    """提取本轮实际调用的工具名。"""

    return [
        item["tool_name"]
        for item in result.get("tool_results", [])
    ]


def run_policy_failure_case() -> dict:
    """模拟 RAG 政策检索工具故障，验证链路会停止自动建单。"""

    original_policy_search = tool_registry.TOOL_HANDLERS["policy_search"]

    def broken_policy_search(*args, **kwargs):
        raise RuntimeError("mock policy search timeout")

    tool_registry.TOOL_HANDLERS["policy_search"] = broken_policy_search

    try:
        return agent_core.run_customer_support_agent(
            user_message="订单 10001 耳机坏了，还在保修期内吗？",
            conversation_id=f"tool-failure-policy-{uuid4().hex}",
            use_llm=False,
        )
    finally:
        tool_registry.TOOL_HANDLERS["policy_search"] = original_policy_search


def run_ticket_failure_case() -> dict:
    """模拟工单创建工具故障，验证回复不会谎称工单已创建。"""

    original_create_ticket = tool_registry.TOOL_HANDLERS["create_ticket"]

    def broken_create_ticket(*args, **kwargs):
        raise RuntimeError("mock ticket database unavailable")

    tool_registry.TOOL_HANDLERS["create_ticket"] = broken_create_ticket

    try:
        return agent_core.run_customer_support_agent(
            user_message="订单 10001 耳机坏了，还在保修期内吗？",
            conversation_id=f"tool-failure-ticket-{uuid4().hex}",
            use_llm=False,
        )
    finally:
        tool_registry.TOOL_HANDLERS["create_ticket"] = original_create_ticket


def main() -> None:
    """验证工具链路断掉后的降级与短路策略。"""

    policy_failure_result = run_policy_failure_case()
    ticket_failure_result = run_ticket_failure_case()

    print("=" * 60)
    print("Tool Failure Smoke Test")
    print("=" * 60)
    print("政策检索失败工具链:", tool_names(policy_failure_result))
    print("政策检索失败回复:", policy_failure_result["reply"])
    print("\n工单创建失败工具链:", tool_names(ticket_failure_result))
    print("工单创建失败回复:", ticket_failure_result["reply"])
    print("=" * 60)

    assert tool_names(policy_failure_result) == ["order_lookup", "policy_search"]
    assert policy_failure_result["tool_results"][-1]["success"] is False
    assert "error_type" in policy_failure_result["tool_results"][-1]["result"]
    assert "create_ticket" not in tool_names(policy_failure_result)
    assert "政策检索工具调用失败" in policy_failure_result["reply"]

    assert tool_names(ticket_failure_result) == ["order_lookup", "policy_search", "create_ticket"]
    assert ticket_failure_result["tool_results"][-1]["success"] is False
    assert "error_type" in ticket_failure_result["tool_results"][-1]["result"]
    assert "工单创建工具本轮调用失败" in ticket_failure_result["reply"]
    assert "我已生成" not in ticket_failure_result["reply"]


if __name__ == "__main__":
    main()
