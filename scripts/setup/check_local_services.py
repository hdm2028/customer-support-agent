from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.rag.embedding_client import get_embedding_provider
from app.storage.cache import cache_health
from app.storage.database import (
    database_health,
    get_connection,
    get_database_backend_name,
    get_order_from_db,
    init_database,
)
from app.storage.mysql_database import get_mysql_connection, mysql_database_name


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

SIDE_EFFECTS = ["[MAY INITIALIZE DATABASE SCHEMA]", "[READS MYSQL/SQLITE]", "[READS REDIS]", "[CALLS EMBEDDING IF CONFIGURED]"]


def print_json(title: str, payload: dict) -> None:
    print(title)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def table_counts() -> dict:
    backend = get_database_backend_name()
    connection_factory = get_mysql_connection if backend == "mysql" else get_connection
    counts = {}

    try:
        with connection_factory() as connection:
            for table in TABLES:
                if backend == "mysql":
                    with connection.cursor() as cursor:
                        cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
                        row = cursor.fetchone()
                else:
                    row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                counts[table] = row["count"]
    except Exception as error:
        return {
            "error_type": type(error).__name__,
            "error_message": str(error),
        }

    return counts


def check_database(order_id: str | None) -> dict:
    settings = get_settings()
    try:
        init_database()
        payload = {
            "backend": get_database_backend_name(),
            "health": database_health(),
            "mysql_configured": bool(settings.mysql_dsn),
            "mysql_database": mysql_database_name() if settings.mysql_dsn else None,
            "table_counts": table_counts(),
        }
        if order_id:
            payload["order"] = get_order_from_db(order_id)
        return payload
    except Exception as error:
        return {
            "backend": get_database_backend_name(),
            "reachable": False,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }


def check_cache() -> dict:
    try:
        return cache_health()
    except Exception as error:
        return {
            "reachable": False,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }


def check_embedding() -> dict:
    settings = get_settings()
    try:
        provider = get_embedding_provider()
        vector = provider.embed_text("退款申请 幂等 MQ 人工审核")
        return {
            "provider": settings.rag_embedding_provider,
            "model": settings.zhipu_embedding_model if settings.rag_embedding_provider == "zhipu" else "local_hash",
            "reachable": True,
            "vector_length": len(vector),
        }
    except Exception as error:
        return {
            "provider": settings.rag_embedding_provider,
            "model": settings.zhipu_embedding_model,
            "reachable": False,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }


def main() -> None:
    order_id = sys.argv[1] if len(sys.argv) > 1 else None
    report = {
        "side_effects": SIDE_EFFECTS,
        "database": check_database(order_id),
        "cache": check_cache(),
        "embedding": check_embedding(),
    }
    print_json("local_services", report)


if __name__ == "__main__":
    main()
