# -*- coding: utf-8 -*-
"""画像查询 HTTP 入口（模块 09 §3.1）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/profiles/{user_id}` | `case:read` | 用户全貌画像（中栏画像卡 `profileCard`） |
| GET | `/graph/{entity_type}/{entity_id}` | `case:read` | 关联网络（中栏 `graphBox`，定义在 `graph_api.py`） |

## 模块 09 的 router 为什么在这里登记

`register_router(module_id, router)` 对**重复编号会显式报错**（设计如此：两个模块
用同一编号时静默覆盖会让后登记者的接口凭空消失）。模块 09 按 Spec §6 的文件规划
拆成 `profile_api.py` 与 `graph_api.py` 两个文件，但**模块编号只有一个**，
因此由本文件 `include_router(graph_api.router)` 后统一登记一次，
`graph_api.py` 只定义路由。

## 权限为什么是 `case:read`

`app/security/permissions.py` 里 `P_CASE_READ` 的注释已经写明它覆盖
「审核工作台：案件列表/**画像**/证据链」——画像正是本接口。因此**不新造权限码**：
权限矩阵是唯一真源（BR-01-12），新增权限串要动模块 01 的矩阵与职责分离校验
（BR-01-13），属跨模块改动。矩阵里只有 REVIEWER 拥有 `case:read`，
与"画像卡是审核员的中栏工具、模块 09 无独占页面"的定位一致。

## 错误怎么给

- 用户不存在 → `GRP-4004`（404）：中栏显示「未找到该用户画像」（§2.1 空态）；
- 画像聚合查询失败 → `GRP-5001`（503）：显示错误占位 + 重试，
  **右栏判定摘要不受影响**（§5 的区块隔离，那是前端的分区渲染，后端只需如实报错）。

两条路径都由 `profile_service` 抛项目异常，框架的异常处理器统一包装成
`{ok, code, message, trace_id, data}`——本模块**不自己拼错误包**。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api import register_router
from app.api import graph_api
from app.deps import require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.schemas.profile_schema import ProfileOut
from app.security.permissions import P_CASE_READ
from app.services import profile_service

router = APIRouter(tags=["画像与关联图谱"])


def _trace(request: Request) -> str:
    """取当前请求的 trace_id（中间件未生效时兜底）。"""
    return getattr(request.state, "trace_id", None) or get_trace_id()


@router.get("/profiles/{user_id}", summary="用户全貌画像")
async def user_profile(
    request: Request,
    user_id: str,
    _user: dict = Depends(require_permission(P_CASE_READ)),
):
    """组装 §3.1 的全貌画像：用户 + 累计统计 + 最近决策 + 设备/IP/地址 + `tag_meta`。

    **`tag_meta` 一并下发**：它是"标签 → 中文名 + 配色档位 + 颜色"的唯一真源，
    前端不得硬编码（§3.1 的字段说明）。8 个标签**全量**下发而不是只发命中项，
    这样前端不必为"没见过的标签"写兜底样式——而那种标签恰恰可能是高危。

    可空字段的 `null` 一律表示"不知道"（不是 0），例如没有 `register_at` 时
    `age_days=null`（显示「—」），绝不猜成 `0`（那是在宣称"今天刚注册"）。
    """
    data = ProfileOut.model_validate(await profile_service.get_profile(user_id))
    return envelope("OK", "查询成功", _trace(request), data.model_dump())


# 模块编号 "09"（`00_模块划分与边界` §6），由注册器统一加 `/api/v1` 前缀。
# 先把 `graph_api` 的路由并进来，保证整个模块**只登记一次**（见模块 docstring）
router.include_router(graph_api.router)
register_router("09", router)

__all__ = ["router"]
