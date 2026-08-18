from uuid import uuid4

from app.storage.cache import delete_cache, get_json_cache, set_json_cache
from app.storage.database import (
    append_message_to_db,
    clear_pending_task_in_db,
    get_pending_task_from_db,
    load_messages_from_db,
    set_pending_task_in_db,
)


def history_cache_key(conversation_id: str) -> str:
    return f"conversation_history:{conversation_id}"


def pending_task_cache_key(conversation_id: str) -> str:
    return f"pending_task:{conversation_id}"


class ConversationMemory:
    """会话管理器，负责持久化最近几轮消息和待补全任务。"""

    def __init__(self, max_messages: int = 8) -> None:
        self.max_messages = max_messages

    # 如果前端带了 conversation_id，就继续原会话；否则创建新会话。
    def ensure_id(self, conversation_id: str | None) -> str:
        if conversation_id:
            return conversation_id

        return str(uuid4())

    # 读取某个会话的历史消息，用于恢复聊天窗口或构造大模型上下文。
    def load(self, conversation_id: str) -> list[dict]:
        cached = get_json_cache(history_cache_key(conversation_id))

        if cached is not None:
            return cached

        messages = load_messages_from_db(
            conversation_id=conversation_id,
            limit=self.max_messages,
        )
        set_json_cache(history_cache_key(conversation_id), messages)

        return messages

    # 追加一条消息。读取时只取最近 max_messages 条，避免上下文无限增长。
    def append(self, conversation_id: str, role: str, content: str) -> None:
        append_message_to_db(
            conversation_id=conversation_id,
            role=role,
            content=content,
        )
        delete_cache(history_cache_key(conversation_id))

    # 保存待补全任务。典型场景：用户说“帮我改地址”，但没有提供订单号。
    def set_pending_task(self, conversation_id: str, task: dict) -> None:
        set_pending_task_in_db(conversation_id, task)
        set_json_cache(pending_task_cache_key(conversation_id), task)

    # 读取待补全任务，用于用户下一轮补充订单号后继续执行。
    def get_pending_task(self, conversation_id: str) -> dict | None:
        cached = get_json_cache(pending_task_cache_key(conversation_id))

        if cached is not None:
            return cached

        task = get_pending_task_from_db(conversation_id)

        if task is not None:
            set_json_cache(pending_task_cache_key(conversation_id), task)

        return task

    # 当任务已经拿到缺失信息并完成处理后，清除待补全任务。
    def clear_pending_task(self, conversation_id: str) -> None:
        clear_pending_task_in_db(conversation_id)
        delete_cache(pending_task_cache_key(conversation_id))
