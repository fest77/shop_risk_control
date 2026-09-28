# -*- coding: utf-8 -*-
"""累计直方图 → P95 近似值（模块 11 BR-11-13）——**纯函数，无 IO**。

**为什么用直方图而不是扫明细**：AD-03 明确禁止查询时聚合 `decisions` 全表。
直方图在**写入时**用 `$inc` 累加（9 个桶位），查询时只读一个文档即可估算分位。

**精度**：在桶内按线性插值估计，误差目标 ≤10%（V-11-12 用 1000 条已知样本与
numpy 真值比对）。这个精度对"看大盘判断系统是否变慢"完全够用；
若将来需要精确分位，只能上专门的分位算法（如 t-digest）或扫明细——两者的
代价都与 AD-03 冲突，故不采用。
"""
from __future__ import annotations

from typing import Optional

from app.engine.metric_agg import HIST_BOUNDS, HIST_KEYS


def total_count(hist: Optional[dict]) -> int:
    """直方图样本总数。"""
    if not hist:
        return 0
    return sum(int(hist.get(k) or 0) for k in HIST_KEYS)


def percentile(hist: Optional[dict], p: float = 0.95) -> Optional[float]:
    """由累计直方图线性插值求第 `p` 分位（毫秒）；无样本返回 `None`。

    `None` 与"0 毫秒"必须区分（BR-11-08）：没有样本时返回 0 会让大盘显示
    "P95 延迟 0ms"，被误读成"系统飞快"。
    """
    total = total_count(hist)
    if total <= 0:
        return None

    target = p * total
    cumulative = 0
    lower = 0.0
    for bound, key in zip(HIST_BOUNDS, HIST_KEYS[:-1]):
        count = int(hist.get(key) or 0)
        if cumulative + count >= target:
            if count <= 0:
                return float(bound)
            # 在 [lower, bound] 内按累计比例线性插值
            ratio = (target - cumulative) / count
            return round(lower + (float(bound) - lower) * ratio, 2)
        cumulative += count
        lower = float(bound)

    # 落在 +∞ 桶：用最后一个边界的 1.5 倍做保守估计（不返回一个精确的假值）
    gt = int(hist.get("gt200") or 0)
    if gt > 0:
        return round(lower * 1.5, 2)
    return round(lower, 2)
