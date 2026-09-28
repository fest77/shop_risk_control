# -*- coding: utf-8 -*-
"""BR-11-01 / 02 / 21 / §3.3：桶键、三种粒度的对齐（含 Asia/Shanghai 日界）与趋势点数。

日界用例是本文件的重点：`1d` 若按 UTC 整日对齐，日趋势图的切分点会落在北京时间
早上 08:00——所有人都会觉得"昨天"算错了，但这种错只在跨日时暴露。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.engine.metric_bucket import (
    ALLOWED_GRANULARITY,
    DEFAULT_RANGE,
    GRANULARITY_MS,
    MAX_TREND_POINTS,
    RANGE_SPECS,
    RETENTION_MS,
    align,
    bucket_id,
    expire_at,
    iter_bucket_starts,
    range_bounds,
    trend_points,
)
from app.utils.timeutil import CN_TZ

pytestmark = pytest.mark.anyio


def cst(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
        second: int = 0, ms: int = 0) -> int:
    """东八区某个时刻的毫秒时间戳（用例里直接写北京时间，避免手算偏移）。"""
    return int(datetime(year, month, day, hour, minute, second, ms * 1000,
                        tzinfo=CN_TZ).timestamp() * 1000)


# ============================================================ BR-11-02 对齐
async def test_minute_and_hour_alignment_is_natural():
    """`1m`/`1h` 按自然时间向下取整。"""
    ts = cst(2026, 9, 23, 10, 44, 31, 208)
    assert align(ts, "1m") == cst(2026, 9, 23, 10, 44)
    assert align(ts, "1h") == cst(2026, 9, 23, 10)
    assert align(cst(2026, 9, 23, 10, 0, 0, 1), "1m") == cst(2026, 9, 23, 10)
    assert align(cst(2026, 9, 23, 10, 0, 0, 1), "1h") == cst(2026, 9, 23, 10)


async def test_day_alignment_uses_shanghai_midnight():
    """BR-11-02 / V-11-03：按**东八区 00:00** 对齐，23:59:59.999 与次日 00:00 分属两天。"""
    before = cst(2026, 9, 22, 23, 59, 59, 999)
    after = cst(2026, 9, 23, 0, 0, 0, 0)
    assert align(before, "1d") == cst(2026, 9, 22)
    assert align(after, "1d") == cst(2026, 9, 23)
    assert align(before, "1d") != align(after, "1d")
    # 同一北京日内的任意时刻都落在同一个日桶
    assert align(cst(2026, 9, 23, 23, 59, 59, 999), "1d") == cst(2026, 9, 23)


async def test_day_bucket_is_not_utc_midnight():
    """若按 UTC 整日对齐，北京 08:00 之前会被算进前一天——这条断言守住该回归。"""
    ts = cst(2026, 9, 23, 3, 0)                     # 北京 03:00 = UTC 前一日 19:00
    utc_aligned = ts - (ts % GRANULARITY_MS["1d"])
    assert align(ts, "1d") == cst(2026, 9, 23)
    assert align(ts, "1d") != utc_aligned
    # 日桶起点换算成 UTC 是**前一日 16:00**（即北京 00:00），而不是 UTC 00:00
    utc_dt = datetime.fromtimestamp(align(ts, "1d") / 1000, tz=timezone.utc)
    assert (utc_dt.year, utc_dt.month, utc_dt.day, utc_dt.hour) == (2026, 9, 22, 16)


async def test_align_rejects_unknown_granularity():
    with pytest.raises(ValueError):
        align(cst(2026, 9, 23), "5m")


# ============================================================ BR-11-01 桶键
async def test_bucket_id_contains_granularity_segment():
    """BR-11-01 / N-11-1：`_id` 必须带 `granularity`，否则整点时 1m/1h 桶互相覆盖。"""
    ts = cst(2026, 9, 23, 10)
    assert bucket_id("global", "all", "1m", ts) == f"global:all:1m:{ts}"
    assert bucket_id("global", "all", "1h", ts) != bucket_id("global", "all", "1m", ts)
    assert bucket_id("rule", "R001", "1d", ts) == f"rule:R001:1d:{ts}"


# ============================================================ BR-11-21 保留策略
async def test_expire_at_follows_retention_table():
    """BR-11-21：`1m` 7 天、`1h` 90 天、`1d` 永久（`None` -> 不写字段）。"""
    ts = cst(2026, 9, 23)
    day = 86_400_000
    assert RETENTION_MS == {"1m": 7 * day, "1h": 90 * day, "1d": None}
    assert expire_at("1m", ts) == ts + 7 * day
    assert expire_at("1h", ts) == ts + 90 * day
    assert expire_at("1d", ts) is None


# ============================================================ §3.3 趋势点数
@pytest.mark.parametrize(
    "range_,points,granularity",
    [("1h", 60, "1m"), ("24h", 24, "1h"), ("7d", 168, "1h"), ("30d", 30, "1d")],
)
async def test_trend_point_counts_match_spec(range_, points, granularity):
    """§3.3 的条数约定：1h→60、24h→24、7d→168、30d→30。"""
    now = cst(2026, 9, 23, 10, 44, 31, 208)
    series = trend_points(range_, now)
    assert len(series) == points
    assert RANGE_SPECS[range_] == (granularity, points)
    # 恰好一个 point，且步长严格等于该粒度的毫秒数
    step = GRANULARITY_MS[granularity]
    assert [ts for ts, _ in series] == sorted(ts for ts, _ in series)
    assert all(b - a == step for a, b in zip([ts for ts, _ in series], [ts for ts, _ in series][1:]))
    # 末点是当前桶（partial），其余都不是
    assert [p for _ts, p in series] == [False] * (points - 1) + [True]
    assert series[-1][0] == align(now, granularity)


async def test_range_bounds_matches_trend_points():
    """BR-11-02 的配套：库里过滤的桶与图上枚举的桶必须是同一批。"""
    now = cst(2026, 9, 23, 10, 44, 31, 208)
    for range_ in RANGE_SPECS:
        series = trend_points(range_, now)
        from_ts, to_ts, granularity = range_bounds(range_, now)
        assert (from_ts, to_ts) == (series[0][0], series[-1][0])
        assert granularity == RANGE_SPECS[range_][0]


async def test_default_range_and_allowed_granularity_are_consistent():
    """`ALLOWED_GRANULARITY` 必须与 `RANGE_SPECS` 的默认粒度一致（两处漂移=接口自相矛盾）。"""
    assert DEFAULT_RANGE == "24h"
    for range_, (granularity, _points) in RANGE_SPECS.items():
        assert granularity in ALLOWED_GRANULARITY[range_]
    assert MAX_TREND_POINTS == 1000


# ============================================================ 任意区间枚举（rollup 用）
async def test_iter_bucket_starts_enumerates_arbitrary_window():
    now = cst(2026, 9, 23, 10, 44, 31, 208)
    start = cst(2026, 9, 23, 8)
    end = cst(2026, 9, 23, 10, 30)
    starts = iter_bucket_starts(start, end, "1h", now)
    assert [ts for ts, _ in starts] == [cst(2026, 9, 23, 8), cst(2026, 9, 23, 9),
                                        cst(2026, 9, 23, 10)]
    # 未闭合的那个小时被标记为 partial（rollup 由此可以判断"要不要重算当前小时"）
    assert [p for _ts, p in starts] == [False, False, True]
