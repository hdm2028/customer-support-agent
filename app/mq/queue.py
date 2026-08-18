from app.storage.database import (
    claim_mq_messages_from_db,
    enqueue_mq_message_to_db,
    list_mq_messages_from_db,
    update_mq_message_status_in_db,
)


REFUND_REQUESTED_TOPIC = "refund.requested"


def publish_message(topic: str, payload: dict) -> dict:
    """发布业务消息。本地用 SQLite 模拟 MQ，生产可替换 RabbitMQ/Kafka。"""

    return enqueue_mq_message_to_db(topic=topic, payload=payload)


def consume_messages(topic: str | None = None, limit: int = 10) -> list[dict]:
    """领取待处理消息。"""

    return claim_mq_messages_from_db(topic=topic, limit=limit)


def ack_message(message_id: str, result: dict | None = None) -> dict | None:
    """确认消息处理成功。"""

    return update_mq_message_status_in_db(message_id, "done", result=result)


def fail_message(message_id: str, result: dict | None = None) -> dict | None:
    """标记消息处理失败。"""

    return update_mq_message_status_in_db(message_id, "failed", result=result)


def list_messages(limit: int = 50) -> list[dict]:
    """查看最近的 MQ 消息。"""

    return list_mq_messages_from_db(limit=limit)
