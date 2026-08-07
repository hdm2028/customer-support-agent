import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from main import app


def main() -> None:
    client = TestClient(app)

    health_response = client.get("/health")
    print("health:", health_response.status_code, health_response.json())

    info_response = client.get("/info")
    print("info:", info_response.status_code, info_response.json()["default_agent"])

    chat_response = client.post(
        "/agent/chat",
        json={
            "message": "我的订单 10001 耳机坏了，还在保修期内吗？我想申请维修检测。",
            "use_llm": False,
        },
    )

    data = chat_response.json()
    print("chat:", chat_response.status_code)
    print("route:", data["route"])
    print("reply:", data["reply"])

    stream_response = client.post(
        "/agent/stream",
        json={
            "message": "订单 10002 物流一直不更新怎么办？",
            "conversation_id": data["conversation_id"],
            "use_llm": False,
            "stream_tokens": False,
        },
    )
    print("stream:", stream_response.status_code)
    print(stream_response.text.splitlines()[0])

    history_response = client.post(
        "/agent/history",
        json={"conversation_id": data["conversation_id"]},
    )
    print("history:", history_response.status_code, len(history_response.json()["messages"]))

    feedback_response = client.post(
        "/feedback",
        json={
            "conversation_id": data["conversation_id"],
            "score": 5,
            "comment": "api-smoke-test",
        },
    )
    print("feedback:", feedback_response.status_code, feedback_response.json())


if __name__ == "__main__":
    main()
