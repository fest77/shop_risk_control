# -*- coding: utf-8 -*-
"""监控指标 HTTP 入口（模块 11 §3.1 ~ §3.8）。路径相对 `API_PREFIX`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET  | `/metrics/overview` | `dashboard:read` | 4 张卡片 |
| GET  | `/metrics/trend` | `dashboard:read` | 拦截率/请求量趋势 |
| GET  | `/metrics/distribution` | `dashboard:read` | 等级/场景分布 |
| GET  | `/metrics/rule-ranking` | `dashboard:read` | 命中排行 |
| GET  | `/metrics/throughput` | `dashboard:read` | 吞吐与延迟 |
| GET  | `/metrics/stream` | `dashboard:read` | **SSE** 实时事件流 |
| POST | `/metrics/rollup` | `sys:config` | 手动补算（幂等） |

读接口对三角色全部开放（E19 权限矩阵「查看大盘：✔✔✔」，G-11）；补算属运维动作，
仅 `admin`（`sys:config`）——错误码 `MET-4001~4005` 的判定在服务层，接口只负责
把参数透传与把响应过一遍模型。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api import register_router
from app.core.metric_stream_bus import get_bus
from app.deps import require_permission
from app.errors import envelope
from app.engine.metric_bucket import DEFAULT_RANGE
from app.logging import get_trace_id
from app.schemas.metric_schema import (
    DIMS,
    DistributionOut,
    DistributionQuery,
    MetricQueryParams,
    OverviewOut,
    RollupOut,
    RollupRequest,
    RuleRankingOut,
    RuleRankingQuery,
    ThroughputOut,
    ThroughputQuery,
    TrendOut,
    WINDOWS,
)
from app.security.permissions import P_DASHBOARD_READ, P_SYS_CONFIG
from app.services import metric_service
from app.services.metric_service import MetricService

router = APIRouter(tags=["监控指标"])


def get_metric_service() -> MetricService:
    """取指标服务单例（缓存/快照必须跨请求共享，见 `MetricService` 的说明）。"""
    return metric_service.get_metric_service()


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_id()


# ============================================================
# 查询参数依赖（把 Query 参数收进 Pydantic 模型，便于复用与文档化）
# ============================================================
def _metric_params(
    range_: str = Query(DEFAULT_RANGE, alias="range",
                        description=f"时间范围：{' / '.join(('1h', '24h', '7d', '30d'))}"),
    scene: Optional[str] = Query(None, description="login / coupon / order / pay / aftersale"),
    level: Optional[str] = Query(None, description="low / medium / high"),
    granularity: Optional[str] = Query(None, description="缺省由 range 推导；与 range 不匹配 -> MET-4002"),
) -> MetricQueryParams:
    """公共查询参数（§3.1）。**不在依赖里判枚举**：非法值要返回 `MET-4xxx` 而不是 `COM-4001`。"""
    return MetricQueryParams(range=range_, scene=scene, level=level, granularity=granularity)


def _distribution_params(
    params: MetricQueryParams = Depends(_metric_params),
    dim: str = Query("level", description=f"分布维度：{' / '.join(DIMS)}"),
) -> DistributionQuery:
    return DistributionQuery(**params.model_dump(), dim=dim)


def _ranking_params(
    params: MetricQueryParams = Depends(_metric_params),
    top: int = Query(10, description="1~50；越界 -> MET-4003"),
) -> RuleRankingQuery:
    return RuleRankingQuery(**params.model_dump(), top=top)


def _throughput_params(
    window: str = Query("5m", description=f"吞吐窗口：{' / '.join(WINDOWS)}"),
) -> ThroughputQuery:
    return ThroughputQuery(window=window)


# ============================================================
# 查询接口
# ============================================================
@router.get("/metrics/overview", summary="态势大盘指标卡片")
async def overview(
    request: Request,
    params: MetricQueryParams = Depends(_metric_params),
    service: MetricService = Depends(get_metric_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """4 张卡片（§3.2）。`block_rate` / `avg_score` 由服务端算好，02 只格式化。"""
    data = await service.overview(
        range_=params.range, scene=params.scene, level=params.level,
        granularity=params.granularity,
    )
    return envelope("OK", "查询成功", _trace(request), OverviewOut.model_validate(data).model_dump())


@router.get("/metrics/trend", summary="拦截率 / 请求量趋势")
async def trend(
    request: Request,
    params: MetricQueryParams = Depends(_metric_params),
    service: MetricService = Depends(get_metric_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """趋势（§3.3）。空桶已补零、末点 `partial=true`，02 不做补点。"""
    data = await service.trend(
        range_=params.range, scene=params.scene, level=params.level,
        granularity=params.granularity,
    )
    return envelope("OK", "查询成功", _trace(request), TrendOut.model_validate(data).model_dump())


@router.get("/metrics/distribution", summary="风险等级 / 场景分布")
async def distribution(
    request: Request,
    params: DistributionQuery = Depends(_distribution_params),
    service: MetricService = Depends(get_metric_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """分布（§3.4）。`key`/`name`/`cnt`/`ratio` 由服务端给定固定顺序与中文名。"""
    data = await service.distribution(
        range_=params.range, scene=params.scene, level=params.level,
        granularity=params.granularity, dim=params.dim,
    )
    return envelope("OK", "查询成功", _trace(request), DistributionOut.model_validate(data).model_dump())


@router.get("/metrics/rule-ranking", summary="规则命中排行")
async def rule_ranking(
    request: Request,
    params: RuleRankingQuery = Depends(_ranking_params),
    service: MetricService = Depends(get_metric_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """排行（§3.5）。`rank` 与 `metric_field` 由服务端给出，02 不得自行重排或切口径。"""
    data = await service.rule_ranking(
        range_=params.range, scene=params.scene, level=params.level,
        granularity=params.granularity, top=params.top,
    )
    return envelope("OK", "查询成功", _trace(request), RuleRankingOut.model_validate(data).model_dump())


@router.get("/metrics/throughput", summary="吞吐与决策延迟")
async def throughput(
    request: Request,
    params: ThroughputQuery = Depends(_throughput_params),
    service: MetricService = Depends(get_metric_service),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """吞吐与 P95 延迟（§3.6）。直方图缺失时 `p95_elapsed_ms=null`，不得返回 0 冒充。"""
    data = await service.throughput(window=params.window)
    return envelope("OK", "查询成功", _trace(request), ThroughputOut.model_validate(data).model_dump())


# ============================================================
# SSE 实时事件流
# ============================================================
def _parse_last_event_id(raw: Optional[str]) -> Optional[int]:
    """解析 `Last-Event-ID` 请求头。

    非数字一律当作"没有该头"处理：浏览器扩展或中间设备可能塞进任意字符串，
    为此返回 400 会让实时流在个别客户端上永远连不上，代价远大于"补偿从最新开始"。
    """
    if not raw:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


@router.get("/metrics/stream", summary="实时事件流（SSE）", response_class=StreamingResponse)
async def stream(
    request: Request,
    scene: Optional[str] = Query(None, description="服务端过滤：login / coupon / order / pay / aftersale"),
    level: Optional[str] = Query(None, description="服务端过滤：low / medium / high"),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID",
                                          description="断线重连时的补偿起点（由浏览器自动携带）"),
    _user: dict = Depends(require_permission(P_DASHBOARD_READ)),
):
    """SSE 实时事件流（§3.7）。

    **本接口是统一响应包的例外**（与 `/audit/export` 的导出、`/` 的 HTML 同理）：
    它返回的是 `text/event-stream` 长连接帧流，帧格式由 SSE 规范定义（`id:` /
    `event:` / `data:` + 空行），**不可能**再套一层 `{ok, code, data}` 信封。
    错误仍然走统一契约——但只限于"流开始之前"的错误（未登录 401、越权 403、
    订阅数超限 `MET-5004`），因为响应头一旦发出就无法再改状态码。
    """
    # 订阅必须在**返回响应之前**完成：这是 MET-5004 能以 503 统一响应包回给客户端的
    # 唯一时机（在生成器里才判定的话，响应已经 200 开头了）
    sub = get_bus().subscribe(
        scene=metric_service.require_scene(scene),
        level=metric_service.require_level(level),
    )
    return StreamingResponse(
        get_bus().frames(sub, _parse_last_event_id(last_event_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 关掉 Nginx 的响应缓冲：否则帧会被攒在代理里，"实时"变成"批量延迟到达"
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Trace-Id": _trace(request),
        },
    )


# ============================================================
# 手动补算（运维）
# ============================================================
@router.post("/metrics/rollup", summary="手动补算 / 修复指标桶")
async def manual_rollup(
    request: Request,
    payload: RollupRequest,
    service: MetricService = Depends(get_metric_service),
    _user: dict = Depends(require_permission(P_SYS_CONFIG)),
):
    """按时间窗重算 `1h` / `1d` 桶（§3.8）。

    幂等（`$set` 覆盖）：重复执行同一时间窗结果完全一致（BR-11-15），
    因此运维可以放心重试。请求体里的 `granularity`/`dimension` 用 `Literal` 约束
    （拼错报文 -> `COM-4001`），区间非法 -> `MET-4005`，执行失败 -> `MET-5006`。
    """
    data = await service.rollup(
        granularity=payload.granularity, from_ts=payload.from_ts,
        to_ts=payload.to_ts, dimension=payload.dimension,
    )
    return envelope("OK", "补算完成", _trace(request), RollupOut.model_validate(data).model_dump())


# 模块编号 "11"（`00_模块划分与边界` §6），由注册器统一加 `/api/v1` 前缀
register_router("11", router)

__all__ = ["get_metric_service", "router"]
