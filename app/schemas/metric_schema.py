# -*- coding: utf-8 -*-
"""模块 11 的查询参数与响应模型（§3.1 ~ §3.6）。

## 参数模型为什么**不做**枚举校验

`audit_schema.py` 已确立的原则在此同样成立：语义边界留给服务层。若用
`Literal["1h","24h",...]` 约束 `range`，非法值会先被 FastAPI 拦成通用
`COM-4001`（422），而契约要求的是 **`MET-4001`**（400）——调用方按状态码与
错误码分流时会走错分支。因此这里只做**长度与类型**约束，枚举判定在
`metric_service` 里按 `MET_CODES` 表给出确定的错误码。

## 响应模型为什么真的被使用

§3.2 ~ §3.6 的字段是模块 02 的渲染依据，靠"手写 dict"很容易与 Spec 漂移
（少一个 `truncated`、把 `block_rate` 写成 `rate` 都不会报错）。接口层把服务端
组装好的 dict 过一遍模型再回包，字段名/类型对不上会**立刻**在设计阶段暴露。

注意 `stale` / `stale_at`：Spec 只在 §3.2 的 overview 里列出它们，但 BR-11-20 的
降级语义适用于**全部查询接口**——趋势图同样不能用 0 值冒充真实数据。因此五个
响应都带上这两个字段（02 只需在 `stale=true` 时把卡片与图表标注为"数据可能已过期"）。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.engine.metric_agg import LEVEL_KEYS, SCENE_KEYS, UNKNOWN_SCENE
from app.engine.metric_bucket import DEFAULT_RANGE, GRANULARITIES, RANGE_SPECS

# ============================================================
# 枚举取值（响应模型与文档使用；**校验仍由服务层负责**）
# ============================================================
RANGES: tuple[str, ...] = tuple(RANGE_SPECS)          # 1h / 24h / 7d / 30d
WINDOWS: tuple[str, ...] = ("1m", "5m", "15m")        # §3.6（默认 5m）
DIMS: tuple[str, ...] = ("level", "scene")            # §3.4（默认 level）
ROLLUP_GRANULARITIES: tuple[str, ...] = ("1h", "1d")  # §3.8
ROLLUP_DIMENSIONS: tuple[str, ...] = ("all", "global", "rule", "level", "scene")

# 场景筛选可选值：E06 的五个业务场景 + `unknown`（BR-11-06 会真实产生该桶）。
# §3.1 的参数表只列了四项，但 frozen 的 `metric_agg.SCENE_KEYS` 含 `pay`
# （决策 D12 把 order_pay 映射到 pay，N-11-6 的裁定结果），以引擎为准。
SCENES: tuple[str, ...] = (*SCENE_KEYS, UNKNOWN_SCENE)
LEVELS: tuple[str, ...] = LEVEL_KEYS

# 中文名由**本模块**下发（§2.3/§2.4：02 不维护映射表）。
# 场景名对齐 `enums.EventType` 的标签，等级名对齐 `enums.RiskLevel`。
LEVEL_NAMES: dict[str, str] = {"low": "低风险", "medium": "中风险", "high": "高风险"}
SCENE_NAMES: dict[str, str] = {
    "login": "登录",
    "coupon": "领券",
    "order": "下单",
    "pay": "支付",
    "aftersale": "售后申请",
    UNKNOWN_SCENE: "未知场景",
}


def name_of_level(key: str) -> str:
    """等级中文名；未知键回落原值（不抛异常：图例缺一个名字不该让整个接口 500）。"""
    return LEVEL_NAMES.get(key, key)


def name_of_scene(key: str) -> str:
    """场景中文名；未知键回落原值。"""
    return SCENE_NAMES.get(key, key)


# ============================================================
# 查询参数模型
# ============================================================
class MetricQueryParams(BaseModel):
    """四个查询接口共用的公共参数（§3.1）。

    字段全部保持 `str`/`Optional[str]`：合法性由 `metric_service` 判定，
    这样才能返回 `MET-4001` / `MET-4002` / `MET-4004` 而不是 `COM-4001`。
    """

    range: str = Field(default=DEFAULT_RANGE, description="1h / 24h / 7d / 30d")
    scene: Optional[str] = Field(default=None, description="login / coupon / order / pay / aftersale")
    level: Optional[str] = Field(default=None, description="low / medium / high")
    granularity: Optional[str] = Field(
        default=None,
        description=f"缺省由 range 推导；取值 {' / '.join(GRANULARITIES)}",
    )

    model_config = {"populate_by_name": True}


class DistributionQuery(MetricQueryParams):
    """§3.4：分布多一个 `dim`。"""

    dim: str = Field(default="level", description="level / scene")


class RuleRankingQuery(MetricQueryParams):
    """§3.5：排行多一个 `top`（1~50，默认 10）。"""

    top: int = Field(default=10, description="1~50")


class ThroughputQuery(BaseModel):
    """§3.6：吞吐只有 `window` 一个参数（不套 range，见该节字段说明）。"""

    window: str = Field(default="5m", description="1m / 5m / 15m")


# ============================================================
# 响应模型
# ============================================================
class OverviewCards(BaseModel):
    """§3.2 的 4 张卡片 + 资损口径分项。"""

    event_cnt: int
    pass_cnt: int
    review_cnt: int
    reject_cnt: int
    # 分母为 0 必须是 null（BR-11-08）：0.0 会被读成"拦截率为零"
    block_rate: Optional[float]
    avg_score: Optional[float]
    pending_case_cnt: int
    estimated_saved_amount: int
    # 供口径切换与排查（§3.2）：分项之和等于 estimated_saved_amount
    saved_amount_by_type: dict[str, int]


class OverviewOut(BaseModel):
    """`GET /metrics/overview` 响应体。"""

    range: str
    granularity: str
    cards: OverviewCards
    stale: bool
    stale_at: Optional[int]
    generated_at: int


class TrendPoint(BaseModel):
    """趋势图的一个点（空桶补零后仍在序列中，§2.2）。"""

    bucket_ts: int
    event_cnt: int
    pass_cnt: int
    review_cnt: int
    reject_cnt: int
    block_rate: Optional[float]
    partial: bool


class TrendOut(BaseModel):
    """`GET /metrics/trend` 响应体。"""

    range: str
    granularity: str
    truncated: bool
    points: list[TrendPoint]
    stale: bool
    stale_at: Optional[int]


class DistributionItem(BaseModel):
    """环形图的一个扇区。"""

    key: str
    name: str
    cnt: int
    ratio: Optional[float]


class DistributionOut(BaseModel):
    """`GET /metrics/distribution` 响应体。"""

    dim: str
    total: int
    items: list[DistributionItem]
    stale: bool
    stale_at: Optional[int]


class RuleRankingItem(BaseModel):
    """排行榜的一行。`rank` 由服务端给出，02 不得自行重排（§2.4）。"""

    rank: int
    rule_code: str
    rule_name: str
    rule_status: str
    hit_cnt: int
    hit_ratio: Optional[float]
    hit_score_sum: int


class RuleRankingOut(BaseModel):
    """`GET /metrics/rule-ranking` 响应体。`metric_field` 回显实际计数口径。"""

    range: str
    metric_field: str
    items: list[RuleRankingItem]
    stale: bool
    stale_at: Optional[int]


class ThroughputOut(BaseModel):
    """`GET /metrics/throughput` 响应体（§3.6）。"""

    window: str
    qps_1m: float
    qps_window: float
    # 直方图缺失 -> null（BR-11-13 / MET-5003），不得返回 0 冒充"飞快"
    p95_elapsed_ms: Optional[float]
    elapsed_hist: dict[str, int]
    event_cnt: int
    window_start: int
    window_end: int
    stale: bool
    stale_at: Optional[int]


class RollupRequest(BaseModel):
    """§3.8 的运维补算请求。

    `granularity` / `dimension` 在这里用 `Literal` 约束：运维接口非法入参属于
    "客户端把报文拼错了"，返回 `COM-4001` 即可；`from_ts > to_ts` 才是契约
    明确要求 `MET-4005` 的业务校验（仍由 `metric_rollup` 判定）。
    """

    granularity: Literal["1h", "1d"]
    from_ts: int = Field(description="毫秒时间戳（闭区间）")
    to_ts: int = Field(description="毫秒时间戳（闭区间）")
    dimension: Literal["all", "global", "rule", "level", "scene"] = "all"


class RollupOut(BaseModel):
    """§3.8 响应体。"""

    upserted: int
    scanned_1m_buckets: int
    elapsed_ms: int


__all__ = [
    "DIMS", "DistributionItem", "DistributionOut", "DistributionQuery",
    "LEVELS", "LEVEL_NAMES", "MetricQueryParams", "OverviewCards", "OverviewOut",
    "RANGES", "ROLLUP_DIMENSIONS", "ROLLUP_GRANULARITIES", "RollupOut", "RollupRequest",
    "RuleRankingItem", "RuleRankingOut", "RuleRankingQuery", "SCENES", "SCENE_NAMES",
    "ThroughputOut", "ThroughputQuery", "TrendOut", "TrendPoint", "WINDOWS",
    "name_of_level", "name_of_scene",
]
