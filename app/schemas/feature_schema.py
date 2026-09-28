# -*- coding: utf-8 -*-
"""模块 04 的响应模型（§3.2 快照、§3.3 特征元数据）。

## 为什么响应模型"真的被使用"

§3.2 的 8 个字段是模块 07（审核工作台）与模块 10（仿真页）的渲染依据。
靠手写 dict 很容易漂移：少一个 `missing_features`、把 `window_config` 写成
`window` 都不会在服务端报错，只会在页面上表现为一片空白。接口层把服务端
组装好的 dict 过一遍模型再回包，字段名/类型对不上会**在设计阶段**暴露。

## 为什么 `degrade_suggested` **不**出现在响应模型里

它是**内部信号**（04 → 03 的契约字段），03 用它决定"要不要在调 05 之前
fail-closed 短路"。把它放进对外响应会诱导前端/调用方去解释这个信号，
而它对 07/10 没有意义——页面上真正要看的是 `missing_features` 与
快照顶部的 `truncated` 黄条（BR-04-19 / FEA-5003）。

## 为什么 `extra="allow"`

库里的快照可能比当前模型多字段（例如 `missing_reasons`、`degrade_reasons`、
`truncated_dimensions`）。这些字段**必须原样透传**：它们是人工复核时
"为什么这一项缺失/为什么这一条带黄条"的唯一依据，删掉就等于让复核者
只能看到一个没有原因的特征列表。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# §3.2 快照
# ============================================================
class WindowConfigOut(BaseModel):
    """`window_config`（E02 的 `window_config` 结构，BR-04-05）。

    字段名与 E02 §window_config 逐字一致，快照里落的就是它——
    "用当时的配置复算"依赖这四个字段齐全。
    """

    model_config = ConfigDict(extra="allow")

    short_window_min: int = 60
    long_window_min: int = 1440
    agg_mode: str = "in_memory_sliding"
    config_version: str = "w1"


class FeatureSnapshotOut(BaseModel):
    """`GET /features/{event_id}` 的 `data`（§3.2 响应表逐字段对齐）。"""

    model_config = ConfigDict(extra="allow")

    snapshot_id: str
    event_id: str
    user_id: str
    #: 18 项特征 KV（键名与 E02 逐项一致，BR-04-06）
    features: dict[str, Any] = Field(default_factory=dict)
    #: `{short_window_min, long_window_min, agg_mode, config_version}`
    window_config: dict[str, Any] = Field(default_factory=dict)
    computed_at: int
    compute_ms: int = 0
    #: 因数据不足未能计算的特征名。**缺失不代表 0**（BR-04-09）
    missing_features: list[str] = Field(default_factory=list)


# ============================================================
# §3.3 特征元数据
# ============================================================
class StatBaselineOut(BaseModel):
    """E21 的**统计基线**（P50/P95，每日刷新）。

    与 `baseline`（静态参照区间）是两个不同的东西：前者是"算出来的近期真实
    分布"，后者是"人工配置的正常区间"。分开下发是刻意的——合并之后
    "这个数字是拍脑袋定的还是算出来的"就再也说不清（详见
    `app/engine/feature_baselines.py` 的模块说明）。
    """

    model_config = ConfigDict(extra="allow")

    p50: Optional[float] = None
    p95: Optional[float] = None
    sample_size: int = 0
    window_days: Optional[int] = None
    computed_at: Optional[int] = None
    segment: str = "all"


class FeatureMetaItem(BaseModel):
    """单个特征的元数据（§3.3 的 `items[]`）。

    `baseline` 取值可能是 `"≤1"` / `"2~5"` / `"false"` / `"≤5%"`，
    也可能是哨兵 `"__none__"`（无基线，界面显示 `—`，BR-04-19）。
    **前端不得硬编码**这些取值（BR-04-16）。
    """

    model_config = ConfigDict(extra="allow")

    key: str
    label: str
    group: str
    unit: str = ""
    #: int / float / bool / str（与 E02 的「类型」列一致）
    data_type: str
    #: 静态参照区间；`"__none__"` 表示无基线
    baseline: str
    #: 为什么是这个区间 / 为什么没有基线
    baseline_desc: str = ""
    has_baseline: bool = True
    #: 偏离方向提示（前端派生「偏离」列的依据，BR-04-18）
    direction_hint: str = "none"
    #: E21 统计基线；查不到即 `null`（**不得用 0 冒充**）
    stat_baseline: Optional[StatBaselineOut] = None
    stat_baseline_available: bool = False


class FeatureMetaOut(BaseModel):
    """`GET /features/meta` 的 `data`（§3.3）。"""

    model_config = ConfigDict(extra="allow")

    #: 18 项，顺序固定为「行为频次 → 设备环境 → 网络 IP → 地址聚集 → 账号画像」
    items: list[FeatureMetaItem] = Field(default_factory=list)
    #: 项数（=18）。前端可用它断言"表头没漏项"，不必自己数数组长度
    total: int = 0
    #: 本次下发的静态基线的配置来源说明（BR-04-16：集中定义、统一下发）
    baseline_source: str = "feature_baselines.py"
    #: E21 统计基线当前使用的分段；09 落地前固定为 `all`
    stat_segment: str = "all"


__all__ = [
    "FeatureMetaItem",
    "FeatureMetaOut",
    "FeatureSnapshotOut",
    "StatBaselineOut",
    "WindowConfigOut",
]
