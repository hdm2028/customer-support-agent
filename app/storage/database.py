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


def now_text() -> str:
    """返回统一格式的创建时间，便于数据库记录和排查问题。"""

    return datetime.now().isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    """创建 SQLite 连接，并让查询结果可以按字段名读取。"""

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
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
            """
        )

    seed_orders_from_json()
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
            connection.execute(
                """
                INSERT INTO orders (order_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    str(order["order_id"]),
                    json.dumps(order, ensure_ascii=False),
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
