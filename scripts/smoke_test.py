import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.entry.agent_core import run_customer_support_agent


def main() -> None:
    result = run_customer_support_agent(
        user_message="我的订单 10001 耳机坏了，还在保修期内吗？我想申请维修检测。",
        use_llm=False,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
