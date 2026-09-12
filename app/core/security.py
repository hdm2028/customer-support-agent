"""API identity boundary. Internal CLI jobs run without an HTTP principal."""
from contextvars import ContextVar
from dataclasses import dataclass
import hmac
import json
import os
from uuid import uuid4

from starlette.responses import JSONResponse


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str


current_principal = ContextVar("request_principal", default=None)
ADMIN_PATHS = ("/manual-reviews", "/mq/", "/refund-tasks/", "/observability/", "/cache/",
               "/knowledge/catalog", "/knowledge/chunks", "/tools", "/admin/")


def _configured_identities():
    configured = os.getenv("AUTH_TOKENS", "")
    if not configured:
        raise RuntimeError("服务尚未配置访问凭据")
    try:
        identities = json.loads(configured)
        if not isinstance(identities, dict):
            raise ValueError()
        for token, identity in identities.items():
            if (len(token) < 32 or not isinstance(identity, dict)
                    or identity.get("role") not in {"customer", "admin"}
                    or not isinstance(identity.get("user_id"), str)
                    or not 1 <= len(identity["user_id"]) <= 64):
                raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError("服务访问凭据配置无效") from None
    return identities


def configured_operators():
    """Expose assignable identities, never their access tokens."""
    return sorted({identity["user_id"] for identity in _configured_identities().values() if identity["role"] == "admin"})


def authenticate(authorization: str) -> Principal:
    identities = _configured_identities()
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise PermissionError("请提供访问凭据")
    for token, identity in identities.items():
        if hmac.compare_digest(token.encode(), supplied.encode()):
            return Principal(identity["user_id"], identity["role"])
    raise PermissionError("访问凭据无效")


class IdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {"/", "/health", "/live", "/ready", "/favicon.ico"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            principal = authenticate(headers.get(b"authorization", b"").decode("latin-1"))
        except PermissionError as error:
            return await JSONResponse({"detail": str(error)}, status_code=401)(scope, receive, send)
        except RuntimeError as error:
            return await JSONResponse({"detail": str(error)}, status_code=503)(scope, receive, send)
        if scope["path"].startswith(ADMIN_PATHS) and principal.role != "admin":
            return await JSONResponse({"detail": "需要管理员权限"}, status_code=403)(scope, receive, send)
        token = current_principal.set(principal)
        try:
            return await self.app(scope, receive, send)
        finally:
            current_principal.reset(token)


def authorize_order(order):
    principal = current_principal.get()
    if principal is not None and principal.role != "admin":
        if order is None or order.get("user_id") != principal.user_id:
            raise PermissionError("订单不存在或不可访问")
    return order


def conversation_access(conversation_id: str | None, *, create=False) -> str:
    from app.storage.transactions import execute, transaction
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("缺少请求身份")
    if conversation_id is None and create:
        conversation_id = str(uuid4())
        with transaction():
            execute("INSERT INTO conversation_owners (conversation_id, user_id) VALUES (?, ?)",
                    (conversation_id, principal.user_id))
        return conversation_id
    if not conversation_id or len(conversation_id) > 64:
        raise PermissionError("会话不存在或不可访问")
    with transaction():
        rows = execute("SELECT user_id FROM conversation_owners WHERE conversation_id = ?", (conversation_id,), fetch=True)
    if principal.role == "admin" or (rows and rows[0]["user_id"] == principal.user_id):
        return conversation_id
    # Unowned legacy conversations cannot be claimed by guessing their ID.
    raise PermissionError("会话不存在或不可访问")


def list_visible_records(table, limit):
    from app.storage.transactions import execute, transaction
    if table not in {"tickets", "refund_requests", "manual_reviews"}:
        raise ValueError("Unsupported record type")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("缺少请求身份")
    clause = "" if principal.role == "admin" else " WHERE user_id = ?"
    parameters = (limit,) if principal.role == "admin" else (principal.user_id, limit)
    with transaction():
        rows = execute(f"SELECT payload FROM {table}{clause} ORDER BY created_at DESC LIMIT ?", parameters, fetch=True)
    return [row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]) for row in rows]
