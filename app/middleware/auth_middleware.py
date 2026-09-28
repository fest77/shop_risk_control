# -*- coding: utf-8 -*-
"""鉴权中间件（模块 01 §3.5，BR-01-14）。

**职责划分（有意如此）**
- 本中间件只做**认证**：白名单判定、令牌校验、回源查用户、把用户放进请求上下文。
  它实现的是"**默认拒绝**"——白名单之外的所有路径都必须带有效令牌，
  因此**将来新增的接口天然是受保护的**，不依赖作者记得加依赖项。
- **授权**（角色能不能做这件事）放在各接口的依赖里（`app/deps.py`），
  因为"这个接口需要哪个权限"只有接口自己知道。中间件在路由解析之前执行，
  `scope["route"]` 尚不存在，靠路径前缀猜权限表必然出错。

**为什么用纯 ASGI 中间件而不是 `BaseHTTPMiddleware`**：模块 02 要用 SSE 推送，
而 `BaseHTTPMiddleware` 会包装响应、按块缓冲，长连接与流式响应容易被破坏。
纯 ASGI 中间件直接透传 `receive/send`，不对响应体做任何加工。
"""
from __future__ import annotations

import urllib.parse

from starlette.types import ASGIApp, Receive, Scope, Send

from app import config, db
from app.errors import AppError, auth_error, envelope
from app.logging import get_logger, get_trace_id
from app.security.jwt import TokenError, bearer_token, decode_access_token
from app.security.login_guard import LoginGuard
from app.services.auth_service import AuthService
from app.utils.ids import new_trace_id

log = get_logger("shop_risk_control.auth.middleware")

# 精确匹配的白名单（§3.5）。`/docs` 等文档路径一并放行：演示时需要用 Swagger
# 逐个接口验收，把它们挡在门外会让验收无从下手（它们也不含任何业务数据）。
WHITELIST_EXACT: frozenset[str] = frozenset({
    "/", "/health", "/favicon.ico", "/docs", "/redoc", "/openapi.json",
    "/api/v1/auth/login",
})
# 前缀匹配的白名单
WHITELIST_PREFIX: tuple[str, ...] = ("/static/", "/api/v1/common/")


def is_whitelisted(path: str) -> bool:
    """是否免鉴权。"""
    if path in WHITELIST_EXACT:
        return True
    return any(path.startswith(p) for p in WHITELIST_PREFIX)


# 允许用查询参数 `?token=` 传令牌的路径（**仅 SSE**）。
# 理由：浏览器原生 `EventSource` 无法设置请求头，只能把令牌放 URL。
# 代价是令牌会进入访问日志与浏览器历史（已登记为 N-11-10），因此**只对该端点放开**，
# 其余接口一律只认 `Authorization` 头。
QUERY_TOKEN_PATHS: frozenset[str] = frozenset({"/api/v1/metrics/stream"})


def query_token(scope: Scope) -> str:
    """从查询串里取 `token`（只对 QUERY_TOKEN_PATHS 生效）。"""
    raw = scope.get("query_string", b"") or b""
    try:
        params = urllib.parse.parse_qs(raw.decode("latin-1"))
    except Exception:  # noqa: BLE001 - 查询串异常不该让鉴权中间件崩掉
        return ""
    values = params.get("token") or []
    return values[0].strip() if values else ""


class AuthMiddleware:
    """纯 ASGI 鉴权中间件。"""

    def __init__(self, app: ASGIApp, *, guard: LoginGuard | None = None):
        self.app = app
        # 登录锁定器只被登录接口使用；这里持有它只是为了统一装配，
        # 中间件本身不用（认证失败不该计入"登录失败"）
        self.guard = guard or LoginGuard(config.LOGIN_MAX_FAILURES, config.LOGIN_LOCK_SECONDS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # websocket / lifespan 直接透传
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if is_whitelisted(path):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        try:
            try:
                token = bearer_token(headers.get("authorization"))
            except TokenError:
                # SSE 的兜底通道：仅此一个路径允许 ?token=
                if path not in QUERY_TOKEN_PATHS:
                    raise
                token = query_token(scope)
                if not token:
                    raise
                log.warning("SSE 使用查询参数传令牌（会进访问日志）path=%s", path)
            payload = decode_access_token(token)
            service = AuthService(repo=_user_repo(), guard=self.guard)
            user = await service.load_user_for_token(payload)
        except TokenError as e:
            if e.code == "AUTH-4003":
                # 签名无效通常意味着有人手工改过 token：按安全事件记 WARNING
                log.warning("令牌签名无效 path=%s（疑似篡改尝试）", path)
            await self._reject(scope, receive, send, auth_error(e.code, e.message))
            return
        except AppError as e:
            await self._reject(scope, receive, send, e)
            return

        # 注入请求上下文，供 deps.get_current_user() 取用（Starlette 的
        # `request.state` 会读 `scope["state"]`，故必须先确保该键存在）
        scope.setdefault("state", {})
        scope["state"]["user"] = user
        scope["state"]["token_payload"] = payload
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, error: AppError) -> None:
        """返回统一响应包的失败响应（与异常处理器保持完全一致的形状）。"""
        trace_id = scope.get("state", {}).get("trace_id") or get_trace_id() or new_trace_id()
        body = envelope(error.code, error.message, trace_id, error.data)
        import json

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": error.http_status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode()),
                (b"x-trace-id", trace_id.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


def _user_repo():
    """延迟取用仓储，避免在 import 期就绑定数据库。"""
    from app.repos.user_repo import UserRepo

    return UserRepo(db.get_db())
