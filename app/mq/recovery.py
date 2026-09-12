"""Database queue leasing and bounded recovery, on either storage backend."""
import json
from datetime import datetime, timedelta
import os

from app.storage.transactions import execute, transaction


def claim_messages(topic=None, limit=10):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    lease_seconds = max(1, int(os.getenv("MQ_LEASE_SECONDS", "120")))
    retry_seconds = max(1, int(os.getenv("MQ_RETRY_SECONDS", "30")))
    max_attempts = max(1, int(os.getenv("MQ_MAX_ATTEMPTS", "3")))
    now = datetime.now()
    lease_cutoff = (now - timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
    retry_cutoff = (now - timedelta(seconds=retry_seconds)).isoformat(timespec="seconds")
    with transaction() as state:
        # Exhausted crashed/failed messages remain visible for explicit repair.
        execute("UPDATE mq_messages SET status = 'dead_letter' WHERE attempts >= ? AND "
                "(status = 'failed' OR (status = 'processing' AND updated_at <= ?))",
                (max_attempts, lease_cutoff))
        clause = " AND topic = ?" if topic else ""
        parameters = [max_attempts, lease_cutoff, retry_cutoff]
        if topic:
            parameters.append(topic)
        parameters.append(limit)
        rows = execute("SELECT * FROM mq_messages WHERE attempts < ? AND "
                       "(status = 'pending' OR (status = 'processing' AND updated_at <= ?) "
                       "OR (status = 'failed' AND updated_at <= ?))" + clause +
                       " ORDER BY created_at, message_id LIMIT ?" + (" FOR UPDATE" if state.mysql else ""),
                       tuple(parameters), fetch=True)
        messages = []
        for row in rows:
            message = dict(row)
            message.update(status="processing", attempts=int(row["attempts"]) + 1,
                           updated_at=now.isoformat(timespec="seconds"))
            execute("UPDATE mq_messages SET status = ?, attempts = ?, updated_at = ? WHERE message_id = ?",
                    (message["status"], message["attempts"], message["updated_at"], message["message_id"]))
            for key in ("payload", "result"):
                if message.get(key) is not None and not isinstance(message[key], dict):
                    message[key] = json.loads(message[key])
            messages.append(message)
        return messages
