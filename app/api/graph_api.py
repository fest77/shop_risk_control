# -*- coding: utf-8 -*-
"""关联网络查询 HTTP 入口（模块 09 §3.2）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/graph/{entity_type}/{entity_id}` | `case:read` | 关联实体图谱（中栏 `graphBox`） |

## 权限为什么是 `case:read`

与 `/profiles/{user_id}` 同源：图谱与画像是**同一个中栏区块组**的两半
（`profileCard` + `graphBox`），消费者都是模块 07 的案件审核工作台，
`permissions.py` 的注释已把「画像」归到 `case:read`。**不新造权限码**。

## 参数校验为什么不在 FastAPI 的 `Query(le=...)` 里做

- `max_hop=3` 必须给出**本模块的错误码** `GRP-4001`（AD-06/BR-09-15），
  而框架级的越界会先被拦成通用 `COM-4001`，调用方按码分流时会走错分支；
- `entity_type=phone` 必须给出 `GRP-4002`（§5：「明确拒绝，不静默降级」），
  若把 `entity_type` 声明成路径参数枚举，框架同样只会给 `COM-4001`。

因此这里只声明**类型**（保证 `?max_hop=abc` 这类非数字仍归框架的 `COM-4001`），
取值范围与枚举的判定统一在 `graph_service` 里，错误码只有一处真源。

## 截断必须显式告知（BR-09-18）

`truncated=true` 时响应里同时给出 `total_nodes` / `total_edges`（**截断前的真实
数量**），前端据此显示橙字「关系过多，已展示关联强度最高的 200 个节点（共 N 个）」。
后端**不允许静默截断**：人工研判必须知道这张图不完整——证据链的可信度正是建立
在"不完整就说出来"之上。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.deps import require_permission
from app.errors import envelope
from app.logging import get_trace_id
from app.schemas.profile_schema import GraphOut
from app.security.permissions import P_CASE_READ
from app.services import graph_service
from app.services.graph_service import (
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_HOP,
    DEFAULT_MAX_NODES,
    MAX_EDGES_CAP,
    MAX_HOP,
    MAX_NODES_CAP,
)

router = APIRouter(tags=["画像与关联图谱"])


def _trace(request: Request) -> str:
    """取当前请求的 trace_id（中间件未生效时兜底）。"""
    return getattr(request.state, "trace_id", None) or get_trace_id()


@router.get("/graph/{entity_type}/{entity_id}", summary="关联网络（1~2 跳）")
async def entity_graph(
    request: Request,
    entity_type: str,
    entity_id: str,
    max_hop: int = Query(DEFAULT_MAX_HOP, description=f"跳数 1~{MAX_HOP}（AD-06 硬上限）"),
    max_nodes: int = Query(DEFAULT_MAX_NODES, description=f"节点上限，最大 {MAX_NODES_CAP}"),
    max_edges: int = Query(DEFAULT_MAX_EDGES, description=f"边上限，最大 {MAX_EDGES_CAP}"),
    risk_only: bool = Query(False, description="只返回 risk_flag=true 的边及其端点"),
    _user: dict = Depends(require_permission(P_CASE_READ)),
):
    """查 `entity_type`（`user`/`device`/`ip`/`address`）的关联网络。

    - 2 跳查询**先取 1 跳、再批量取 2 跳**（BR-09-16），节点属性批量补齐
      （BR-09-20），因此整张图的查询次数是**常数**且与节点数无关；
    - 节点/边超上限时按 `weight` 降序保留，并置 `truncated=true`
      同时返回截断前的真实 `total_*`（BR-09-17/18）；
    - 超时（>3s）按 §5 的 `GRP-5002` 返回**已取到的部分** + `truncated=true`，
      不报错；
    - 中心实体不存在 → `GRP-4004`（404）；存在但无任何关联 → **200** + 空边集
      （§2.2 的「孤立账号」，与"查不到"是两件事）。
    """
    data = await graph_service.query(
        entity_type, entity_id,
        max_hop=max_hop, max_nodes=max_nodes, max_edges=max_edges, risk_only=risk_only,
    )
    # `by_alias=True`：`GraphEdgeOut.from_` 的对外键名必须是 `from`（§3.2）
    payload = GraphOut.model_validate(data).model_dump(by_alias=True)
    # `timeout` / `attributes_partial` / `total_is_lower_bound` 是模型未声明的
    # 诊断键（`extra="allow"` 不保留它们），这里显式并回：它们是**如实告知**的
    # 一部分（超时返回部分结果、属性缺失、总数只是下界），丢掉会让响应看起来
    # 像一张完整的图
    for key in ("timeout", "notice", "attributes_partial", "total_is_lower_bound"):
        if key in data:
            payload[key] = data[key]
    return envelope("OK", "查询成功", _trace(request), payload)


# 模块编号 "09"（`00_模块划分与边界` §6）的 router **只在 `profile_api.py` 登记一次**：
# `register_router()` 对重复编号会显式报错（避免两个模块用同一编号时静默覆盖，
# 那会让后登记者的接口凭空消失）。本模块只负责**定义**路由，由 `profile_api`
# 把本 router `include_router` 进去后统一登记。

__all__ = ["router"]
