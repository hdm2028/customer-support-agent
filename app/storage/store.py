from typing import Any

from app.core.config import BASE_DIR
from app.storage.database import get_order_from_db, load_orders_from_db

DATA_DIR = BASE_DIR / "data"
ORDERS_PATH = DATA_DIR / "orders.json"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"


def load_orders() -> list[dict[str, Any]]:
    """从 SQLite 读取订单数据。orders.json 只作为种子数据保留。"""

    return load_orders_from_db()


def get_order_by_id(order_id: str) -> dict[str, Any] | None:
    """按订单号从 SQLite 查询订单。"""

    return get_order_from_db(order_id)
