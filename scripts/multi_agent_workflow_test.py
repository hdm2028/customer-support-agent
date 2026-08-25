import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.orchestrator import DEFAULT_ORCHESTRATOR
from app.observability.tracing import start_trace
from app.storage.database import init_database


def main() -> None:
    init_database()
    message = "订单 10001 耳机坏了我要退款"
    trace = start_trace(message, conversation_id="workflow-test")
    route = DEFAULT_ORCHESTRATOR.route(message)
    state = DEFAULT_ORCHESTRATOR.run_agent_loop(
        user_message=message,
        route=route,
        conversation_id="workflow-test",
        trace=trace,
    )

    agent_steps = [
        step["agent_key"]
        for step in state.agent_steps
        if step["tool_names"]
    ]
    tool_names = [item.tool_name for item in state.tool_results]

    expected_prefix = [
        "after_sales_agent",
        "customer_agent",
        "risk_agent",
        "after_sales_agent",
    ]
    if agent_steps[:4] != expected_prefix:
        raise AssertionError(f"unexpected agent workflow: {agent_steps}")

    for tool_name in ["order_lookup", "policy_search", "risk_check", "refund_apply"]:
        if tool_name not in tool_names:
            raise AssertionError(f"missing tool {tool_name}, got {tool_names}")

    if "agent_dispatch" not in {event["event_type"] for event in trace["events"]}:
        raise AssertionError("orchestrator did not record agent dispatch events")

    print("multi_agent_workflow_test: passed")
    print({"agents": agent_steps, "tools": tool_names})


if __name__ == "__main__":
    main()
