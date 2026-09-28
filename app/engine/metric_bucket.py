# -*- coding: utf-8 -*-
"""桶键与时间对齐（模块 11 §4.1，BR-11-01/02/21）——**纯函数，无 IO**。

## 与 E15 的一处修正（BR-11-01 / N-11-1）

桶主键由 `{type}:{key}:{ts}` 改为 **`{type}:{key}:{granularity}:{ts}`**。
原因：整点时刻 `1m` 桶与 `1h` 桶的 `bucket_ts` **完全相同**（都是整点毫秒），
少了 `granularity` 段两者会互相覆盖——表现为"小时趋势图数据莫名变成分钟量级"。
"""
from __future__ import annotations

from typing import Optional

from app.utils.timeutil import align_bucket

# 桶粒度（E15 granularity）
GRANULARITIES: tuple[str, ...] = ("1m", "1h", "1d")
GRANULARITY_MS: dict[str, int] = {"1m": 60_000, "1h": 3_600_000, "1d": 86_400_000}

# 查询时间范围档位（N-11-5：原型只出现"近 24 小时"，其余为本模块取值）
# range -> (默认粒度, 期望点数)
RANGE_SPECS: dict[str, tuple[str, int]] = {
    "1h": ("1m", 60),
    "24h": ("1h", 24),
    "7d": ("1h", 168),
    "30d": ("1d", 30),
}
DEFAULT_RANGE = "24h"

# range 允许的粒度（BR-11-11：显式传入不匹配的粒度 -> MET-4002）
ALLOWED_GRANULARITY: dict[str, frozenset[str]] = {
    "1h": frozenset({"1m"}),
    "24h": frozenset({"1h"}),
    "7d": frozenset({"1h"}),
    "30d": frozenset({"1d"}),
}

# 趋势图点数上限（§3.3：超过则丢弃最旧并置 truncated）
MAX_TREND_POINTS = 1000

# 保留策略（BR-11-21）：1m 7 天、1h 90 天、1d 永久
RETENTION_MS: dict[str, Optional[int]] = {
    "1m": 7 * 86_400_000,
    "1h": 90 * 86_400_000,
    "1d": None,
}


def align(ts_ms: int, granularity: str) -> int:
    """把时间戳对齐到该粒度的桶起点。

    `1m`/`1h` 为自然对齐；**`1d` 按 `Asia/Shanghai` 00:00 对齐**（BR-11-02）——
    若按 UTC 整日对齐，日趋势图的切分点会落在北京时间早上 08:00，
    与所有人的直觉都不一致。对齐实现复用模块 00 的 `timeutil.align_bucket`，
    避免两处各写一份时区换算。
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"未知粒度：{granularity}")
    return align_bucket(ts_ms, granularity)


def bucket_id(bucket_type: str, bucket_key: str, granularity: str, bucket_ts: int) -> str:
    """BR-11-01 的桶主键。"""
    return f"{bucket_type}:{bucket_key}:{granularity}:{bucket_ts}"


def expire_at(granularity: str, bucket_ts: int) -> Optional[int]:
    """该桶的过期时刻（`None` = 永不过期）。

    **为什么不用「按 `bucket_ts` 的 TTL 索引」**：MongoDB 的 TTL 索引对**整个集合**
    只有一份 `expireAfterSeconds`，无法做到"`1m` 留 7 天、`1h` 留 90 天"。
    若按 `bucket_ts` 设 7 天，`1h` 桶会被一起清掉（它们的 `bucket_ts` 更旧），
    与 BR-11-21 自己声明的保留策略矛盾。
    因此改为**每条文档携带 `expire_at`**（写入时算好），TTL 索引建在 `expire_at`
    上、`expireAfterSeconds=0`；`1d` 桶不写该字段 → 永不过期。
    """
    keep = RETENTION_MS.get(granularity)
    return None if keep is None else bucket_ts + keep


def trend_points(range_: str, now_ms: int) -> list[tuple[int, bool]]:
    """趋势图的桶序列：`[(bucket_ts, partial)]`，**恰好 `points` 个**（§3.3）。

    以**当前桶**为末点向前取整数个桶，而不是"从 from_ts 向下取整再逐格迭代"——
    后者在 from_ts 未落在桶边界时会多出一个点（实测 24h 得到 25 个），
    与 §3.3 约定的 24/168/30 不一致，而"多一个点"在图上表现为首格半空，
    很难判断是数据问题还是取点问题。

    折线图必须"不断裂"，故空桶也要给出 0 值点：补零是**服务端**职责。
    `partial=True` 表示该桶尚未闭合（仅末点可能为真）。
    """
    if range_ not in RANGE_SPECS:
        raise ValueError(f"未知时间范围：{range_}")
    granularity, points = RANGE_SPECS[range_]
    step = GRANULARITY_MS[granularity]
    current = align(now_ms, granularity)
    starts = [current - (points - 1 - i) * step for i in range(points)]
    if len(starts) > MAX_TREND_POINTS:
        # 理论上不会触发（最大 168），保留兜底以防将来新增更长的档位
        starts = starts[-MAX_TREND_POINTS:]
    return [(ts, ts == current) for ts in starts]


def range_bounds(range_: str, now_ms: int) -> tuple[int, int, str]:
    """把 `range` 折算成查询用的 `[from_ts, to_ts]` 与实际粒度。

    `from_ts` 取**首个趋势桶的起点**（而不是 `now - span`）：这样"数据库过滤出来的桶"
    与"图上枚举的桶"严格是同一批，求和结果不会因为多算/少算一个桶而对不上。
    """
    points = trend_points(range_, now_ms)
    granularity = RANGE_SPECS[range_][0]
    return points[0][0], points[-1][0], granularity


def iter_bucket_starts(from_ts: int, to_ts: int, granularity: str, now_ms: int) -> list[tuple[int, bool]]:
    """按给定区间枚举桶（供 rollup 等需要任意区间的场景使用）。

    趋势图请用 `trend_points()`：那个方法保证点数与 §3.3 的约定一致。
    """
    step = GRANULARITY_MS[granularity]
    start = align(from_ts, granularity)
    current_bucket = align(now_ms, granularity)
    out: list[tuple[int, bool]] = []
    ts = start
    while ts <= to_ts:
        out.append((ts, ts == current_bucket))
        ts += step
    if len(out) > MAX_TREND_POINTS:
        out = out[-MAX_TREND_POINTS:]
    return out
