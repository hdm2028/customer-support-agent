from typing import Any

from app.storage.database import get_order_from_db


def get_order_by_id(order_id: str) -> dict[str, Any] | None:
    from app.core.security import authorize_order
    return authorize_order(get_order_from_db(order_id))
