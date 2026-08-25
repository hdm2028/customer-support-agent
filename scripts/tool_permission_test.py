import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.database import init_database
from app.tools.executor import execute_agent_tool


def main() -> None:
    init_database()
    result = execute_agent_tool(
        agent_key="customer_agent",
        tool_name="refund_apply",
        arguments={
            "order_id": "10001",
            "user_request": "订单 10001 我要退款",
        },
    )

    if result.success:
        raise AssertionError("customer_agent should not be allowed to call refund_apply")

    if not isinstance(result.result, dict) or result.result.get("error_type") != "ToolPermissionDenied":
        raise AssertionError(f"unexpected permission result: {result.result}")

    print("tool_permission_test: passed")


if __name__ == "__main__":
    main()
