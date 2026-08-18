import sys
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.entry.agent_core import run_customer_support_agent


def tool_names(result: dict) -> list[str]:
    """提取本轮实际调用的工具名，便于断言多轮上下文是否生效。"""

    return [
        item["tool_name"]
        for item in result.get("tool_results", [])
    ]


def main() -> None:
    """验证普通多轮上下文：用户后续省略订单号时，系统能继承最近订单。"""

    conversation_id = f"context-smoke-{uuid4().hex}"

    first_result = run_customer_support_agent(
        user_message="我不想要这个耳机了",
        conversation_id=conversation_id,
        use_llm=False,
    )
    second_result = run_customer_support_agent(
        user_message="10001",
        conversation_id=conversation_id,
        use_llm=False,
    )
    third_result = run_customer_support_agent(
        user_message="退款",
        conversation_id=conversation_id,
        use_llm=False,
    )

    print("=" * 60)
    print("Conversation Context Smoke Test")
    print("=" * 60)
    print("第一轮 effective_user_message:", first_result["effective_user_message"])
    print("第一轮回复:", first_result["reply"])
    print("\n第二轮 effective_user_message:", second_result["effective_user_message"])
    print("第二轮工具:", tool_names(second_result))
    print("第二轮回复:", second_result["reply"])
    print("\n第三轮 effective_user_message:", third_result["effective_user_message"])
    print("第三轮是否使用普通上下文:", third_result["used_conversation_context"])
    print("第三轮上下文来源:", third_result["conversation_context"])
    print("第三轮工具:", tool_names(third_result))
    print("第三轮回复:", third_result["reply"])
    print("=" * 60)

    assert first_result["route"]["need_clarification"] is True
    assert second_result["route"]["order_id"] == "10001"
    assert second_result["route"]["need_policy"] is True
    assert second_result["route"]["need_ticket"] is False
    assert "order_lookup" in tool_names(second_result)
    assert third_result["used_conversation_context"] is True
    assert third_result["conversation_context"]["order_id"] == "10001"
    assert third_result["effective_user_message"] == "订单 10001 退款"
    assert third_result["route"]["order_id"] == "10001"
    assert tool_names(third_result) == ["order_lookup", "policy_search", "create_ticket"]


if __name__ == "__main__":
    main()
