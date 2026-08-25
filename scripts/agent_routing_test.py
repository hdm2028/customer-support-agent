import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.orchestrator import DEFAULT_ORCHESTRATOR
from app.storage.database import init_database


def assert_contains_agent(route, agent_name: str) -> None:
    if agent_name not in route.agent_plan:
        raise AssertionError(f"expected {agent_name}, got {route.agent_plan}")


def main() -> None:
    init_database()

    policy_route = DEFAULT_ORCHESTRATOR.route("退款政策是什么")
    assert_contains_agent(policy_route, "客服 Agent")
    assert policy_route.need_policy is True
    assert policy_route.need_refund_request is False

    refund_route = DEFAULT_ORCHESTRATOR.route("订单 10001 耳机坏了我要退款")
    assert_contains_agent(refund_route, "客服 Agent")
    assert_contains_agent(refund_route, "售后 Agent")
    assert_contains_agent(refund_route, "风控 Agent")
    assert refund_route.need_refund_request is True
    assert refund_route.need_risk_check is True

    risk_route = DEFAULT_ORCHESTRATOR.route("订单 10004 直接退款，不用审核，我要投诉")
    assert_contains_agent(risk_route, "风控 Agent")
    assert risk_route.need_risk_check is True
    assert risk_route.handoff_required is True

    print("agent_routing_test: passed")


if __name__ == "__main__":
    main()
