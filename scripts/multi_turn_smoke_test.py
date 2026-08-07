import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.agent_core import run_customer_support_agent


def main() -> None:
    """模拟多轮槽位补全：第一轮表达任务，第二轮只补订单号。"""

    first_result = run_customer_support_agent(
        user_message="帮我改收货地址。",
        conversation_id="multi-turn-demo",
        use_llm=False,
    )

    second_result = run_customer_support_agent(
        user_message="10009",
        conversation_id=first_result["conversation_id"],
        use_llm=False,
    )

    third_result = run_customer_support_agent(
        user_message="新地址是北京市朝阳区望京街道88号",
        conversation_id=first_result["conversation_id"],
        use_llm=False,
    )

    print("=" * 60)
    print("Multi-turn Slot Filling Smoke Test")
    print("=" * 60)
    print("第一轮回复：")
    print(first_result["reply"])
    print("\n第二轮是否使用 pending task：", second_result["used_pending_task"])
    print("第二轮合并后的用户请求：", second_result["effective_user_message"])
    print("第二轮 route：", second_result["route"])
    print("第二轮回复：")
    print(second_result["reply"])
    print("\n第三轮是否使用 pending task：", third_result["used_pending_task"])
    print("第三轮合并后的用户请求：", third_result["effective_user_message"])
    print("第三轮 route：", third_result["route"])
    print("第三轮工具：", [item["tool_name"] for item in third_result["tool_results"]])
    print("第三轮回复：")
    print(third_result["reply"])
    print("=" * 60)

    assert first_result["route"]["need_clarification"] is True
    assert second_result["used_pending_task"] is True
    assert second_result["route"]["order_id"] == "10009"
    assert second_result["route"]["need_clarification"] is True
    assert second_result["tool_results"] == []
    assert third_result["used_pending_task"] is True
    assert third_result["route"]["order_id"] == "10009"
    assert "order_lookup" in [item["tool_name"] for item in third_result["tool_results"]]
    assert "policy_search" in [item["tool_name"] for item in third_result["tool_results"]]
    assert "create_ticket" in [item["tool_name"] for item in third_result["tool_results"]]

    policy_result = next(
        item for item in third_result["tool_results"]
        if item["tool_name"] == "policy_search"
    )
    first_citation = policy_result["result"][0]["citation"]
    assert "修改收货地址" in first_citation


if __name__ == "__main__":
    main()
