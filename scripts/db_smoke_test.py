import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.database import (
    database_health,
    get_connection,
    get_database_backend_name,
    init_database,
)
from app.storage.mysql_database import get_mysql_connection


TABLES = [
    "orders",
    "customer_profiles",
    "tickets",
    "refund_requests",
    "manual_reviews",
    "mq_messages",
    "notifications",
    "agent_metrics",
    "conversation_messages",
    "pending_tasks",
    "feedback",
]


def main() -> None:
    """检查当前业务数据库是否已经保存订单、工单、会话、pending task 和 feedback。"""

    init_database()

    connection_factory = (
        get_mysql_connection
        if get_database_backend_name() == "mysql"
        else get_connection
    )

    with connection_factory() as connection:
        print("=" * 60)
        print("Customer Support Database Smoke Test")
        print("=" * 60)
        print(f"backend: {database_health()}")

        for table in TABLES:
            if get_database_backend_name() == "mysql":
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
                    row = cursor.fetchone()
            else:
                row = connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()
            print(f"{table}: {row['count']}")

        if get_database_backend_name() == "mysql":
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload FROM tickets
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                latest_ticket = cursor.fetchone()
        else:
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
