import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.storage.mysql_database import get_mysql_connection, mysql_database_name


def print_json(title: str, payload: dict) -> None:
    print(title)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def check_mysql(order_id: str | None = None) -> None:
    settings = get_settings()

    if settings.database_backend != "mysql" and not settings.mysql_dsn:
        print_json(
            "mysql",
            {
                "configured": False,
                "message": "Set DATABASE_BACKEND=mysql and MySQL connection settings.",
            },
        )
        return

    try:
        with get_mysql_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'orders'
                    """
                )
                has_orders_table = bool(cursor.fetchone()["count"])

                result = {
                    "configured": True,
                    "reachable": True,
                    "database": mysql_database_name(),
                    "orders_table": has_orders_table,
                }

                if has_orders_table:
                    cursor.execute("SELECT COUNT(*) AS count FROM orders")
                    result["orders_count"] = cursor.fetchone()["count"]

                    if order_id:
                        cursor.execute(
                            """
                            SELECT order_id, user_id, product_name, amount,
                                   order_status, after_sales_status, updated_at
                            FROM orders
                            WHERE order_id = %s
                            """,
                            (order_id,),
                        )
                        result["order"] = cursor.fetchone()
                    else:
                        cursor.execute(
                            """
                            SELECT order_id, user_id, product_name, amount,
                                   order_status, after_sales_status, updated_at
                            FROM orders
                            ORDER BY order_id
                            LIMIT 5
                            """
                        )
                        result["sample_orders"] = cursor.fetchall()

        print_json("mysql", result)
    except Exception as error:
        print_json(
            "mysql",
            {
                "configured": bool(settings.mysql_dsn),
                "reachable": False,
                "database": mysql_database_name(),
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )


def check_redis() -> None:
    settings = get_settings()

    if not settings.redis_url:
        print_json(
            "redis",
            {
                "configured": False,
                "message": "Set REDIS_URL or REDIS_HOST.",
            },
        )
        return

    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        print_json(
            "redis",
            {
                "configured": True,
                "reachable": bool(client.ping()),
            },
        )
    except Exception as error:
        print_json(
            "redis",
            {
                "configured": True,
                "reachable": False,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )


def main() -> None:
    order_id = sys.argv[1] if len(sys.argv) > 1 else None
    check_mysql(order_id)
    check_redis()


if __name__ == "__main__":
    main()
