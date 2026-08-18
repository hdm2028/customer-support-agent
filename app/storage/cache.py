import json
import threading
import time
from typing import Any

from app.core.config import get_settings


class InMemoryTTLCache:
    """本地开发用 TTL 缓存，提供和 Redis 接近的 JSON 读写接口。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._data.get(key)

            if not item:
                return None

            expires_at, value = item
            if expires_at is not None and expires_at < time.time():
                self._data.pop(key, None)
                return None

            return value

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        with self._lock:
            expires_at = time.time() + ex if ex else None
            self._data[key] = (expires_at, value)

    def set_if_absent(self, key: str, value: str, ex: int | None = None) -> bool:
        with self._lock:
            if self.get(key) is not None:
                return False

            self.set(key, value, ex=ex)
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def compare_and_delete(self, key: str, expected_value: str) -> bool:
        with self._lock:
            if self.get(key) != expected_value:
                return False

            self.delete(key)
            return True

    def ping(self) -> bool:
        return True


class RedisJsonCache:
    """Redis JSON 缓存封装。redis 包不存在或 Redis 不可用时不会影响主流程。"""

    def __init__(self, redis_url: str) -> None:
        import redis

        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.client.ping()

    def get(self, key: str) -> str | None:
        return self.client.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.client.set(key, value, ex=ex)

    def set_if_absent(self, key: str, value: str, ex: int | None = None) -> bool:
        return bool(self.client.set(key, value, ex=ex, nx=True))

    def delete(self, key: str) -> None:
        self.client.delete(key)

    def compare_and_delete(self, key: str, expected_value: str) -> bool:
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        end
        return 0
        """
        return bool(self.client.eval(script, 1, key, expected_value))

    def ping(self) -> bool:
        return bool(self.client.ping())


_CACHE_BACKEND = None
_CACHE_BACKEND_ERROR = None


def get_cache_backend():
    """优先使用 Redis；缺配置、缺依赖或连接失败时降级到内存缓存。"""

    global _CACHE_BACKEND, _CACHE_BACKEND_ERROR

    if _CACHE_BACKEND is not None:
        return _CACHE_BACKEND

    settings = get_settings()

    if settings.redis_url:
        try:
            _CACHE_BACKEND = RedisJsonCache(settings.redis_url)
            _CACHE_BACKEND_ERROR = None
            return _CACHE_BACKEND
        except Exception as error:
            _CACHE_BACKEND_ERROR = {
                "error_type": type(error).__name__,
                "error_message": str(error),
            }

    _CACHE_BACKEND = InMemoryTTLCache()
    return _CACHE_BACKEND


def cache_backend_name() -> str:
    backend = get_cache_backend()

    if isinstance(backend, RedisJsonCache):
        return "redis"

    return "memory"


def cache_health() -> dict:
    settings = get_settings()
    backend = get_cache_backend()
    reachable = False

    try:
        reachable = bool(backend.ping())
    except Exception:
        reachable = False

    return {
        "backend": cache_backend_name(),
        "redis_configured": bool(settings.redis_url),
        "reachable": reachable,
        "fallback_reason": _CACHE_BACKEND_ERROR,
    }


def get_json_cache(key: str) -> Any | None:
    raw_value = get_cache_backend().get(key)

    if raw_value is None:
        return None

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


def set_json_cache(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_seconds
    get_cache_backend().set(
        key,
        json.dumps(value, ensure_ascii=False),
        ex=ttl,
    )


def delete_cache(key: str) -> None:
    get_cache_backend().delete(key)


def cache_agent_state(
    conversation_id: str,
    current_node: str,
    status: str,
    payload: dict | None = None,
) -> dict:
    """保存 Agent 当前节点、工具状态和会话状态，方便前端或排障读取。"""

    state = {
        "conversation_id": conversation_id,
        "current_node": current_node,
        "status": status,
        "payload": payload or {},
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    set_json_cache(
        f"agent_state:{conversation_id}",
        state,
        ttl_seconds=get_settings().agent_state_ttl_seconds,
    )

    return state


def get_agent_state(conversation_id: str) -> dict | None:
    return get_json_cache(f"agent_state:{conversation_id}")
