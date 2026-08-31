from typing import Any

from app.storage.database import get_order_from_db, load_orders_from_db


def load_orders() -> list[dict[str, Any]]:
    return load_orders_from_db()


def get_order_by_id(order_id: str) -> dict[str, Any] | None:
    return get_order_from_db(order_id)
