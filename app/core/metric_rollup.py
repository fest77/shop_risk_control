# -*- coding: utf-8 -*-
"""定时 / 手动 rollup：`1m` 桶 -> `1h` / `1d` 桶（模块 11 §3.8、BR-11-15/16）。

## 为什么 rollup 必须用 `$set` 覆盖而不是 `$inc`

实时写入（`record_decision`）是"每条决策 +1"，天然增量；rollup 是"把一段时间的
`1m` 桶重新加总一遍"，它的正确性判据是**幂等**：同一个时间窗连跑 3 次，结果必须
完全一致（V-11-09）。若也用 `$inc`，第二次执行就把数值翻倍——大盘会在整点后
"自动涨一倍"，而且只在跨过整点时出现，极难复现。因此这里用 `$set` 整体替换
`metrics` 子文档（BR-11-15）。

## 为什么"空窗口不写桶"

目标窗口在 `1m` 桶里查不到任何数据时**跳过**，而不是写一个全 0 的桶。两个理由：

1. 空桶会被趋势图当作真实数据点；更重要的是，`1m` 桶有 7 天 TTL，而 `1h` 有
   90 天——7 天后再跑一次历史 rollup，若"空窗口写 0"，就会把三个月前**本来正确**
   的小时桶覆盖成全 0，历史数据被静默抹掉。跳过则保住已有结果。
2. 存储：30 天 × 24 小时 × 四维度，若为每个不存在的维度都物化空桶，桶数量会
   按维度数（而不是按真实流量）膨胀。

## 为什么只读 `1m` 桶

BR-11-15 明确"只读 `1m` 桶，不读 `decisions`"（AD-03）。因此 rollup **不能**
修复"写入时漏计"的问题，它只能把已有的 `1m` 桶重新加总——这一点必须如实写在
文档里，否则运维会误以为"点一下补算就把丢的数据补回来了"。
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, Optional

from app.engine.metric_agg import HIST_KEYS, SAVED_AMOUNT_TYPES
from app.engine.metric_bucket import (
    GRANULARITY_MS,
    align,
    bucket_id,
    expire_at,
    iter_bucket_starts,
)
from app.engine.metric_percentile import total_count
from app.errors import AppError
from app.logging import get_logger
from app.repos.metric_repo import MetricRepo
from app.services.metric_query import COUNT_FIELDS, HIT_FIELDS, SCORE_FIELDS
from app.services.metric_service import (
    met_error,
    require_dimension,
    require_rollup_granularity,
    require_rollup_window,
)
from app.utils.timeutil import now_ms, to_dt

log = get_logger("shop_risk_control.metric.rollup")

# 各维度桶原样携带的字段集合（按 §3.10 的"实际写入字段"列区分）。
# 聚合时必须把全部字段都求出来（一次 `$group` 拿全），但**只把该维度该有的字段
# 写进目标桶**：否则 `rule` 桶里会凭空出现 `event_cnt: 0`，与 E15 的结构不成立。
_NON_RULE_FIELDS: tuple[str, ...] = COUNT_FIELDS + SCORE_FIELDS + (
    "estimated_saved_amount",
    *(f"saved_amount_by_type.{t}" for t in SAVED_AMOUNT_TYPES),
)
_GLOBAL_FIELDS: tuple[str, ...] = _NON_RULE_FIELDS + (
    "elapsed_sum", "elapsed_cnt", *(f"elapsed_hist.{k}" for k in HIST_KEYS),
)
_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "global": _GLOBAL_FIELDS,
    "level": _NON_RULE_FIELDS,
    "scene": _NON_RULE_FIELDS,
    "rule": HIT_FIELDS,
}
_ALL_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys([*_GLOBAL_FIELDS, *HIT_FIELDS])
)

# 单次 rollup 的目标桶上限：运维接口若被误传"一年 + 1h"，会变成 8760 次聚合，
# 把请求挂死并给库带来无谓压力。超过即拒绝并提示缩小窗口（MET-4005）。
MAX_TARGET_BUCKETS = 1000

# 定时任务的默认节奏（BR-11-15）：每 60s 重算上一个已闭合 1h 桶；
# 每日 00:05（Asia/Shanghai）重算前一日 1d 桶
HOURLY_INTERVAL_SEC = 60.0
DAILY_HOUR = 0
DAILY_MINUTE = 5


def _repo() -> MetricRepo:
    """每次操作重新取数据库句柄（测试会切换库，缓存句柄会写错地方）。"""
    from app import db as db_module

    return MetricRepo(db_module.get_db())


def target_bucket_starts(
    from_ts: int, to_ts: int, granularity: str, now: Optional[int] = None,
) -> list[int]:
    """枚举该窗口内需要重算的目标桶起点（复用引擎的 `iter_bucket_starts`）。

    这里必须**自己再算一遍期望条数**并比对：`iter_bucket_starts` 为了防止趋势图
    点数失控，超过 1000 会**丢弃最旧**的桶。对趋势图那是可接受的降级，对 rollup
    却是"静默漏算一段历史"——因此超过上限时宁可直接拒绝，也不接受被截断的列表。
    """
    moment = now if now is not None else now_ms()
    starts = iter_bucket_starts(from_ts, to_ts, granularity, moment)
    step = GRANULARITY_MS[granularity]
    expected = (align(to_ts, granularity) - align(from_ts, granularity)) // step + 1
    if expected > MAX_TARGET_BUCKETS:
        raise met_error(
            "MET-4005",
            f"rollup 时间区间过大：需要重算 {expected} 个 {granularity} 桶，"
            f"上限 {MAX_TARGET_BUCKETS}，请缩小窗口",
            {"expected_buckets": expected, "max_buckets": MAX_TARGET_BUCKETS},
        )
    if len(starts) != expected:  # pragma: no cover - 防御引擎侧上限变化
        raise met_error("MET-4005", f"rollup 桶枚举被截断（期望 {expected}，实得 {len(starts)}）")
    return [ts for ts, _partial in starts]


async def rollup(
    granularity: Any, from_ts: Any, to_ts: Any, dimension: Any = "all",
) -> dict:
    """把 `[from_ts, to_ts]` 内的 `1m` 桶重新加总成 `1h` / `1d` 桶（幂等）。

    签名与 §3.8 的请求体逐字对应（允许位置参数，便于运维脚本直接调用）；
    返回 `{upserted, scanned_1m_buckets, elapsed_ms}`。
    """
    started = time.perf_counter()
    gran = require_rollup_granularity(granularity)
    dim = require_dimension(dimension)
    start, end = require_rollup_window(from_ts, to_ts)
    bucket_types = None if dim == "all" else (dim,)
    step = GRANULARITY_MS[gran]
    now = now_ms()
    starts = target_bucket_starts(start, end, gran, now)

    repo = _repo()
    upserted = 0
    scanned = 0
    try:
        for target_ts in starts:
            # 一个目标桶 = 一次聚合：`$match` 用 ix_dim_ts 的前缀（granularity + bucket_ts），
            # 文档数由时间窗决定，但绝不把 1m 级别的原始桶拉进 Python
            rows = await repo.sum_by_dimension(
                granularity="1m",
                from_ts=target_ts,
                to_ts=target_ts + step - 1,
                fields=_ALL_FIELDS,
                bucket_types=bucket_types,
            )
            if not rows:
                continue  # 空窗口不写桶，见模块 docstring 的第 1 条理由
            docs = [
                _target_doc(gran, target_ts, row, now) for row in rows
            ]
            upserted += await repo.overwrite_many(docs)
            # 只统计真正参与聚合的源桶数：调用方据此判断"这次补算覆盖了多少数据"
            scanned += sum(int(row.get("scanned") or 0) for row in rows)
    except AppError as e:
        if e.code != "MET-5001":
            raise  # 参数类错误原样返回（MET-4002/4004/4005）
        # 依赖类失败统一落到 MET-5006（§5：手动 rollup 失败 -> 500）
        raise met_error("MET-5006", f"指标补算执行失败：{e.message}", e.data) from e
    except Exception as e:  # noqa: BLE001 - 兜底：任何未归类异常都必须是 MET-5006 而不是 500 裸栈
        raise met_error("MET-5006", f"指标补算执行失败：{type(e).__name__}: {e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("rollup 完成 granularity=%s dimension=%s buckets=%d scanned_1m=%d upserted=%d 耗时=%dms",
             gran, dim, len(starts), scanned, upserted, elapsed_ms)
    return {"upserted": upserted, "scanned_1m_buckets": scanned, "elapsed_ms": elapsed_ms}


def _target_doc(granularity: str, bucket_ts: int, row: dict, now: int) -> dict:
    """把一行的聚合结果翻译成目标桶文档（字段按维度裁剪）。"""
    bucket_type = str(row["bucket_type"])
    bucket_key = str(row["bucket_key"])
    fields = _FIELDS_BY_TYPE.get(bucket_type, _NON_RULE_FIELDS)
    metrics: dict[str, Any] = {}
    hist: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for field in fields:
        value = int(row.get(field) or 0)
        if field.startswith("saved_amount_by_type."):
            by_type[field.split(".", 1)[1]] = value
        elif field.startswith("elapsed_hist."):
            hist[field.split(".", 1)[1]] = value
        else:
            metrics[field] = value
    if by_type:
        metrics["saved_amount_by_type"] = by_type
    # 直方图整块只在**确实有样本**时写入：全 0 的直方图会让
    # "elapsed_hist 缺失 -> p95=null"（MET-5003）这条语义失去可判定的载体
    if total_count(hist) > 0:
        metrics["elapsed_hist"] = hist
    return {
        "_id": bucket_id(bucket_type, bucket_key, granularity, bucket_ts),
        "bucket_type": bucket_type,
        "bucket_key": bucket_key,
        "granularity": granularity,
        "bucket_ts": bucket_ts,
        # BR-11-21：1h 留 90 天、1d 永久（expire_at 为 None 即不写该字段）
        "expire_at": expire_at(granularity, bucket_ts),
        "metrics": metrics,
        "created_at": now,
    }


# ============================================================
# 定时任务（BR-11-15）
# ============================================================
def seconds_until(now: int, hour: int, minute: int) -> float:
    """距离**下一个** Asia/Shanghai `hour:minute` 的秒数（用于每日补算）。

    按东八区算而不是 UTC：日桶本身就是按东八区 00:00 对齐的（BR-11-02），
    调度时区与分桶时区若不一致，日桶会在"刚算完又有新数据"的窗口里反复重算。
    """
    dt = to_dt(now)
    target = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= dt:
        target += timedelta(days=1)
    return max(1.0, (target - dt).total_seconds())


class RollupScheduler:
    """两个后台协程：整点后重算上一个 `1h` 桶；每日 00:05 重算前一日 `1d` 桶。

    失败**只记日志**（MET-5006）：定时任务没有调用方可返回错误，且补算失败
    不影响查询（查询仍返回已有桶），因此"本轮放弃、下一轮重试"是唯一合理的处置。
    """

    def __init__(
        self,
        *,
        interval_sec: float = HOURLY_INTERVAL_SEC,
        daily_hour: int = DAILY_HOUR,
        daily_minute: int = DAILY_MINUTE,
    ):
        self.interval_sec = interval_sec
        self.daily_hour = daily_hour
        self.daily_minute = daily_minute
        self._tasks: list[asyncio.Task] = []
        self._stop: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stats: dict[str, int] = {"hourly_runs": 0, "daily_runs": 0, "failures": 0}

    def _ensure_loop(self) -> None:
        """把停机事件绑定到当前循环（测试逐用例新建循环，参见 AuditService）。"""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._tasks = []
            self._stop = None

    async def start(self) -> None:
        """启动两个后台协程（幂等：已在跑则不重复启动）。"""
        self._ensure_loop()
        if any(not t.done() for t in self._tasks):
            return
        self._stop = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._hourly_loop(), name="metric-rollup-1h"),
            asyncio.create_task(self._daily_loop(), name="metric-rollup-1d"),
        ]
        log.info("指标 rollup 定时任务已启动（1h 每 %.0fs；1d 每日 %02d:%02d Asia/Shanghai）",
                 self.interval_sec, self.daily_hour, self.daily_minute)

    async def stop(self) -> None:
        """停止后台协程（取消即可：每轮之间没有需要落盘的中间状态）。"""
        self._ensure_loop()
        stop = self._stop
        if stop is not None:
            stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        self._stop = None
        log.info("指标 rollup 定时任务已停止")

    async def _sleep(self, seconds: float) -> None:
        """可被 `stop()` 立刻打断的等待（用 Event 而不是 `asyncio.sleep`）。"""
        if self._stop is None:  # pragma: no cover - start() 之前不会进入循环
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _hourly_loop(self) -> None:
        while not (self._stop and self._stop.is_set()):
            await self._sleep(self.interval_sec)
            if self._stop and self._stop.is_set():
                return
            try:
                await self.run_hourly_once()
            except Exception as e:  # noqa: BLE001 - 放弃本轮，下一轮重试（MET-5006）
                self.stats["failures"] += 1
                log.warning("[MET-5006] 1h 补算失败，下一轮重试：%s", e)

    async def _daily_loop(self) -> None:
        while not (self._stop and self._stop.is_set()):
            await self._sleep(seconds_until(now_ms(), self.daily_hour, self.daily_minute))
            if self._stop and self._stop.is_set():
                return
            try:
                await self.run_daily_once()
            except Exception as e:  # noqa: BLE001
                self.stats["failures"] += 1
                log.warning("[MET-5006] 1d 补算失败，下一轮重试：%s", e)

    async def run_hourly_once(self, now: Optional[int] = None) -> dict:
        """重算**上一个已闭合**的 `1h` 桶（当前这个小时还在累积，不重算）。"""
        moment = now if now is not None else now_ms()
        step = GRANULARITY_MS["1h"]
        target = align(moment, "1h") - step
        result = await rollup(granularity="1h", from_ts=target, to_ts=target + step - 1)
        self.stats["hourly_runs"] += 1
        return result

    async def run_daily_once(self, now: Optional[int] = None) -> dict:
        """重算**前一日**的 `1d` 桶（东八区日界）。"""
        moment = now if now is not None else now_ms()
        step = GRANULARITY_MS["1d"]
        target = align(moment, "1d") - step
        result = await rollup(granularity="1d", from_ts=target, to_ts=target + step - 1)
        self.stats["daily_runs"] += 1
        return result


_SCHEDULER = RollupScheduler()


def get_rollup_scheduler() -> RollupScheduler:
    """取进程内调度器单例（由 `app/main.py` 的 lifespan 装配）。"""
    return _SCHEDULER


async def start_rollup() -> None:
    """启动定时补算（lifespan 启动阶段调用）。"""
    await _SCHEDULER.start()


async def stop_rollup() -> None:
    """停止定时补算（lifespan 关闭阶段调用）。"""
    await _SCHEDULER.stop()


__all__ = [
    "MAX_TARGET_BUCKETS", "RollupScheduler", "get_rollup_scheduler", "rollup",
    "seconds_until", "start_rollup", "stop_rollup", "target_bucket_starts",
]
