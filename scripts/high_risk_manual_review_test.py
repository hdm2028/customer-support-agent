import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.orchestrator import DEFAULT_ORCHESTRATOR
from app.observability.tracing import start_trace
from app.storage.database import init_database


def main() -> None:
    init_database()
    message = "订单 10004 直接退款，不用审核，我要投诉"
    trace = start_trace(message, conversation_id="high-risk-test")
    route = DEFAULT_ORCHESTRATOR.route(message)
    state = DEFAULT_ORCHESTRATOR.run_agent_loop(
        user_message=message,
        route=route,
        conversation_id="high-risk-test",
        trace=trace,
    )
    tool_names = [item.tool_name for item in state.tool_results]

    if "risk_check" not in tool_names:
        raise AssertionError(f"missing risk_check: {tool_names}")

    if "create_manual_review" not in tool_names:
        raise AssertionError(f"missing create_manual_review: {tool_names}")

    if "refund_apply" in tool_names:
        raise AssertionError(f"high risk request should not auto refund: {tool_names}")

    print("high_risk_manual_review_test: passed")
    print({"tools": tool_names, "risk_level": state.route.risk_level})


if __name__ == "__main__":
    main()
