# -*- coding: utf-8 -*-
"""特征计算引擎 HTTP 入口（模块 04 §3.2 / §3.3）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/features/meta` | `dashboard:read` | 18 项特征的元数据 + 静态参照基线 |
| GET | `/features/{event_id}` | `dashboard:read` | 查询某事件的快照 |

## 路由顺序为什么重要

`/features/meta` **必须**声明在 `/features/{event_id}` 之前。FastAPI 按注册顺序
匹配，若反过来，`GET /features/meta` 会先命中 `{event_id}` 路径参数，
于是"取元数据"变成"查一个叫 meta 的事件快照"并返回 `FEA-4004`。
这类顺序缺陷在手工测试时极难发现（只看单个接口是好的），因此这里加了注释钉住。

## 权限假设（Spec 未规定，按最小合理假设并标注）

Spec §3.1/§3.3 都没写权限。这里两个接口都用 **`P_DASHBOARD_READ`**
（`dashboard:read`，三角色均可读）：

- §3.2 的消费者是**模块 07**（案件审核工作台，reviewer 主用）与**模块 10**
  （事件仿真测试页，strategist 主用）。若限成某个角色，另一方就会取不到数据；
- 两者都是**只读**接口，不改变任何状态；
- 新增一个专用权限串要动模块 01 的权限矩阵（BR-01-12 的唯一真源）与
  职责分离校验（BR-01-13），属跨模块改动，超出本模块范围。

**这是一处刻意的偏离**（Spec 未规定），已登记在交付报告里。
生产环境若要求"仿真页只能策略师访问"，只需把这里的依赖换成新的权限串。

## 为什么错误码在服务层判

`FEA-4001`（编号格式非法）与 `FEA-4004`（查不到）对调用方的含义不同：
前者是报文错误（可改），后者是数据尚未落库（可重试）。若用
`Path(pattern=...)` 让框架拦格式，会得到通用 `COM-4001`，
调用方按错误码分流时会走错分支——因此格式判定在 `feature_service`。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api import register_router
from app.deps import require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.schemas.feature_schema import FeatureMetaOut, FeatureSnapshotOut
from app.security.permissions import P_DASHBOARD_READ
from app.services import feature_service
from app.services.feature_service import FeatureService, build_feature_meta

router = APIRouter(tags=["特征计算"])


def get_service() -> FeatureService:
    """取特征服务单例。

    滑动窗口是**进程内状态**（AD-05），因此必须跨请求共享同一份——
    每次请求新建一个服务会让每个事件的窗口里只有它自己，
    `coupon_cnt_1h` 之类永远等于 1。
    """
    return feature_service.get_feature_service()


def _trace(request: Request) -> str:
    """取当前请求的 trace_id（中间件未生效时兜底）。"""
    return getattr(request.state, "trace_id", None) or get_trace_id()


@router.get("/features/meta", summary="18 项特征元数据与基线")
async def feature_meta(
    request: Request,
    service: FeatureService = Depends(get_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """下发 18 项特征的键名/标签/分组/单位/类型 + 静态参照区间（§3.3）。

    **前端不得硬编码特征名与基线**（BR-04-16）：中文标签、分组、单位与
    `≤1`/`2~5`/`false`/`≤5%`/`—` 全部由本接口给出，保证与后端逐字一致。
    E21 的统计基线在 `stat_baseline` 键下（查不到即 `null`，不得用 0 冒充）。
    """
    stat = await service.latest_baselines()
    items = build_feature_meta(stat)
    data = FeatureMetaOut.model_validate({
        "items": items,
        "total": len(items),
        "baseline_source": "feature_baselines.py",
        "stat_segment": "all",
    })
    return envelope("OK", "查询成功", _trace(request), data.model_dump())


@router.get("/features/{event_id}", summary="查询某事件的特征快照")
async def feature_snapshot(
    request: Request,
    event_id: str,
    service: FeatureService = Depends(get_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """按事件编号取快照（§3.2，供 07 的研判页与 10 的仿真页读取）。

    - 编号格式非法 → `FEA-4001`（422）
    - 查不到快照 → `FEA-4004`（404）+ 提示：快照是**异步落库**的
      （BR-04-22），刚接入的事件稍后重试即可。这个 404 是正常状态，
      不是故障；把它显示成"系统错误"会让审核员误判为数据丢失。
    """
    data = FeatureSnapshotOut.model_validate(await service.get_snapshot(event_id))
    return envelope("OK", "查询成功", _trace(request), data.model_dump())


# 模块编号 "04"（`00_模块划分与边界` §6），由注册器统一加 `/api/v1` 前缀
register_router("04", router)

__all__ = ["get_service", "router"]
