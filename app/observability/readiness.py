"""Readiness probes report configured dependency failures without credentials."""
from app.core.config import get_settings
from app.core.security import authenticate
from app.rag.index_manager import get_rag_index_manager
from app.storage.cache import cache_health
from app.storage.database import database_health


def readiness_report():
    settings = get_settings()
    checks = {}
    try:
        checks["database"] = bool(database_health().get("reachable"))
    except Exception:
        checks["database"] = False
    try:
        cache = cache_health()
        checks["cache"] = bool(cache.get("reachable")) and (not settings.redis_url or cache.get("backend") == "redis")
    except Exception:
        checks["cache"] = False
    index = get_rag_index_manager().get_active_index()
    checks["knowledge_index"] = bool(index is not None and index.chunks)
    try:
        authenticate("")  # Validate server configuration without accepting an identity.
    except PermissionError:
        checks["identity_configuration"] = True
    except RuntimeError:
        checks["identity_configuration"] = False
    checks["llm_configuration"] = settings.has_llm_key
    return {"success": all(checks.values()), "checks": checks,
            "database_backend": settings.database_backend,
            "cache": {"backend": cache.get("backend", "unavailable") if checks["cache"] else "unavailable"},
            "mq_backend": settings.mq_backend, "rag_embedding_provider": settings.rag_embedding_provider,
            "remote_llm_probed": False, "payment_channel_probed": False}
