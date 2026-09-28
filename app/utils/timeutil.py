# -*- coding: utf-8 -*-
"""时间工具：毫秒时间戳、时间桶对齐、滑动窗口计算（模块 00 §6）。

**为什么统一**：原先 `now_ms()` 在 `core/degraded.py` 与 `list_service.py`
各写一份，属于重复实现（改一处漏一处）；更危险的是"时间桶对齐"若各模块
各写一版，大盘（模块 02）与指标写入（模块 11）会算出**错开的桶**，表现为
图表上出现半空柱子——这类缺陷只在聚合边界暴露，极难排查。

**坐标约定**：全项目时间一律用 **UTC 毫秒时间戳**（int），与 E01/E03/E07 的
`ts` 字段一致；仅在"按天切桶/生成编号"时按 **Asia/Shanghai** 换算日历日，
避免演示环境（UTC+8）在 08:00 之前把事件算到前一天。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# 演示与验收均在东八区；按天分桶/生成业务编号必须用本地日历日
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_ms() -> int:
    """当前时间的毫秒时间戳（UTC 基线）。"""
    return int(time.time() * 1000)


def to_dt(ms: int) -> datetime:
    """毫秒时间戳 -> 东八区 datetime。"""
    return datetime.fromtimestamp(ms / 1000, tz=CN_TZ)


def date_key(ms: int) -> str:
    """业务编号用的日期段 `yyyyMMdd`（东八区日历日）。"""
    return to_dt(ms).strftime("%Y%m%d")


def time_key(ms: int) -> str:
    """trace_id 用的时间段 `HHmmss`（东八区）。"""
    return to_dt(ms).strftime("%H%M%S")


def iso(ms: int) -> str:
    """ISO8601（带东八区偏移），用于 `/health` 的 `time` 与前端展示。"""
    return to_dt(ms).isoformat()


def align_bucket(ms: int, granularity: str) -> int:
    """把时间戳对齐到桶起点，供 E15 `metric_buckets` 增量聚合使用。

    对齐规则与 E15 的 `granularity` 取值一一对应：
    - `1m`：向下取整到分钟
    - `1h`：向下取整到小时
    - `1d`：向下取整到**东八区**当日 00:00（不是 UTC 00:00，
      否则大盘的"今日"会比业务方认知早 8 小时）
    """
    if granularity == "1m":
        return ms - (ms % 60_000)
    if granularity == "1h":
        return ms - (ms % 3_600_000)
    if granularity == "1d":
        dt = to_dt(ms)
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp() * 1000)
    raise ValueError(f"未知粒度 granularity={granularity!r}，仅支持 1m / 1h / 1d")


def window_start(ms: int, minutes: int) -> int:
    """滑动窗口起点：`ms` 往前 `minutes` 分钟。用于 G-02 的 60/1440 分钟窗口。"""
    return ms - minutes * 60_000


def day_range(ms: int) -> tuple[int, int]:
    """东八区当日 `[00:00, 次日00:00)` 的毫秒区间，供"今日"类查询使用。"""
    dt = to_dt(ms)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def parse_iso(value: str) -> int:
    """ISO8601 -> 毫秒时间戳。前端传时间字符串时使用（缺失时区按东八区解释）。"""
    s = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return int(dt.timestamp() * 1000)
