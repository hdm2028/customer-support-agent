from uuid import uuid4
from datetime import datetime
import json
import os

from app.storage.database import (
    append_message_to_db,
    clear_pending_task_in_db,
    load_messages_from_db,
    set_pending_task_in_db,
)
from app.storage.transactions import execute, transaction


def history_cache_key(conversation_id: str) -> str:
    return f"conversation_history:{conversation_id}"


def pending_task_cache_key(conversation_id: str) -> str:
    return f"pending_task:{conversation_id}"


class ConversationMemory:
    """会话管理器，负责持久化最近几轮消息和待补全任务。"""

    def __init__(self, max_messages: int = 8, max_context_chars: int | None = None,
                 max_age_seconds: int | None = None) -> None:
        if max_context_chars is None:
            max_context_chars = int(os.getenv('MEMORY_CONTEXT_MAX_CHARS', '8000'))
        if max_age_seconds is None:
            max_age_seconds = int(os.getenv('MEMORY_CONTEXT_TTL_SECONDS', '1800'))
        if max_messages < 1 or max_context_chars < 1 or max_age_seconds < 1:
            raise ValueError("Memory limits must be positive")
        self.max_messages = max_messages
        self.max_context_chars = max_context_chars
        self.max_age_seconds = max_age_seconds

    def _is_recent(self, timestamp: str) -> bool:
        # Existing repositories write application-local ISO timestamps. Accept
        # aware timestamps too; expiry never extends just because it was read.
        try:
            saved = datetime.fromisoformat(str(timestamp))
        except (TypeError, ValueError):
            return False
        return (datetime.now(tz=saved.tzinfo) - saved).total_seconds() < self.max_age_seconds

    # 如果前端带了 conversation_id，就继续原会话；否则创建新会话。
    def ensure_id(self, conversation_id: str | None) -> str:
        if conversation_id:
            return conversation_id

        return str(uuid4())

    # 读取某个会话的历史消息，用于恢复聊天窗口或构造大模型上下文。
    def load(self, conversation_id: str) -> list[dict]:
        # Eight indexed rows are cheap. Cross-process cache invalidation cannot
        # guarantee freshness, so executable context always comes from the DB.
        return load_messages_from_db(
            conversation_id=conversation_id,
            limit=self.max_messages,
        )

    def load_context(self, conversation_id: str) -> list[dict]:
        """Keep a contiguous suffix of whole turns without rewriting evidence.

        This is a character bound, not measured tokenizer usage. Full messages
        remain in the database and the history endpoint is unaffected.
        """
        with transaction():
            rows = execute('SELECT role, content, created_at FROM conversation_messages '
                           'WHERE conversation_id = ? ORDER BY id DESC LIMIT ?',
                           (conversation_id, self.max_messages), fetch=True)
        recent = []
        for row in rows:
            if not self._is_recent(row['created_at']):
                break
            recent.append({'role': row['role'], 'content': row['content']})
        turns: list[list[dict]] = []
        for message in reversed(recent):
            if message['role'] == 'user':
                turns.append([message])
            elif turns:
                turns[-1].append(message)
        selected = []
        size = 0
        for turn in reversed(turns):
            turn_size = sum(len(m['content']) for m in turn)
            if size + turn_size > self.max_context_chars:
                break
            selected.insert(0, turn)
            size += turn_size
        return [message for turn in selected for message in turn]

    # Single-message compatibility API; workflow uses atomic append_turn below.
    def append(self, conversation_id: str, role: str, content: str) -> None:
        append_message_to_db(
            conversation_id=conversation_id,
            role=role,
            content=content,
        )

    def append_turn(self, conversation_id: str, user_message: str, reply: str, *, turn_id: str | None = None) -> None:
        """Both messages commit or neither does, on SQLite and MySQL."""
        with transaction() as tx:
            if turn_id:
                # Claim before writing. The unique key serializes concurrent
                # retries, and the claim commits atomically with both messages.
                sql = ("INSERT INTO agent_persisted_turns (turn_id, conversation_id, user_message, reply) "
                       "VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE turn_id=turn_id") if tx.mysql else (
                       "INSERT INTO agent_persisted_turns (turn_id, conversation_id, user_message, reply) "
                       "VALUES (?, ?, ?, ?) ON CONFLICT(turn_id) DO NOTHING")
                inserted = execute(sql, (turn_id, conversation_id, user_message, reply))
                if inserted == 0:
                    row = execute("SELECT conversation_id, user_message, reply FROM agent_persisted_turns WHERE turn_id = ?",
                                  (turn_id,), fetch=True)[0]
                    if (row["conversation_id"], row["user_message"], row["reply"]) != (conversation_id, user_message, reply):
                        raise ValueError("Persisted turn conflicts with recovery input")
                    return
            append_message_to_db(conversation_id, 'user', user_message)
            append_message_to_db(conversation_id, 'assistant', reply)

    # 保存待补全任务。典型场景：用户说“帮我改地址”，但没有提供订单号。
    def set_pending_task(self, conversation_id: str, task: dict) -> None:
        set_pending_task_in_db(conversation_id, task)

    # 读取待补全任务，用于用户下一轮补充订单号后继续执行。
    def get_pending_task(self, conversation_id: str) -> dict | None:
        with transaction():
            rows = execute('SELECT task_json, updated_at FROM pending_tasks WHERE conversation_id = ?',
                           (conversation_id,), fetch=True)
        if not rows or not self._is_recent(rows[0]['updated_at']):
            # Do not delete here: a concurrent request may already have saved a
            # new task. Archival records are not executable conversation memory.
            return None
        task = rows[0]['task_json']
        return task if isinstance(task, dict) else json.loads(task)

    # 当任务已经拿到缺失信息并完成处理后，清除待补全任务。
    def clear_pending_task(self, conversation_id: str) -> None:
        clear_pending_task_in_db(conversation_id)
