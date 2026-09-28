# -*- coding: utf-8 -*-
"""案件处置 HTTP 入口（模块 08）。路径为**相对 `API_PREFIX`**，前缀由
`app/api/__init__.py` 的注册器统一添加（模块 00 §6 约定）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/cases/{case_no}/claim` | `case:dispose` | 认领（`findAndModify` 原子，幂等） |
| POST | `/cases/{case_no}/dispose/preview` | `case:dispose` | 签发二次确认令牌 + 副作用清单（**无副作用**） |
| POST | `/cases/{case_no}/dispose` | `case:dispose` | 执行处置（核心） |
| POST | `/cases/{case_no}/biz-sync/retry` | `case:dispose` | 业务系统同步重试 |
| POST | `/cases/{case_no}/archive` | `sys:config`（**admin**，见下） | 归档 |
| GET | `/cases/{case_no}/actions` | `case:read` | 处置流水（07 详情页复用） |

## 权限：严格照权限矩阵，**不自造权限码**（D26 / D28）

`claim` / `dispose` / `preview` / `biz-sync/retry` 用 **`P_CASE_DISPOSE`**（仅
reviewer），读用 **`P_CASE_READ`**（仅 reviewer）——两者都取自
`app/security/permissions.py`，本文件**没有一处角色判断**（D28：所有权限拒绝
由权限层统一给 `AUTH-4020`）。

⚠️ **`archive` 的权限是一处需要裁定的借用**：Spec 08 §3.1 / BR-08-36 要求
"`archive` 仅 `admin` 或系统定时任务"，但权限矩阵里 **`admin` 没有任何
`case:*` 权限**（`case:read` / `case:dispose` 都只有 reviewer）。既然
"矩阵是唯一真源"且"不得新造权限码"，本实现只能**借用一个既有的 admin 专属
权限**：选 `P_SYS_CONFIG`（`sys:config`，"系统设置（运行参数）"）——归档阈值
`CASE_ARCHIVE_AFTER_DAYS` 本身就属系统运行参数（BR-08-38），且系统定时任务的
自动归档走的是同一个服务方法。另一条同样成立的选项是在模块 01 的矩阵里
**新增 `case:archive` 并授予 admin**（改一行数据 + 一条断言），是否采纳需
评审裁定（已登记在交付报告）。

## 为什么响应体在这里用 `envelope(...)` 包

本仓库的约定是 **handler 自己包信封**（框架不做包装，D17）。因此每个端点
都显式 `return envelope("OK", ..., _trace(request), data)`。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request

from app.api import register_router
from app.deps import client_ip, require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.security.permissions import P_CASE_DISPOSE, P_CASE_READ, P_SYS_CONFIG
from app.schemas.case_schema import (
    ArchiveIn,
    BizSyncRetryIn,
    DisposeIn,
    DisposePreviewIn,
)
from app.services.case_service import CaseService, get_case_service
from app.services.disposal_service import DisposalService, get_disposal_service

router = APIRouter(tags=["案件处置"])


# ============================================================
# 依赖装配
# ============================================================
def get_case_service_dep() -> CaseService:
    """案件服务（**每次请求重新装配仓储**）。

    不缓存 `CaseRepo`：测试会切换数据库（`db.use_database`），缓存了某个库的
    集合句柄就会把数据写到别的库里（与 `audit_service._repo` 同因）。
    """
    return get_case_service()


def get_disposal_service_dep() -> DisposalService:
    return get_disposal_service()


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


def _actor(user: dict) -> str:
    """操作人取自**令牌身份**（BR-08-07）：请求体里的同名字段一律忽略。"""
    return str(user.get("_id") or user.get("username") or "unknown")


def _role(user: dict) -> str:
    return str(user.get("role") or "")


# ============================================================
# 写：认领
# ============================================================
@router.post("/cases/{case_no}/claim", summary="认领案件")
async def claim_case(
    request: Request,
    case_no: str,
    service: CaseService = Depends(get_case_service_dep),
    user: dict = Depends(require_permission(P_CASE_DISPOSE)),
):
    """认领（Spec §3.1，BR-08-04 的**原子**认领）。

    幂等：同一个人重复认领返回 `200` 且**不刷新** `claimed_at`
    （响应里的 `changed=false` 就是给前端判断"这次点击到底有没有生效"用的）。
    """
    data = await service.claim(
        case_no, _actor(user), actor_role=_role(user),
        ip=client_ip(request), ua=request.headers.get("user-agent"),
    )
    message = "认领成功" if data.get("changed") else "你已认领该案件（幂等，未刷新认领时间）"
    return envelope("OK", message, _trace(request), data)


# ============================================================
# 写：二次确认预览（**无副作用**）
# ============================================================
@router.post("/cases/{case_no}/dispose/preview", summary="处置预览（签发二次确认令牌）")
async def preview_dispose(
    request: Request,
    case_no: str,
    payload: DisposePreviewIn,
    service: DisposalService = Depends(get_disposal_service_dep),
    user: dict = Depends(require_permission(P_CASE_DISPOSE)),
):
    """服务端权威生成弹窗内容 + 签发一次性令牌（Spec §2.2 / BR-08-26~28）。

    **本接口不产生任何副作用**：不写库、不发事件（BR-08-28）。前端"取消"
    弹窗时不发任何请求，因此取消路径上连这一步都不会发生。
    """
    data = await service.preview(case_no, payload, _actor(user))
    return envelope("OK", "已生成处置预览与确认令牌（30 秒内有效）",
                    _trace(request), data)


# ============================================================
# 写：执行处置（核心）
# ============================================================
@router.post("/cases/{case_no}/dispose", summary="执行处置")
async def dispose_case(
    request: Request,
    case_no: str,
    payload: DisposeIn,
    service: DisposalService = Depends(get_disposal_service_dep),
    user: dict = Depends(require_permission(P_CASE_DISPOSE)),
):
    """执行处置（Spec §3.1）。严格按 BR-08-29 的顺序执行七步副作用。

    错误码：`400 DSP-4001`（参数）/ `422 DSP-4002`（不相容）/ `409 DSP-4003`
    （状态）/ `409 DSP-4004`（已处置）/ `409 DSP-4005`（非认领人）/
    `422 DSP-4006`（令牌）/ `404 DSP-4040`（案件不存在）/ `503 DSP-5002`
    （名单写入失败，已回滚）/ `500 DSP-5001` / `500 DSP-5005`。
    旁路失败（`DSP-5003` / `DSP-5004`）**仍是 200**，靠 `notice_code` 与
    `degraded` 表达（那两个码不在错误码表里，见 `errors.DSP_NOTICE`）。
    """
    data = await service.dispose(
        case_no, payload, _actor(user), actor_role=_role(user),
        ip=client_ip(request), ua=request.headers.get("user-agent"),
    )
    message = "处置成功"
    if data.get("degraded"):
        message = (data.get("notice")
                   or "处置已生效，但存在待重试的旁路副作用")
    return envelope("OK", message, _trace(request), data)


# ============================================================
# 写：业务同步重试
# ============================================================
@router.post("/cases/{case_no}/biz-sync/retry", summary="重试业务系统同步")
async def retry_biz_sync(
    request: Request,
    case_no: str,
    payload: Optional[BizSyncRetryIn] = None,
    service: DisposalService = Depends(get_disposal_service_dep),
    user: dict = Depends(require_permission(P_CASE_DISPOSE)),
):
    """重试 `BizAdapter` 同步（Spec §3.1）。

    `action_id` 可省（缺省重试该案件全部未成功的联动）。`attempt_no` 递增，
    每次重试**追加**一条审计，不覆盖原记录。只重试业务同步——名单与案件状态
    在处置时已经生效，重放它们等于重复执行一次不可撤销的处置。
    """
    data = await service.retry_biz_sync(
        case_no, (payload.action_id if payload else None), _actor(user),
        actor_role=_role(user), ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    message = ("业务系统已同步成功" if not data.get("notice_code")
               else str(data.get("notice")))
    return envelope("OK", message, _trace(request), data)


# ============================================================
# 写：归档（**仅 admin**，见模块 docstring 的权限裁定）
# ============================================================
@router.post("/cases/{case_no}/archive", summary="归档案件（仅 admin）")
async def archive_case(
    request: Request,
    case_no: str,
    payload: Optional[ArchiveIn] = None,
    service: CaseService = Depends(get_case_service_dep),
    user: dict = Depends(require_permission(P_SYS_CONFIG)),
):
    """归档（Spec §3.1：仅 `disposed` 可归档；角色限 `admin` 或系统定时任务）。

    写 `archived_at` + 审计 `case.archive`（BR-08-09：状态迁移必须留痕）；
    审计写不进则**回滚归档**并返回 `AUD-5001`（D41）。
    """
    data = await service.archive(
        case_no, _actor(user),
        remark=str((payload.remark if payload else "") or ""),
        actor_role=_role(user), ip=client_ip(request),
        ua=request.headers.get("user-agent"),
    )
    return envelope("OK", "案件已归档", _trace(request), data)


# ============================================================
# 读：处置流水（07 详情页复用）
# ============================================================
@router.get("/cases/{case_no}/actions", summary="案件处置流水")
async def list_case_actions(
    request: Request,
    case_no: str,
    service: CaseService = Depends(get_case_service_dep),
    _user: dict = Depends(require_permission(P_CASE_READ)),
):
    """按 `acted_at` 升序返回该案件的全部处置流水（Spec §3.1 / §3.2）。

    只读，`case:read` 把关；07 的详情页直接复用本接口，**不重复实现**。
    """
    data = await service.list_actions(case_no)
    return envelope("OK", "ok", _trace(request), data)


# 模块编号 `08` **只登记一次**（一个模块一个前缀，D18）。
register_router("08", router)

# 模块 07（案件审核工作台）的**读侧**端点（`GET /cases` 列表、`GET /cases/{case_no}`
# 详情）**不在本文件**：它们定义在 `app/api/case_query_api.py`，并以
# `register_router("07", ...)` 单独登记一次（模块编号只能登记一次，D18/D67）。
# 本文件只负责处置侧，两者的分工见 `case_query_api.py` 的模块 docstring。
