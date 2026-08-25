import json
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from app.core.config import get_settings


MYSQL_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS customer_profiles (
        user_id VARCHAR(64) PRIMARY KEY,
        user_name VARCHAR(128) NOT NULL,
        account_status VARCHAR(32) NOT NULL DEFAULT 'normal',
        refund_count_30d INT NOT NULL DEFAULT 0,
        complaint_count_30d INT NOT NULL DEFAULT 0,
        risk_tags JSON NULL,
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id VARCHAR(64) PRIMARY KEY,
        user_id VARCHAR(64) NOT NULL,
        product_name VARCHAR(255) NOT NULL,
        category VARCHAR(128) NOT NULL,
        amount DECIMAL(12, 2) NOT NULL DEFAULT 0,
        payment_status VARCHAR(32) NOT NULL,
        order_status VARCHAR(64) NOT NULL,
        shipping_status VARCHAR(255) NULL,
        signed_date DATE NULL,
        warranty_months INT NOT NULL DEFAULT 0,
        return_window_days INT NOT NULL DEFAULT 7,
        after_sales_status VARCHAR(64) NOT NULL DEFAULT 'none',
        notes TEXT NULL,
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_orders_user_id (user_id),
        CONSTRAINT fk_orders_user
            FOREIGN KEY (user_id) REFERENCES customer_profiles(user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        ticket_id VARCHAR(64) PRIMARY KEY,
        order_id VARCHAR(64) NULL,
        user_id VARCHAR(64) NULL,
        issue_type VARCHAR(64) NOT NULL,
        priority VARCHAR(32) NOT NULL,
        status VARCHAR(64) NOT NULL,
        user_request TEXT NOT NULL,
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_tickets_order_id (order_id),
        INDEX idx_tickets_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS refund_requests (
        refund_id VARCHAR(64) PRIMARY KEY,
        order_id VARCHAR(64) NOT NULL,
        user_id VARCHAR(64) NULL,
        amount DECIMAL(12, 2) NOT NULL,
        reason VARCHAR(64) NOT NULL,
        status VARCHAR(64) NOT NULL,
        risk_level VARCHAR(32) NOT NULL,
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_refunds_order_id (order_id),
        INDEX idx_refunds_status (status),
        INDEX idx_refunds_order_status (order_id, status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS manual_reviews (
        review_id VARCHAR(64) PRIMARY KEY,
        order_id VARCHAR(64) NULL,
        user_id VARCHAR(64) NULL,
        review_type VARCHAR(64) NOT NULL,
        risk_level VARCHAR(32) NOT NULL,
        status VARCHAR(64) NOT NULL DEFAULT 'pending_review',
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_reviews_status (status),
        INDEX idx_reviews_order_id (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS mq_messages (
        message_id VARCHAR(64) PRIMARY KEY,
        topic VARCHAR(128) NOT NULL,
        status VARCHAR(32) NOT NULL,
        attempts INT NOT NULL DEFAULT 0,
        payload JSON NOT NULL,
        result JSON NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_mq_topic_status (topic, status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id VARCHAR(64) PRIMARY KEY,
        user_id VARCHAR(64) NULL,
        channel VARCHAR(32) NOT NULL,
        content TEXT NOT NULL,
        status VARCHAR(32) NOT NULL,
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_notifications_user_id (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_messages (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        conversation_id VARCHAR(64) NOT NULL,
        role VARCHAR(32) NOT NULL,
        content TEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_messages_conversation_id (conversation_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_tasks (
        conversation_id VARCHAR(64) PRIMARY KEY,
        task_json JSON NOT NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        conversation_id VARCHAR(64) NOT NULL,
        score INT NOT NULL,
        comment TEXT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_feedback_conversation_id (conversation_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_metrics (
        metric_id VARCHAR(64) PRIMARY KEY,
        trace_id VARCHAR(64) NOT NULL,
        conversation_id VARCHAR(64) NULL,
        success TINYINT(1) NOT NULL,
        duration_ms DECIMAL(12, 2) NULL,
        token_usage JSON NOT NULL,
        error_info JSON NULL,
        payload JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_metrics_trace_id (trace_id),
        INDEX idx_metrics_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

_MYSQL_INITIALIZED = False


def parse_mysql_dsn(dsn: str) -> dict:
    parsed = urlparse(dsn)

    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("MYSQL_DSN must use mysql:// or mysql+pymysql://")

    database = parsed.path.lstrip("/")
    if not parsed.hostname or not database:
        raise ValueError("MYSQL_DSN must include host and database name")

    query = parse_qs(parsed.query)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": query.get("charset", ["utf8mb4"])[0],
        "connect_timeout": int(query.get("connect_timeout", ["5"])[0]),
    }


def mysql_database_name() -> str | None:
    dsn = get_settings().mysql_dsn
    if not dsn:
        return None

    return parse_mysql_dsn(dsn)["database"]


def get_mysql_connection(autocommit: bool = True):
    import pymysql

    options = parse_mysql_dsn(get_settings().mysql_dsn)
    return pymysql.connect(
        host=options["host"],
        port=options["port"],
        user=options["user"],
        password=options["password"],
        database=options["database"],
        charset=options["charset"],
        connect_timeout=options["connect_timeout"],
        autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )


def normalize_json_value(value):
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    return value


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=normalize_json_value)


def from_json(value):
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")

    return json.loads(value)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_mysql_database() -> None:
    if not _MYSQL_INITIALIZED:
        init_mysql_database()


def init_mysql_database() -> None:
    global _MYSQL_INITIALIZED

    if _MYSQL_INITIALIZED:
        return

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            for statement in MYSQL_SCHEMA:
                cursor.execute(statement)
            apply_mysql_migrations(cursor)

    seed_customer_profiles_to_mysql()
    seed_orders_to_mysql()
    _MYSQL_INITIALIZED = True


def mysql_column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table_name, column_name),
    )
    row = cursor.fetchone()

    return bool(row and row["count"])


def mysql_index_exists(cursor, table_name: str, index_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table_name, index_name),
    )
    row = cursor.fetchone()

    return bool(row and row["count"])


def apply_mysql_migrations(cursor) -> None:
    if not mysql_column_exists(cursor, "customer_profiles", "payload"):
        cursor.execute(
            "ALTER TABLE customer_profiles ADD COLUMN payload JSON NULL AFTER risk_tags"
        )

    if not mysql_index_exists(cursor, "refund_requests", "idx_refunds_order_status"):
        cursor.execute(
            "ALTER TABLE refund_requests ADD INDEX idx_refunds_order_status (order_id, status, created_at)"
        )


def seed_customer_profiles_to_mysql() -> None:
    from app.storage.database import DEFAULT_CUSTOMER_PROFILES

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            for profile in DEFAULT_CUSTOMER_PROFILES:
                payload = {
                    **profile,
                    "updated_at": now_text(),
                }
                cursor.execute(
                    """
                    INSERT INTO customer_profiles (
                        user_id, user_name, account_status, refund_count_30d,
                        complaint_count_30d, risk_tags, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        user_name = VALUES(user_name),
                        account_status = VALUES(account_status),
                        refund_count_30d = VALUES(refund_count_30d),
                        complaint_count_30d = VALUES(complaint_count_30d),
                        risk_tags = VALUES(risk_tags),
                        payload = VALUES(payload)
                    """,
                    (
                        profile["user_id"],
                        profile.get("user_name", profile["user_id"]),
                        profile.get("account_status", "normal"),
                        int(profile.get("refund_count_30d") or 0),
                        int(profile.get("complaint_count_30d") or 0),
                        to_json(profile.get("risk_tags", [])),
                        to_json(payload),
                    ),
                )


def seed_orders_to_mysql() -> None:
    from app.storage.database import ORDERS_SEED_PATH, normalize_order

    if not ORDERS_SEED_PATH.exists():
        return

    orders = json.loads(ORDERS_SEED_PATH.read_text(encoding="utf-8"))

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            for order in orders:
                normalized = normalize_order(order)
                upsert_order(cursor, normalized)


def signed_date_or_none(value):
    return value or None


def upsert_order(cursor, order: dict) -> None:
    cursor.execute(
        """
        INSERT INTO orders (
            order_id, user_id, product_name, category, amount, payment_status,
            order_status, shipping_status, signed_date, warranty_months,
            return_window_days, after_sales_status, notes, payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            user_id = VALUES(user_id),
            product_name = VALUES(product_name),
            category = VALUES(category),
            amount = VALUES(amount),
            payment_status = VALUES(payment_status),
            order_status = VALUES(order_status),
            shipping_status = VALUES(shipping_status),
            signed_date = VALUES(signed_date),
            warranty_months = VALUES(warranty_months),
            return_window_days = VALUES(return_window_days),
            after_sales_status = VALUES(after_sales_status),
            notes = VALUES(notes),
            payload = VALUES(payload)
        """,
        (
            str(order["order_id"]),
            order.get("user_id"),
            order.get("product_name", ""),
            order.get("category", ""),
            float(order.get("amount") or 0),
            order.get("payment_status", "paid"),
            order.get("order_status", ""),
            order.get("shipping_status"),
            signed_date_or_none(order.get("signed_date")),
            int(order.get("warranty_months") or 0),
            int(order.get("return_window_days") or 7),
            order.get("after_sales_status", "none"),
            order.get("notes"),
            to_json(order),
        ),
    )


def load_orders_from_mysql() -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM orders ORDER BY order_id")
            rows = cursor.fetchall()

    return [from_json(row["payload"]) for row in rows]


def get_order_from_mysql(order_id: str) -> dict | None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM orders WHERE order_id = %s",
                (str(order_id),),
            )
            row = cursor.fetchone()

    return from_json(row["payload"]) if row else None


def update_order_in_mysql(order_id: str, updates: dict) -> dict | None:
    ensure_mysql_database()
    order = get_order_from_mysql(order_id)

    if not order:
        return None

    order.update(updates)

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            upsert_order(cursor, order)

    return order


def get_customer_profile_from_mysql(user_id: str | None) -> dict | None:
    if not user_id:
        return None

    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM customer_profiles WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()

    return from_json(row["payload"]) if row else None


def save_refund_request_to_mysql(refund_request: dict) -> dict:
    ensure_mysql_database()
    now = now_text()
    saved = {
        "refund_id": refund_request.get("refund_id") or f"R-{uuid4().hex[:12]}",
        "created_at": refund_request.get("created_at") or now,
        "updated_at": now,
        **refund_request,
    }

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO refund_requests (
                    refund_id, order_id, user_id, amount, reason, status,
                    risk_level, payload, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    saved["refund_id"],
                    saved["order_id"],
                    saved.get("user_id"),
                    float(saved.get("amount") or 0),
                    saved["reason"],
                    saved["status"],
                    saved["risk_level"],
                    to_json(saved),
                    saved["created_at"],
                    saved["updated_at"],
                ),
            )

    return saved


def get_refund_request_from_mysql(refund_id: str) -> dict | None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM refund_requests WHERE refund_id = %s",
                (refund_id,),
            )
            row = cursor.fetchone()

    return from_json(row["payload"]) if row else None


def get_active_refund_request_by_order_id_from_mysql(order_id: str) -> dict | None:
    ensure_mysql_database()
    inactive_statuses = {"failed", "rejected", "cancelled", "canceled"}

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload FROM refund_requests
                WHERE order_id = %s
                ORDER BY created_at DESC
                """,
                (str(order_id),),
            )
            rows = cursor.fetchall()

    for row in rows:
        refund_request = from_json(row["payload"])
        if refund_request.get("status") not in inactive_statuses:
            return refund_request

    return None


def update_refund_request_in_mysql(refund_id: str, updates: dict) -> dict | None:
    ensure_mysql_database()
    refund_request = get_refund_request_from_mysql(refund_id)

    if not refund_request:
        return None

    refund_request.update(updates)
    refund_request["updated_at"] = now_text()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE refund_requests
                SET status = %s, risk_level = %s, payload = %s, updated_at = %s
                WHERE refund_id = %s
                """,
                (
                    refund_request["status"],
                    refund_request["risk_level"],
                    to_json(refund_request),
                    refund_request["updated_at"],
                    refund_id,
                ),
            )

    return refund_request


def list_refund_requests_from_mysql(limit: int = 50) -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload FROM refund_requests
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    return [from_json(row["payload"]) for row in rows]


def save_manual_review_to_mysql(review: dict) -> dict:
    ensure_mysql_database()
    now = now_text()
    saved = {
        "review_id": review.get("review_id") or f"H-{uuid4().hex[:12]}",
        "status": review.get("status", "pending_review"),
        "created_at": review.get("created_at") or now,
        "updated_at": now,
        **review,
    }

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO manual_reviews (
                    review_id, order_id, user_id, review_type, risk_level,
                    status, payload, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    saved["review_id"],
                    saved.get("order_id"),
                    saved.get("user_id"),
                    saved["review_type"],
                    saved["risk_level"],
                    saved["status"],
                    to_json(saved),
                    saved["created_at"],
                    saved["updated_at"],
                ),
            )

    return saved


def list_manual_reviews_from_mysql(limit: int = 50) -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload FROM manual_reviews
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    return [from_json(row["payload"]) for row in rows]


def enqueue_mq_message_to_mysql(topic: str, payload: dict) -> dict:
    ensure_mysql_database()
    now = now_text()
    message = {
        "message_id": f"MQ-{uuid4().hex[:12]}",
        "topic": topic,
        "status": "pending",
        "attempts": 0,
        "payload": payload,
        "result": None,
        "created_at": now,
        "updated_at": now,
    }

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO mq_messages (
                    message_id, topic, status, attempts, payload,
                    result, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    message["message_id"],
                    topic,
                    message["status"],
                    message["attempts"],
                    to_json(payload),
                    None,
                    now,
                    now,
                ),
            )

    return message


def build_message_from_row(row: dict, status: str | None = None, attempts: int | None = None) -> dict:
    return {
        "message_id": row["message_id"],
        "topic": row["topic"],
        "status": status or row["status"],
        "attempts": attempts if attempts is not None else row["attempts"],
        "payload": from_json(row["payload"]),
        "result": from_json(row["result"]) if row.get("result") else None,
        "created_at": normalize_json_value(row["created_at"]),
        "updated_at": normalize_json_value(row["updated_at"]),
    }


def claim_mq_messages_from_mysql(topic: str | None = None, limit: int = 10) -> list[dict]:
    ensure_mysql_database()

    connection = get_mysql_connection(autocommit=False)
    try:
        with connection.cursor() as cursor:
            if topic:
                cursor.execute(
                    """
                    SELECT message_id, topic, status, attempts, payload, result,
                           created_at, updated_at
                    FROM mq_messages
                    WHERE status = 'pending' AND topic = %s
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE
                    """,
                    (topic, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT message_id, topic, status, attempts, payload, result,
                           created_at, updated_at
                    FROM mq_messages
                    WHERE status = 'pending'
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE
                    """,
                    (limit,),
                )

            rows = cursor.fetchall()
            messages = []

            for row in rows:
                attempts = int(row["attempts"] or 0) + 1
                updated_at = now_text()
                cursor.execute(
                    """
                    UPDATE mq_messages
                    SET status = 'processing', attempts = %s, updated_at = %s
                    WHERE message_id = %s AND status = 'pending'
                    """,
                    (attempts, updated_at, row["message_id"]),
                )
                row["updated_at"] = updated_at
                messages.append(
                    build_message_from_row(row, status="processing", attempts=attempts)
                )

        connection.commit()
        return messages
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_mq_message_status_in_mysql(
    message_id: str,
    status: str,
    result: dict | None = None,
) -> dict | None:
    ensure_mysql_database()
    updated_at = now_text()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT message_id, topic, status, attempts, payload, result,
                       created_at, updated_at
                FROM mq_messages
                WHERE message_id = %s
                """,
                (message_id,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            cursor.execute(
                """
                UPDATE mq_messages
                SET status = %s, result = %s, updated_at = %s
                WHERE message_id = %s
                """,
                (
                    status,
                    to_json(result) if result is not None else None,
                    updated_at,
                    message_id,
                ),
            )

    row["updated_at"] = updated_at
    return {
        **build_message_from_row(row, status=status),
        "result": result,
    }


def list_mq_messages_from_mysql(limit: int = 50) -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT message_id, topic, status, attempts, payload, result,
                       created_at, updated_at
                FROM mq_messages
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    return [build_message_from_row(row) for row in rows]


def save_notification_to_mysql(notification: dict) -> dict:
    ensure_mysql_database()
    saved = {
        "notification_id": notification.get("notification_id") or f"N-{uuid4().hex[:12]}",
        "status": notification.get("status", "sent"),
        "created_at": notification.get("created_at") or now_text(),
        **notification,
    }

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO notifications (
                    notification_id, user_id, channel, content, status, payload, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    saved["notification_id"],
                    saved.get("user_id"),
                    saved["channel"],
                    saved["content"],
                    saved["status"],
                    to_json(saved),
                    saved["created_at"],
                ),
            )

    return saved


def save_agent_metric_to_mysql(trace: dict) -> None:
    ensure_mysql_database()
    error_events = [
        event
        for event in trace.get("events", [])
        if event.get("event_type") in {"error", "tool_failed"}
    ]
    error_info = error_events[-1] if error_events else None

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_metrics (
                    metric_id, trace_id, conversation_id, success, duration_ms,
                    token_usage, error_info, payload, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"M-{uuid4().hex[:12]}",
                    trace.get("trace_id"),
                    trace.get("conversation_id"),
                    1 if trace.get("success") else 0,
                    trace.get("duration_ms"),
                    to_json(trace.get("token_usage", {})),
                    to_json(error_info) if error_info else None,
                    to_json(trace),
                    now_text(),
                ),
            )


def list_agent_metrics_from_mysql(limit: int = 50) -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload FROM agent_metrics
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    return [from_json(row["payload"]) for row in rows]


def save_ticket_to_mysql(ticket: dict) -> dict:
    ensure_mysql_database()
    saved = {
        "ticket_id": ticket.get("ticket_id") or f"T-{uuid4().hex[:12]}",
        "created_at": ticket.get("created_at") or now_text(),
        **ticket,
    }

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tickets (
                    ticket_id, order_id, user_id, issue_type, priority, status,
                    user_request, payload, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    saved["ticket_id"],
                    saved.get("order_id"),
                    saved.get("user_id"),
                    saved["issue_type"],
                    saved["priority"],
                    saved["status"],
                    saved["user_request"],
                    to_json(saved),
                    saved["created_at"],
                ),
            )

    return saved


def list_tickets_from_mysql(limit: int = 50) -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload FROM tickets
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    return [from_json(row["payload"]) for row in rows]


def append_message_to_mysql(conversation_id: str, role: str, content: str) -> None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (conversation_id, role, content, now_text()),
            )


def load_messages_from_mysql(conversation_id: str, limit: int) -> list[dict]:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT role, content
                FROM conversation_messages
                WHERE conversation_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (conversation_id, limit),
            )
            rows = cursor.fetchall()

    return list(reversed([
        {"role": row["role"], "content": row["content"]}
        for row in rows
    ]))


def set_pending_task_in_mysql(conversation_id: str, task: dict) -> None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pending_tasks (conversation_id, task_json, updated_at)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    task_json = VALUES(task_json),
                    updated_at = VALUES(updated_at)
                """,
                (conversation_id, to_json(task), now_text()),
            )


def get_pending_task_from_mysql(conversation_id: str) -> dict | None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT task_json FROM pending_tasks WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cursor.fetchone()

    return from_json(row["task_json"]) if row else None


def clear_pending_task_in_mysql(conversation_id: str) -> None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM pending_tasks WHERE conversation_id = %s",
                (conversation_id,),
            )


def save_feedback_to_mysql(conversation_id: str, score: int, comment: str | None) -> None:
    ensure_mysql_database()

    with get_mysql_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO feedback (conversation_id, score, comment, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (conversation_id, score, comment, now_text()),
            )


def mysql_health() -> dict:
    try:
        with get_mysql_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone()
        return {
            "backend": "mysql",
            "configured": True,
            "reachable": bool(row and row.get("ok") == 1),
            "database": mysql_database_name(),
        }
    except Exception as error:
        return {
            "backend": "mysql",
            "configured": True,
            "reachable": False,
            "database": None,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
