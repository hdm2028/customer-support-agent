from app.storage.database import (
    claim_mq_messages_from_db,
    enqueue_mq_message_to_db,
    list_mq_messages_from_db,
    update_mq_message_status_in_db,
)


REFUND_CREATED_TOPIC = "refund.created"


def publish_message(topic: str, payload: dict) -> dict:
    return enqueue_mq_message_to_db(topic=topic, payload=payload)


def consume_messages(topic: str | None = None, limit: int = 10) -> list[dict]:
    return claim_mq_messages_from_db(topic=topic, limit=limit)


def ack_message(message_id: str, result: dict | None = None) -> dict | None:
    return update_mq_message_status_in_db(message_id, "done", result=result)


def fail_message(message_id: str, result: dict | None = None, *, attempts: int | None = None) -> dict | None:
    from app.storage.transactions import lock_record, transaction
    with transaction():
        message = lock_record("mq_messages", "message_id", message_id)
        if not message or message["status"] in {"done", "dead_letter"}:
            return None
        if attempts is not None and message["attempts"] != attempts:
            return None
        return update_mq_message_status_in_db(message_id, "failed", result=result)


def list_messages(limit: int = 50) -> list[dict]:
    return list_mq_messages_from_db(limit=limit)
