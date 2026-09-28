# -*- coding: utf-8 -*-
"""登录与鉴权接口（模块 01 §3.1 ~ §3.4）。路径相对 `API_PREFIX`。

    POST /auth/login     账号密码登录（白名单内，无需令牌）
    GET  /auth/me        当前用户 + 权限串（供前端渲染菜单）
    POST /auth/logout    登出（无状态 JWT：服务端只确认，前端清 token）
    POST /auth/password  本人改密（成功后旧令牌立即失效）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app import config
from app.api import register_router
from app.db import get_db
from app.deps import client_ip, current_user_name, get_current_user
from app.errors import AppError, envelope
from app.logging import get_logger, get_trace_id
from app.repos.user_repo import UserRepo
from app.schemas.auth_schema import ChangePasswordRequest, LoginRequest
from app.security.login_guard import LoginGuard
from app.services import audit_service
from app.services.auth_service import AuthService

log = get_logger("shop_risk_control.auth")

router = APIRouter(tags=["登录与鉴权"])

# 登录失败锁定器（进程内单例）。导出给测试重置用。
LOGIN_GUARD = LoginGuard(config.LOGIN_MAX_FAILURES, config.LOGIN_LOCK_SECONDS)


def get_auth_service() -> AuthService:
    return AuthService(UserRepo(get_db()), LOGIN_GUARD)


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


@router.post("/auth/login", summary="登录")
async def login(
    request: Request,
    payload: LoginRequest,
    service: AuthService = Depends(get_auth_service),
):
    """账号密码登录。

    失败一律 `AUTH-4001`「账号或密码错误」：不区分"账号不存在"与"密码错误"
    （BR-01-02），且响应耗时也已对齐（见 `security.password.dummy_verify`）。

    **登录事件按要求记审计（BR-12-17，`strict=False`）**：成功与失败都记。
    Spec 的动作清单只列了 `auth.login`，这里用同一个动作名 + `after.result`
    区分成败，而不是新造一个 `auth.login_failed`——保持动作清单不膨胀，
    同时让"有人在爆破这个账号"这件事在审计里看得见。
    """
    try:
        data = await service.login(payload.username, payload.password)
    except AppError as e:
        await audit_service.audit(
            actor=payload.username, actor_role="", action="auth.login",
            target_type="user", target_id=payload.username,
            after={"result": "failed", "code": e.code},
            ip=client_ip(request), ua=request.headers.get("user-agent"),
            strict=False,
        )
        raise
    await audit_service.audit(
        actor=payload.username, actor_role=data["user"]["role"], action="auth.login",
        target_type="user", target_id=payload.username,
        after={"result": "success"},
        ip=client_ip(request), ua=request.headers.get("user-agent"),
        strict=False,
    )
    return envelope("OK", "登录成功", _trace(request), data)


@router.get("/auth/me", summary="当前用户与权限")
async def me(request: Request):
    """返回当前用户、角色标签与**权限串**（§3.2）。

    前端据此渲染菜单与按钮，**不自行推导权限**（BR-01-12），
    因此权限矩阵改动只需改后端一处。
    """
    user = get_current_user(request)
    payload = request.state.token_payload
    return envelope("OK", "ok", _trace(request), AuthService.me_payload(user, payload))


@router.post("/auth/logout", summary="登出")
async def logout(request: Request):
    """登出（§3.3）。

    JWT 无状态，服务端**不做真正的失效**——只确认并让前端清 token。
    这里明确记录登出日志，便于审计时区分"主动登出"与"令牌被冒用"。
    """
    username = current_user_name(request)
    role = str(get_current_user(request).get("role", ""))
    log.info("用户登出 user=%s", username)
    await audit_service.audit(
        actor=username, actor_role=role, action="auth.logout",
        target_type="user", target_id=username,
        ip=client_ip(request), ua=request.headers.get("user-agent"),
        strict=False,
    )
    return envelope("OK", "已登出", _trace(request), {"logged_out": True, "username": username})


@router.post("/auth/password", summary="修改本人密码")
async def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    service: AuthService = Depends(get_auth_service),
):
    """仅本人可改自己的密码（§3.4）；成功后该用户所有旧令牌立即失效。"""
    username = current_user_name(request)
    data = await service.change_password(username, payload.old_password, payload.new_password)
    return envelope("OK", "密码已修改，请重新登录", _trace(request), data)


register_router("01", router)
