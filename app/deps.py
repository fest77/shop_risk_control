# -*- coding: utf-8 -*-
"""请求依赖：当前用户、权限校验、限流键（模块 01 §3.5 / BR-01-14）。

**从"请求头身份"到"JWT 身份"的切换在此完成**：阶段一用 `X-Operator` /
`X-Role` 承载身份，仅为在模块 01 之前验证 `CFG-4031`；现在身份一律来自
`Authorization: Bearer <jwt>`，由鉴权中间件解析并注入 `request.state.user`。
**这两个头不再被读取**——留着它们等于留了一个"自称任意角色"的后门。
"""
from __future__ import annotations

import hashlib
from typing import Callable

from fastapi import Request

from app.errors import AuthDependencyError, PermissionDeniedError
from app.logging import get_logger
from app.security.permissions import role_has

log = get_logger("shop_risk_control.deps")

# 说明：早期版本只对写操作留痕（照 BR-01-15 字面）。模块 12 的 BR-12-24 追加要求
# "越权访问审计接口本身也要记为 auth.denied"，且越权**读**同样是安全事件
# （有人在试探自己不该看的接口）。因此改为**所有被拒的访问都留痕**。
# 真正需要防的是"刷屏"，而刷屏来自**被允许**的高频读——那些依然不记。


def get_current_user(request: Request) -> dict:
    """取当前登录用户（鉴权中间件注入的库中用户文档）。

    取不到说明该路径未经过鉴权中间件——那属于**配置错误**（接口暴露在白名单
    之外却没有认证），必须显式报错，而不是返回一个匿名身份继续执行。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise AuthDependencyError("请求上下文中缺少用户信息（该路径可能未接入鉴权中间件）")
    return user


def current_user_name(request: Request) -> str:
    """当前账号名（`sys_users._id`）。"""
    user = get_current_user(request)
    return str(user.get("_id") or user.get("username") or "unknown")


def require_permission(permission: str) -> Callable:
    """构造"需要某权限"的依赖（BR-01-14：后端必须独立校验）。

    前端不渲染按钮只是体验优化；真正的拦截在这里——即使有人手工构造请求，
    也会得到 `AUTH-4020`；越权**写**操作还会按 BR-01-15 留痕。
    """

    async def _dependency(request: Request) -> dict:
        user = get_current_user(request)
        role = str(user.get("role", ""))
        if not role_has(role, permission):
            # 留痕与拒绝**都要做**：只拒绝不留痕会让越权尝试无迹可查（BR-01-15 / BR-12-24）
            await _record_denied(request, user, permission)
            raise PermissionDeniedError(permission, role)
        return user

    # 依赖名会出现在 OpenAPI 里，命名清晰便于逐个核对"这个接口要什么权限"
    _dependency.__name__ = f"require_{permission.replace(':', '_')}"
    return _dependency


async def _record_denied(request: Request, user: dict, permission: str) -> None:
    """BR-01-15 / BR-12-19 / BR-12-24：越权访问留痕（`action=auth.denied`）。

    `strict=False`（BR-12-17）：留痕失败不能把"你没权限"变成"服务器错误"；
    但 `target_type`/`target_id` 必须带上（BR-12-19：不得省略），
    否则这条记录无法回答"他到底想干什么"。
    """
    from app.services import audit_service

    await audit_service.audit(
        actor=str(user.get("_id") or user.get("username") or "unknown"),
        actor_role=str(user.get("role", "")),
        action="auth.denied",
        target_type="permission",
        target_id=permission,
        after={"method": request.method, "path": request.url.path},
        ip=client_ip(request),
        ua=request.headers.get("user-agent"),
        strict=False,
    )


def client_ip(request: Request) -> str:
    """取客户端 IP（优先 `X-Forwarded-For`，便于将来放在反向代理之后）。"""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def current_identity_key(request: Request) -> str:
    """限流用的身份键（模块 00 的 `COM-4290`）。

    优先按**令牌指纹**计数（同一账号在多标签页操作共享一个配额）。这里不直接
    用令牌原文：限流键会出现在日志里，把令牌写进日志等于把凭据写进日志。
    未登录请求退化为按客户端 IP 计数——匿名请求同样需要限流。
    """
    auth = (request.headers.get("Authorization") or "").strip()
    if auth:
        return f"tk:{hashlib.sha256(auth.encode('utf-8')).hexdigest()[:16]}"
    return f"ip:{client_ip(request)}"
