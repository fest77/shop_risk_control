# -*- coding: utf-8 -*-
"""系统设置 HTTP 入口（模块 13 §3.1 ~ §3.5）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET  | `/system/config` | `sys:config` | 运行参数（§3.1） |
| PUT  | `/system/config` | `sys:config` | 保存运行参数，返回生效方式（§3.2） |
| GET  | `/system/stats` | `sys:config` | 吞吐与组件健康度（§3.3） |
| GET  | `/system/users` | `account:manage` | 账号列表（§3.4） |
| POST | `/system/users` | `account:manage` | 新增账号（口令仅返回一次） |
| GET  | `/system/users/{username}` | `account:manage` | 单个账号（供页面刷新单行） |
| PUT  | `/system/users/{username}` | `account:manage` | 改姓名 / 改角色 |
| POST | `/system/users/{username}/disable` | `account:manage` | 停用（可带 `transfer_to`） |
| POST | `/system/users/{username}/enable` | `account:manage` | 启用 |
| POST | `/system/users/{username}/reset-password` | `account:manage` | 重置密码（旧令牌立即失效） |
| DELETE | `/system/users/{username}` | `account:manage` | 删除（软删除，需先停用） |
| GET  | `/system/engine-config` | `engine:config` | 决策引擎配置（含模型引擎预留位） |
| PUT  | `/system/engine-config` | `engine:config` | 当前仅接受 `engine_type=rule` |

## 权限只用矩阵里现有的三个码（BR-01-12 的唯一真源）

`sys:config`（运行参数 + 吞吐健康度，仅 admin）、`account:manage`（账号与角色）、
`engine:config`（决策引擎）。**没有新增权限串**——新增权限要动模块 01 的权限
矩阵与职责分离校验（`assert_matrix_is_sane`），属跨模块改动；而这三个码本来就是
矩阵为"系统设置"这一页预留的（`app/security/permissions.py` 的注释写得很明确）。

## 为什么 `current_user` 既进服务层、又由后端独立校验

服务层需要它来判 `SYS-4005`（不能停用/删除自己）与给列表项打 `is_self` 标记；
而**真正的拦截在 `require_permission`**（BR-01-14：前端不渲染按钮只是体验优化）。
`is_self` 只用于页面禁用按钮，后端**从不**依赖它做安全判断。

## 「恢复默认」（BR-13-09）

Spec §3 没有为它定义独立端点：它就是一次**把所有字段写回默认值的普通保存**
（`PUT /system/config` + `GET` 回来的 `defaults`）。因此这里不新增
`/system/config/reset`——多一个端点就多一处"两条路写同一份配置"的漂移风险，
而"二次确认"是前端行为（§2.1 的按钮语义），后端只需要保证"保存什么就是什么"。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api import register_router
from app.db import get_db
from app.deps import client_ip, current_user_name, require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.schemas.system_schema import (
    EngineConfigIn,
    EngineConfigOut,
    ResetPasswordIn,
    RuntimeConfigOut,
    RuntimeConfigSaveOut,
    RuntimeConfigUpdate,
    SystemStatsOut,
    UserActionIn,
    UserCreateIn,
    UserCreateOut,
    UserListOut,
    UserOut,
    UserPasswordResetOut,
    UserUpdateIn,
)
from app.security.permissions import P_ACCOUNT_MANAGE, P_ENGINE_CONFIG, P_SYS_CONFIG
from app.services.config_service import (
    build_config_service,
    build_engine_config_service,
)
from app.services.health_service import build_health_service
from app.services.user_admin_service import build_user_admin_service

router = APIRouter(tags=["系统设置"])


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


def _actor(request: Request) -> tuple[str, str, Optional[str], Optional[str]]:
    """从请求里取审计四要素：`(operator, role, ip, ua)`。"""
    user = request.state.user
    return (
        current_user_name(request),
        str(user.get("role", "")),
        client_ip(request),
        request.headers.get("user-agent"),
    )


# ============================================================
# §3.1 / §3.2 运行参数
# ============================================================
@router.get("/system/config", summary="读取风控运行参数")
async def get_system_config(
    request: Request,
    _user: dict = Depends(require_permission(P_SYS_CONFIG)),
):
    """运行参数（§3.1）。未被保存过时返回代码默认值（`config_version=w1`）。

    同时下发每个参数的**取值域与默认值**：页面据此设输入框的 min/max 并实现
    「恢复默认」，不必在前端再抄一份数字（BR-00-07 的单一来源原则）。
    """
    data = await build_config_service(get_db()).get_config()
    return envelope("OK", "ok", _trace(request), RuntimeConfigOut.model_validate(data).model_dump())


@router.put("/system/config", summary="保存风控运行参数")
async def put_system_config(
    request: Request,
    payload: RuntimeConfigUpdate,
    _user: dict = Depends(require_permission(P_SYS_CONFIG)),
):
    """保存运行参数（§3.2）。

    **响应必须含 `applied` 与 `requires_restart`**（BR-13-02）：让前端能明确
    告知"哪些已生效、哪些要重启"，而不是让用户以为改了没生效。
    校验失败（`SYS-4001`/`SYS-4002`）时**一个参数都不会保存**（Spec §5 的原子性）。
    """
    patch = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    operator, role, ip, ua = _actor(request)
    data = await build_config_service(get_db()).save_config(
        patch, operator=operator, actor_role=role, ip=ip, ua=ua
    )
    if not data["changed"]:
        message = "参数没有变化，未写入"
    else:
        message = f"已保存：立即生效 {len(data['applied'])} 项"
        if data["requires_restart"]:
            message += f"，需重启 {len(data['requires_restart'])} 项"
    return envelope("OK", message, _trace(request),
                    RuntimeConfigSaveOut.model_validate(data).model_dump())


# ============================================================
# §3.3 吞吐与健康度
# ============================================================
@router.get("/system/stats", summary="吞吐与接口健康度")
async def get_system_stats(
    request: Request,
    _user: dict = Depends(require_permission(P_SYS_CONFIG)),
):
    """运维看板（§3.3 / BR-13-29）。

    与 `/health` 的分工：`/health` 是**存活探针**（轻量、无鉴权），本接口是
    **运维看板**（含统计、需 `sys:config`）。四个组件**并发探测且各自 ≤1s 超时**
    （BR-13-28），任一组件超时只影响它自己那一行（`SYS-5003`）。
    """
    data = await build_health_service().stats()
    return envelope("OK", "ok", _trace(request), SystemStatsOut.model_validate(data).model_dump())


# ============================================================
# §3.4 账号与角色管理
# ============================================================
def _user_service():
    return build_user_admin_service(get_db())


@router.get("/system/users", summary="账号列表（分页 + role/status 筛选）")
async def list_users(
    request: Request,
    role: Optional[str] = Query(None, description="reviewer / strategist / admin"),
    status: Optional[str] = Query(None, description="active / disabled / deleted"),
    page: int = Query(1),
    page_size: int = Query(20),
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """账号列表（§3.4）。**响应绝不包含 `password_hash`**（BR-13-20）。"""
    current = current_user_name(request)
    data = await _user_service().list_users(
        role=role, status=status, page=page, page_size=page_size, current_user=current
    )
    return envelope("OK", "ok", _trace(request), UserListOut.model_validate(data).model_dump())


@router.post("/system/users", summary="新增账号")
async def create_user(
    request: Request,
    payload: UserCreateIn,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """新增账号（§3.4）。未传 `initial_password` 时后端生成并**仅返回一次**。"""
    operator, role, ip, ua = _actor(request)
    data = await _user_service().create_user(
        payload, operator=operator, actor_role=role, ip=ip, ua=ua
    )
    message = (
        "账号已创建（初始密码仅在本次响应中返回，请立即转交本人）"
        if data.get("generated_password") else "账号已创建"
    )
    return envelope("OK", message, _trace(request),
                    UserCreateOut.model_validate(data).model_dump())


@router.get("/system/users/{username}", summary="账号详情")
async def get_user(
    request: Request,
    username: str,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """单个账号（供页面在改动后刷新单行；软删除的账号按"不存在"返回 `COM-4004`）。"""
    doc = await _user_service().get_user(username)
    data = UserOut.from_doc(doc, current_user=current_user_name(request))
    return envelope("OK", "ok", _trace(request), data.model_dump())


@router.put("/system/users/{username}", summary="修改姓名 / 角色")
async def update_user(
    request: Request,
    username: str,
    payload: UserUpdateIn,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """改姓名或角色（§3.4）。**账号名不可修改**（BR-13-11）。

    "把最后一个可用管理员降级"会得到 `SYS-4006`（BR-13-14）——护栏在服务层，
    与页面上是否灰掉那个下拉框无关。
    """
    operator, role, ip, ua = _actor(request)
    data = await _user_service().update_user(
        username, payload, operator=operator, actor_role=role, ip=ip, ua=ua
    )
    message = "账号已更新" if data.get("changed") else "没有变化，未写入"
    return envelope("OK", message, _trace(request),
                    {"changed": data["changed"], "user": data["user"].model_dump()})


@router.post("/system/users/{username}/disable", summary="停用账号")
async def disable_user(
    request: Request,
    username: str,
    payload: Optional[UserActionIn] = None,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """停用账号（§3.4 + BR-13-15）。

    请求体可选：`{"transfer_to": "reviewer02"}` 用于"名下有待处置案件"时先转交。
    不给 `transfer_to` 而有待办案件 → `SYS-4007` 并把案件清单回给页面；
    停用自己 → `SYS-4005`；停用最后一个可用管理员 → `SYS-4006`。
    """
    action = payload or UserActionIn()
    operator, role, ip, ua = _actor(request)
    data = await _user_service().disable_user(
        username, transfer_to=action.transfer_to, reason=action.reason,
        operator=operator, actor_role=role, ip=ip, ua=ua,
    )
    message = f"账号已停用（转交 {data['transferred_cases']} 个案件）" \
        if data.get("transferred_cases") else ("账号已停用" if data.get("changed") else "账号本就处于停用状态")
    return envelope("OK", message, _trace(request), {
        "changed": data["changed"], "username": data["username"], "status": data["status"],
        "transferred_cases": data["transferred_cases"], "user": data["user"].model_dump(),
    })


@router.post("/system/users/{username}/enable", summary="启用账号")
async def enable_user(
    request: Request,
    username: str,
    payload: Optional[UserActionIn] = None,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """启用账号（§3.4）。幂等：已是启用态时返回 `changed=false`，不写审计。"""
    action = payload or UserActionIn()
    operator, role, ip, ua = _actor(request)
    data = await _user_service().enable_user(
        username, reason=action.reason, operator=operator, actor_role=role, ip=ip, ua=ua
    )
    return envelope("OK", "账号已启用" if data.get("changed") else "账号本就处于启用状态",
                    _trace(request), {
                        "changed": data["changed"], "username": data["username"],
                        "status": data["status"], "user": data["user"].model_dump(),
                    })


@router.post("/system/users/{username}/reset-password", summary="重置密码")
async def reset_password(
    request: Request,
    username: str,
    payload: Optional[ResetPasswordIn] = None,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """重置密码（§3.4 + BR-13-16）。

    重置后该账号**所有令牌立即失效**（靠 `password_changed_at`，01 的
    `load_user_for_token` 据此判定），页面必须提示"需重新登录"。
    未传 `new_password` 时后端生成（≥12 位）并**仅在本次响应中返回**。
    """
    body = payload or ResetPasswordIn()
    operator, role, ip, ua = _actor(request)
    data = await _user_service().reset_password(
        username, new_password=body.new_password, reason=body.reason,
        operator=operator, actor_role=role, ip=ip, ua=ua,
    )
    return envelope("OK", "密码已重置，该账号需重新登录", _trace(request),
                    UserPasswordResetOut.model_validate(data).model_dump())


@router.delete("/system/users/{username}", summary="删除账号（软删除）")
async def delete_user(
    request: Request,
    username: str,
    payload: Optional[UserActionIn] = None,
    _user: dict = Depends(require_permission(P_ACCOUNT_MANAGE)),
):
    """删除账号（§3.4 + BR-13-17）。

    **软删除**（`status=deleted`）：文档保留以便审计追溯（"这个账号曾经存在过"
    是审计链可读性的前提）。仅 `status=disabled` 且无待处置案件的账号可删；
    删除自己 → `SYS-4005`。
    """
    action = payload or UserActionIn()
    operator, role, ip, ua = _actor(request)
    data = await _user_service().delete_user(
        username, transfer_to=action.transfer_to, reason=action.reason,
        operator=operator, actor_role=role, ip=ip, ua=ua,
    )
    return envelope("OK", "账号已删除（软删除，保留审计可追溯）", _trace(request), {
        "changed": data["changed"], "username": data["username"], "status": data["status"],
        "soft_deleted": data["soft_deleted"], "transferred_cases": data["transferred_cases"],
        "user": data["user"].model_dump(),
    })


# ============================================================
# §3.5 决策引擎配置（模型引擎为预留）
# ============================================================
@router.get("/system/engine-config", summary="读取决策引擎配置")
async def get_engine_config(
    request: Request,
    _user: dict = Depends(require_permission(P_ENGINE_CONFIG)),
):
    """决策引擎配置（§3.5）。`model_available` **恒为 false**（AD-09/G-01），
    `note` 是给页面橙色警示条用的原文（BR-13-23）。
    """
    data = await build_engine_config_service(get_db()).get_config()
    return envelope("OK", "ok", _trace(request), EngineConfigOut.model_validate(data).model_dump())


@router.put("/system/engine-config", summary="保存决策引擎配置")
async def put_engine_config(
    request: Request,
    payload: EngineConfigIn,
    _user: dict = Depends(require_permission(P_ENGINE_CONFIG)),
):
    """保存决策引擎配置（§3.5）。

    **当前仅接受 `engine_type=rule`**（BR-13-22）：`model`/`hybrid` 一律
    `SYS-4003`「模型引擎尚未实现（G-01）」并**不改配置**——AD-09 的模型引擎是
    空实现，假装成功会让评审以为系统真的支持模型决策。
    """
    operator, role, ip, ua = _actor(request)
    data = await build_engine_config_service(get_db()).save_config(
        payload, operator=operator, actor_role=role, ip=ip, ua=ua
    )
    return envelope("OK", "决策引擎配置已保存" if data.get("changed") else "没有变化，未写入",
                    _trace(request), {
                        "changed": data["changed"],
                        "config": EngineConfigOut.model_validate(data["config"]).model_dump(),
                    })


# 模块编号 "13"（`00_模块划分与边界` §6），由注册器统一加 `/api/v1` 前缀
register_router("13", router)

__all__ = ["router"]
