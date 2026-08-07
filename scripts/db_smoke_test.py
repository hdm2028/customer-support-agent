import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.database import get_connection, init_database


TABLES = [
    "orders",
    "tickets",
    "conversation_messages",
    "pending_tasks",
    "feedback",
]


def main() -> None:
    """检查 SQLite 是否已经保存订单、工单、会话、pending task 和 feedback。"""

    init_database()

    with get_connection() as connection:
        print("=" * 60)
        print("Customer Support Database Smoke Test")
        print("=" * 60)

        for table in TABLES:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()
            print(f"{table}: {row['count']}")

        latest_ticket = connection.execute(
            """
            SELECT payload FROM tickets
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

        if latest_ticket:
            ticket = json.loads(latest_ticket["payload"])
            print("\nlatest_ticket:")
            print(json.dumps(ticket, ensure_ascii=False, indent=2))

        print("=" * 60)


if __name__ == "__main__":
    main()
