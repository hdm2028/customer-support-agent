import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import BASE_DIR, load_env_file


load_env_file()
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "customer_support.db")))
ORDERS_SEED_PATH = DATA_DIR / "orders.json"

_INITIALIZED = False

ORDER_BUSINESS_DEFAULTS = {
    "10001": {"user_id": "u001", "amount": 399.0, "payment_status": "paid"},
    "10002": {"user_id": "u002", "amount": 299.0, "payment_status": "paid"},
    "10003": {"user_id": "u003", "amount": 129.0, "payment_status": "paid"},
    "10004": {"user_id": "u004", "amount": 1899.0, "payment_status": "paid"},
    "10005": {"user_id": "u005", "amount": 59.0, "payment_status": "unpaid"},
    "10006": {"user_id": "u006", "amount": 329.0, "payment_status": "paid"},
    "10007": {"user_id": "u007", "amount": 499.0, "payment_status": "payment_pending"},
    "10008": {"user_id": "u008", "amount": 1299.0, "payment_status": "paid"},
    "10009": {"user_id": "u009", "amount": 899.0, "payment_status": "paid"},
    "10010": {"user_id": "u010", "amount": 1599.0, "payment_status": "paid"},
    "10011": {"user_id": "u011", "amount": 699.0, "payment_status": "paid"},
}

DEFAULT_CUSTOMER_PROFILES = [
    {
        "user_id": "u001",
        "user_name": "张三",
        "account_status": "normal",
        "refund_count_30d": 1,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
    {
        "user_id": "u002",
        "user_name": "李四",
        "account_status": "normal",
        "refund_count_30d": 0,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
    {
        "user_id": "u003",
        "user_name": "王五",
        "account_status": "normal",
        "refund_count_30d": 4,
        "complaint_count_30d": 1,
        "risk_tags": ["高频退款"],
    },
    {
        "user_id": "u004",
        "user_name": "赵六",
        "account_status": "abnormal",
        "refund_count_30d": 3,
        "complaint_count_30d": 2,
        "risk_tags": ["账号异常", "投诉频繁"],
    },
    {
        "user_id": "u005",
        "user_name": "陈晨",
        "account_status": "normal",
        "refund_count_30d": 0,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
    {
        "user_id": "u006",
        "user_name": "孙小雨",
        "account_status": "normal",
        "refund_count_30d": 1,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
    {
        "user_id": "u007",
        "user_name": "周明",
        "account_status": "normal",
        "refund_count_30d": 0,
        "complaint_count_30d": 0,
        "risk_tags": ["支付待核验"],
    },
    {
        "user_id": "u008",
        "user_name": "吴敏",
        "account_status": "normal",
        "refund_count_30d": 1,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
    {
        "user_id": "u009",
        "user_name": "郑楠",
        "account_status": "normal",
        "refund_count_30d": 0,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
    {
        "user_id": "u010",
        "user_name": "林可",
        "account_status": "normal",
        "refund_count_30d": 2,
        "complaint_count_30d": 1,
        "risk_tags": ["发货时效待确认"],
    },
    {
        "user_id": "u011",
        "user_name": "何安",
        "account_status": "normal",
        "refund_count_30d": 0,
        "complaint_count_30d": 0,
        "risk_tags": [],
    },
]


def now_text() -> str:
    """返回统一格式的创建时间，便于数据库记录和排查问题。"""

    return datetime.now().isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    """创建 SQLite 连接，并让查询结果可以按字段名读取。"""

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_database() -> None:
    """初始化数据库表，并把 orders.json 作为订单种子数据导入。"""

    global _INITIALIZED

    if _INITIALIZED:
        return

    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                order_id TEXT,
                issue_type TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                user_request TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
            ON conversation_messages(conversation_id, id);

            CREATE TABLE IF NOT EXISTS pending_tasks (
                conversation_id TEXT PRIMARY KEY,
                task_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                score INTEGER NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_profiles (
                user_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS refund_requests (
                refund_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                user_id TEXT,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manual_reviews (
                review_id TEXT PRIMARY KEY,
                order_id TEXT,
                user_id TEXT,
                review_type TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mq_messages (
                message_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mq_messages_topic_status
            ON mq_messages(topic, status, created_at);

            CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY,
                user_id TEXT,
                channel TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_metrics (
                metric_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                conversation_id TEXT,
                success INTEGER NOT NULL,
                duration_ms REAL,
                token_usage TEXT NOT NULL,
                error_info TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    seed_orders_from_json()
    seed_customer_profiles()
    _INITIALIZED = True


def ensure_database() -> None:
    """确保任意数据读写前，数据库结构都已经存在。"""

    init_database()


def seed_orders_from_json(path: Path = ORDERS_SEED_PATH) -> None:
    """把 JSON 种子订单同步到 SQLite，方便后续统一从数据库读取订单。"""

    if not path.exists():
        return

    orders = json.loads(path.read_text(encoding="utf-8"))

    with get_connection() as connection:
        for order in orders:
            normalized_order = normalize_order(order)
            connection.execute(
                """
                INSERT INTO orders (order_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    str(normalized_order["order_id"]),
                    json.dumps(normalized_order, ensure_ascii=False),
                    now_text(),
                ),
            )


def normalize_order(order: dict) -> dict:
    """给旧版订单种子补齐业务字段，避免改动旧数据也能跑通新流程。"""

    defaults = ORDER_BUSINESS_DEFAULTS.get(str(order["order_id"]), {})
    normalized = {
        "user_id": defaults.get("user_id", f"u{order['order_id']}"),
        "amount": defaults.get("amount", 0.0),
        "payment_status": defaults.get("payment_status", "paid"),
        "after_sales_status": order.get("after_sales_status", "none"),
        **order,
    }

    for key, value in defaults.items():
        normalized.setdefault(key, value)

    return normalized


def seed_customer_profiles() -> None:
    """写入客户画像种子数据，供风控 Agent 判断高频退款、异常账号等风险。"""

    with get_connection() as connection:
        for profile in DEFAULT_CUSTOMER_PROFILES:
            connection.execute(
                """
                INSERT INTO customer_profiles (user_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    profile["user_id"],
                    json.dumps(profile, ensure_ascii=False),
                    now_text(),
                ),
            )


def load_orders_from_db() -> list[dict]:
    """读取全部订单。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            "SELECT payload FROM orders ORDER BY order_id"
        ).fetchall()

    return [
        json.loads(row["payload"])
        for row in rows
    ]


def get_order_from_db(order_id: str) -> dict | None:
    """按订单号读取单个订单。"""

    ensure_database()

    with get_connection() as connection:
        row = connection.execute(
            "SELECT payload FROM orders WHERE order_id = ?",
            (str(order_id),),
        ).fetchone()

    if not row:
        return None

    return json.loads(row["payload"])


def update_order_in_db(order_id: str, updates: dict) -> dict | None:
    """更新订单 JSON payload，用于退款处理服务写回订单状态。"""

    ensure_database()
    order = get_order_from_db(order_id)

    if not order:
        return None

    order.update(updates)

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE orders
            SET payload = ?, updated_at = ?
            WHERE order_id = ?
            """,
            (
                json.dumps(order, ensure_ascii=False),
                now_text(),
                str(order_id),
            ),
        )

    return order


def get_customer_profile_from_db(user_id: str | None) -> dict | None:
    """读取客户画像。"""

    if not user_id:
        return None

    ensure_database()

    with get_connection() as connection:
        row = connection.execute(
            "SELECT payload FROM customer_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return None

    return json.loads(row["payload"])


def save_refund_request_to_db(refund_request: dict) -> dict:
    """保存退款申请，返回带 refund_id、状态和时间戳的记录。"""

    ensure_database()
    now = now_text()
    saved = {
        "refund_id": refund_request.get("refund_id") or f"R-{uuid4().hex[:12]}",
        "created_at": refund_request.get("created_at") or now,
        "updated_at": now,
        **refund_request,
    }

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO refund_requests (
                refund_id, order_id, user_id, amount, reason, status,
                risk_level, payload, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved["refund_id"],
                saved["order_id"],
                saved.get("user_id"),
                float(saved.get("amount") or 0),
                saved["reason"],
                saved["status"],
                saved["risk_level"],
                json.dumps(saved, ensure_ascii=False),
                saved["created_at"],
                saved["updated_at"],
            ),
        )

    return saved


def get_refund_request_from_db(refund_id: str) -> dict | None:
    """按退款申请号读取退款申请。"""

    ensure_database()

    with get_connection() as connection:
        row = connection.execute(
            "SELECT payload FROM refund_requests WHERE refund_id = ?",
            (refund_id,),
        ).fetchone()

    if not row:
        return None

    return json.loads(row["payload"])


def update_refund_request_in_db(refund_id: str, updates: dict) -> dict | None:
    """更新退款申请状态或处理结果。"""

    ensure_database()
    refund_request = get_refund_request_from_db(refund_id)

    if not refund_request:
        return None

    refund_request.update(updates)
    refund_request["updated_at"] = now_text()

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE refund_requests
            SET status = ?, risk_level = ?, payload = ?, updated_at = ?
            WHERE refund_id = ?
            """,
            (
                refund_request["status"],
                refund_request["risk_level"],
                json.dumps(refund_request, ensure_ascii=False),
                refund_request["updated_at"],
                refund_id,
            ),
        )

    return refund_request


def list_refund_requests_from_db(limit: int = 50) -> list[dict]:
    """按创建时间倒序读取退款申请。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT payload FROM refund_requests
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [json.loads(row["payload"]) for row in rows]


def save_manual_review_to_db(review: dict) -> dict:
    """保存人工审核单。"""

    ensure_database()
    now = now_text()
    saved = {
        "review_id": review.get("review_id") or f"H-{uuid4().hex[:12]}",
        "status": review.get("status", "pending_review"),
        "created_at": review.get("created_at") or now,
        "updated_at": now,
        **review,
    }

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO manual_reviews (
                review_id, order_id, user_id, review_type, risk_level,
                status, payload, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved["review_id"],
                saved.get("order_id"),
                saved.get("user_id"),
                saved["review_type"],
                saved["risk_level"],
                saved["status"],
                json.dumps(saved, ensure_ascii=False),
                saved["created_at"],
                saved["updated_at"],
            ),
        )

    return saved


def list_manual_reviews_from_db(limit: int = 50) -> list[dict]:
    """按创建时间倒序读取人工审核单。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT payload FROM manual_reviews
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [json.loads(row["payload"]) for row in rows]


def enqueue_mq_message_to_db(topic: str, payload: dict) -> dict:
    """写入一条待消费 MQ 消息。本地用 SQLite 模拟队列，生产可替换成 RabbitMQ/Kafka。"""

    ensure_database()
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

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO mq_messages (
                message_id, topic, status, attempts, payload,
                result, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message["message_id"],
                topic,
                message["status"],
                message["attempts"],
                json.dumps(payload, ensure_ascii=False),
                None,
                now,
                now,
            ),
        )

    return message


def claim_mq_messages_from_db(topic: str | None = None, limit: int = 10) -> list[dict]:
    """领取待消费消息，并标记为 processing。"""

    ensure_database()

    if topic:
        query = """
            SELECT message_id, topic, status, attempts, payload, result, created_at, updated_at
            FROM mq_messages
            WHERE status = 'pending' AND topic = ?
            ORDER BY created_at
            LIMIT ?
        """
        params = (topic, limit)
    else:
        query = """
            SELECT message_id, topic, status, attempts, payload, result, created_at, updated_at
            FROM mq_messages
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT ?
        """
        params = (limit,)

    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()
        messages = []

        for row in rows:
            attempts = int(row["attempts"] or 0) + 1
            updated_at = now_text()
            connection.execute(
                """
                UPDATE mq_messages
                SET status = 'processing', attempts = ?, updated_at = ?
                WHERE message_id = ? AND status = 'pending'
                """,
                (attempts, updated_at, row["message_id"]),
            )
            messages.append(
                {
                    "message_id": row["message_id"],
                    "topic": row["topic"],
                    "status": "processing",
                    "attempts": attempts,
                    "payload": json.loads(row["payload"]),
                    "result": json.loads(row["result"]) if row["result"] else None,
                    "created_at": row["created_at"],
                    "updated_at": updated_at,
                }
            )

    return messages


def update_mq_message_status_in_db(message_id: str, status: str, result: dict | None = None) -> dict | None:
    """更新 MQ 消息状态。"""

    ensure_database()
    updated_at = now_text()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT message_id, topic, attempts, payload, created_at
            FROM mq_messages
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()

        if not row:
            return None

        connection.execute(
            """
            UPDATE mq_messages
            SET status = ?, result = ?, updated_at = ?
            WHERE message_id = ?
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                updated_at,
                message_id,
            ),
        )

    return {
        "message_id": row["message_id"],
        "topic": row["topic"],
        "status": status,
        "attempts": row["attempts"],
        "payload": json.loads(row["payload"]),
        "result": result,
        "created_at": row["created_at"],
        "updated_at": updated_at,
    }


def list_mq_messages_from_db(limit: int = 50) -> list[dict]:
    """查看最近的 MQ 消息。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT message_id, topic, status, attempts, payload, result, created_at, updated_at
            FROM mq_messages
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "message_id": row["message_id"],
            "topic": row["topic"],
            "status": row["status"],
            "attempts": row["attempts"],
            "payload": json.loads(row["payload"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def save_notification_to_db(notification: dict) -> dict:
    """保存用户通知记录。"""

    ensure_database()
    saved = {
        "notification_id": notification.get("notification_id") or f"N-{uuid4().hex[:12]}",
        "status": notification.get("status", "sent"),
        "created_at": notification.get("created_at") or now_text(),
        **notification,
    }

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO notifications (
                notification_id, user_id, channel, content, status, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved["notification_id"],
                saved.get("user_id"),
                saved["channel"],
                saved["content"],
                saved["status"],
                json.dumps(saved, ensure_ascii=False),
                saved["created_at"],
            ),
        )

    return saved


def save_agent_metric_to_db(trace: dict) -> None:
    """把 trace 的关键指标落库，便于后续做评测和失败案例分析。"""

    ensure_database()
    error_events = [
        event
        for event in trace.get("events", [])
        if event.get("event_type") in {"error", "tool_failed"}
    ]
    error_info = error_events[-1] if error_events else None

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO agent_metrics (
                metric_id, trace_id, conversation_id, success, duration_ms,
                token_usage, error_info, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"M-{uuid4().hex[:12]}",
                trace.get("trace_id"),
                trace.get("conversation_id"),
                1 if trace.get("success") else 0,
                trace.get("duration_ms"),
                json.dumps(trace.get("token_usage", {}), ensure_ascii=False),
                json.dumps(error_info, ensure_ascii=False) if error_info else None,
                json.dumps(trace, ensure_ascii=False),
                now_text(),
            ),
        )


def list_agent_metrics_from_db(limit: int = 50) -> list[dict]:
    """查看最近 Agent 指标记录。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT payload FROM agent_metrics
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [json.loads(row["payload"]) for row in rows]


def save_ticket_to_db(ticket: dict) -> dict:
    """保存工单草稿，并返回带 ticket_id 和 created_at 的工单。"""

    ensure_database()

    saved_ticket = {
        "ticket_id": ticket.get("ticket_id") or f"T-{uuid4().hex[:12]}",
        "created_at": ticket.get("created_at") or now_text(),
        **ticket,
    }

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO tickets (
                ticket_id, order_id, issue_type, priority, status,
                user_request, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved_ticket["ticket_id"],
                saved_ticket.get("order_id"),
                saved_ticket["issue_type"],
                saved_ticket["priority"],
                saved_ticket["status"],
                saved_ticket["user_request"],
                json.dumps(saved_ticket, ensure_ascii=False),
                saved_ticket["created_at"],
            ),
        )

    return saved_ticket


def list_tickets_from_db(limit: int = 50) -> list[dict]:
    """按创建时间倒序读取最近的工单草稿。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT payload FROM tickets
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        json.loads(row["payload"])
        for row in rows
    ]


def append_message_to_db(conversation_id: str, role: str, content: str) -> None:
    """保存一条会话消息。"""

    ensure_database()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO conversation_messages (conversation_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (conversation_id, role, content, now_text()),
        )


def load_messages_from_db(conversation_id: str, limit: int) -> list[dict]:
    """读取某个会话最近的消息。"""

    ensure_database()

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT role, content
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()

    messages = [
        {
            "role": row["role"],
            "content": row["content"],
        }
        for row in rows
    ]

    return list(reversed(messages))


def set_pending_task_in_db(conversation_id: str, task: dict) -> None:
    """保存或更新某个会话的待补全任务。"""

    ensure_database()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO pending_tasks (conversation_id, task_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                task_json = excluded.task_json,
                updated_at = excluded.updated_at
            """,
            (
                conversation_id,
                json.dumps(task, ensure_ascii=False),
                now_text(),
            ),
        )


def get_pending_task_from_db(conversation_id: str) -> dict | None:
    """读取某个会话的待补全任务。"""

    ensure_database()

    with get_connection() as connection:
        row = connection.execute(
            "SELECT task_json FROM pending_tasks WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()

    if not row:
        return None

    return json.loads(row["task_json"])


def clear_pending_task_in_db(conversation_id: str) -> None:
    """清除某个会话已经完成的待补全任务。"""

    ensure_database()

    with get_connection() as connection:
        connection.execute(
            "DELETE FROM pending_tasks WHERE conversation_id = ?",
            (conversation_id,),
        )


def save_feedback_to_db(conversation_id: str, score: int, comment: str | None) -> None:
    """保存用户反馈。"""

    ensure_database()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO feedback (conversation_id, score, comment, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (conversation_id, score, comment, now_text()),
        )


_sqlite_init_database = init_database
_sqlite_ensure_database = ensure_database
_sqlite_seed_orders_from_json = seed_orders_from_json
_sqlite_seed_customer_profiles = seed_customer_profiles
_sqlite_load_orders_from_db = load_orders_from_db
_sqlite_get_order_from_db = get_order_from_db
_sqlite_update_order_in_db = update_order_in_db
_sqlite_get_customer_profile_from_db = get_customer_profile_from_db
_sqlite_save_refund_request_to_db = save_refund_request_to_db
_sqlite_get_refund_request_from_db = get_refund_request_from_db
_sqlite_update_refund_request_in_db = update_refund_request_in_db
_sqlite_list_refund_requests_from_db = list_refund_requests_from_db
_sqlite_save_manual_review_to_db = save_manual_review_to_db
_sqlite_list_manual_reviews_from_db = list_manual_reviews_from_db
_sqlite_enqueue_mq_message_to_db = enqueue_mq_message_to_db
_sqlite_claim_mq_messages_from_db = claim_mq_messages_from_db
_sqlite_update_mq_message_status_in_db = update_mq_message_status_in_db
_sqlite_list_mq_messages_from_db = list_mq_messages_from_db
_sqlite_save_notification_to_db = save_notification_to_db
_sqlite_save_agent_metric_to_db = save_agent_metric_to_db
_sqlite_list_agent_metrics_from_db = list_agent_metrics_from_db
_sqlite_save_ticket_to_db = save_ticket_to_db
_sqlite_list_tickets_from_db = list_tickets_from_db
_sqlite_append_message_to_db = append_message_to_db
_sqlite_load_messages_from_db = load_messages_from_db
_sqlite_set_pending_task_in_db = set_pending_task_in_db
_sqlite_get_pending_task_from_db = get_pending_task_from_db
_sqlite_clear_pending_task_in_db = clear_pending_task_in_db
_sqlite_save_feedback_to_db = save_feedback_to_db


def get_database_backend_name() -> str:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.database_backend == "mysql":
        return "mysql"

    if settings.database_backend == "sqlite":
        return "sqlite"

    if settings.mysql_dsn:
        return "mysql"

    return "sqlite"


def using_mysql_backend() -> bool:
    return get_database_backend_name() == "mysql"


def _mysql_backend():
    from app.storage import mysql_database

    return mysql_database


def _cache_get(key: str):
    try:
        from app.storage.cache import get_json_cache

        return get_json_cache(key)
    except Exception:
        return None


def _cache_set(key: str, value: dict | list | None) -> None:
    if value is None:
        return

    try:
        from app.storage.cache import set_json_cache

        set_json_cache(key, value)
    except Exception:
        pass


def _cache_delete(key: str) -> None:
    try:
        from app.storage.cache import delete_cache

        delete_cache(key)
    except Exception:
        pass


def order_cache_key(order_id: str) -> str:
    return f"business:order:{order_id}"


def customer_profile_cache_key(user_id: str) -> str:
    return f"business:customer_profile:{user_id}"


def init_database() -> None:
    global _INITIALIZED

    if _INITIALIZED:
        return

    if using_mysql_backend():
        _mysql_backend().init_mysql_database()
        _INITIALIZED = True
        return

    _sqlite_init_database()


def ensure_database() -> None:
    init_database()


def seed_orders_from_json(path: Path = ORDERS_SEED_PATH) -> None:
    if using_mysql_backend():
        _mysql_backend().seed_orders_to_mysql()
        return

    _sqlite_seed_orders_from_json(path)


def seed_customer_profiles() -> None:
    if using_mysql_backend():
        _mysql_backend().seed_customer_profiles_to_mysql()
        return

    _sqlite_seed_customer_profiles()


def load_orders_from_db() -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().load_orders_from_mysql()

    return _sqlite_load_orders_from_db()


def get_order_from_db(order_id: str) -> dict | None:
    cache_key = order_cache_key(str(order_id))
    cached = _cache_get(cache_key)

    if cached is not None:
        return cached

    if using_mysql_backend():
        order = _mysql_backend().get_order_from_mysql(order_id)
    else:
        order = _sqlite_get_order_from_db(order_id)

    _cache_set(cache_key, order)
    return order


def update_order_in_db(order_id: str, updates: dict) -> dict | None:
    if using_mysql_backend():
        order = _mysql_backend().update_order_in_mysql(order_id, updates)
    else:
        order = _sqlite_update_order_in_db(order_id, updates)

    if order is None:
        _cache_delete(order_cache_key(str(order_id)))
    else:
        _cache_set(order_cache_key(str(order_id)), order)

    return order


def get_customer_profile_from_db(user_id: str | None) -> dict | None:
    if not user_id:
        return None

    cache_key = customer_profile_cache_key(user_id)
    cached = _cache_get(cache_key)

    if cached is not None:
        return cached

    if using_mysql_backend():
        profile = _mysql_backend().get_customer_profile_from_mysql(user_id)
    else:
        profile = _sqlite_get_customer_profile_from_db(user_id)

    _cache_set(cache_key, profile)
    return profile


def save_refund_request_to_db(refund_request: dict) -> dict:
    if using_mysql_backend():
        return _mysql_backend().save_refund_request_to_mysql(refund_request)

    return _sqlite_save_refund_request_to_db(refund_request)


def get_refund_request_from_db(refund_id: str) -> dict | None:
    if using_mysql_backend():
        return _mysql_backend().get_refund_request_from_mysql(refund_id)

    return _sqlite_get_refund_request_from_db(refund_id)


def update_refund_request_in_db(refund_id: str, updates: dict) -> dict | None:
    if using_mysql_backend():
        return _mysql_backend().update_refund_request_in_mysql(refund_id, updates)

    return _sqlite_update_refund_request_in_db(refund_id, updates)


def list_refund_requests_from_db(limit: int = 50) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().list_refund_requests_from_mysql(limit)

    return _sqlite_list_refund_requests_from_db(limit)


def save_manual_review_to_db(review: dict) -> dict:
    if using_mysql_backend():
        return _mysql_backend().save_manual_review_to_mysql(review)

    return _sqlite_save_manual_review_to_db(review)


def list_manual_reviews_from_db(limit: int = 50) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().list_manual_reviews_from_mysql(limit)

    return _sqlite_list_manual_reviews_from_db(limit)


def enqueue_mq_message_to_db(topic: str, payload: dict) -> dict:
    if using_mysql_backend():
        return _mysql_backend().enqueue_mq_message_to_mysql(topic, payload)

    return _sqlite_enqueue_mq_message_to_db(topic, payload)


def claim_mq_messages_from_db(topic: str | None = None, limit: int = 10) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().claim_mq_messages_from_mysql(topic, limit)

    return _sqlite_claim_mq_messages_from_db(topic, limit)


def update_mq_message_status_in_db(
    message_id: str,
    status: str,
    result: dict | None = None,
) -> dict | None:
    if using_mysql_backend():
        return _mysql_backend().update_mq_message_status_in_mysql(
            message_id,
            status,
            result,
        )

    return _sqlite_update_mq_message_status_in_db(message_id, status, result)


def list_mq_messages_from_db(limit: int = 50) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().list_mq_messages_from_mysql(limit)

    return _sqlite_list_mq_messages_from_db(limit)


def save_notification_to_db(notification: dict) -> dict:
    if using_mysql_backend():
        return _mysql_backend().save_notification_to_mysql(notification)

    return _sqlite_save_notification_to_db(notification)


def save_agent_metric_to_db(trace: dict) -> None:
    if using_mysql_backend():
        _mysql_backend().save_agent_metric_to_mysql(trace)
        return

    _sqlite_save_agent_metric_to_db(trace)


def list_agent_metrics_from_db(limit: int = 50) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().list_agent_metrics_from_mysql(limit)

    return _sqlite_list_agent_metrics_from_db(limit)


def save_ticket_to_db(ticket: dict) -> dict:
    if using_mysql_backend():
        return _mysql_backend().save_ticket_to_mysql(ticket)

    return _sqlite_save_ticket_to_db(ticket)


def list_tickets_from_db(limit: int = 50) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().list_tickets_from_mysql(limit)

    return _sqlite_list_tickets_from_db(limit)


def append_message_to_db(conversation_id: str, role: str, content: str) -> None:
    if using_mysql_backend():
        _mysql_backend().append_message_to_mysql(conversation_id, role, content)
        return

    _sqlite_append_message_to_db(conversation_id, role, content)


def load_messages_from_db(conversation_id: str, limit: int) -> list[dict]:
    if using_mysql_backend():
        return _mysql_backend().load_messages_from_mysql(conversation_id, limit)

    return _sqlite_load_messages_from_db(conversation_id, limit)


def set_pending_task_in_db(conversation_id: str, task: dict) -> None:
    if using_mysql_backend():
        _mysql_backend().set_pending_task_in_mysql(conversation_id, task)
        return

    _sqlite_set_pending_task_in_db(conversation_id, task)


def get_pending_task_from_db(conversation_id: str) -> dict | None:
    if using_mysql_backend():
        return _mysql_backend().get_pending_task_from_mysql(conversation_id)

    return _sqlite_get_pending_task_from_db(conversation_id)


def clear_pending_task_in_db(conversation_id: str) -> None:
    if using_mysql_backend():
        _mysql_backend().clear_pending_task_in_mysql(conversation_id)
        return

    _sqlite_clear_pending_task_in_db(conversation_id)


def save_feedback_to_db(conversation_id: str, score: int, comment: str | None) -> None:
    if using_mysql_backend():
        _mysql_backend().save_feedback_to_mysql(conversation_id, score, comment)
        return

    _sqlite_save_feedback_to_db(conversation_id, score, comment)


def database_health() -> dict:
    if using_mysql_backend():
        return _mysql_backend().mysql_health()

    return {
        "backend": "sqlite",
        "configured": True,
        "reachable": True,
        "path": str(DB_PATH),
    }
