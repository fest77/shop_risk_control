# -*- coding: utf-8 -*-
"""窗口清理 + 统计基线刷新的定时任务（模块 04 BR-04-13 / 决策 D9）。

## 两个后台协程，两件不同的事

| 协程 | 节奏 | 做什么 |
|---|---|---|
| sweep | 每 60 秒（BR-04-13） | 丢弃超出长窗的条目、清理空键、推进重试队列 |
| baseline | 每日刷新（D9） | 从历史快照重算 E21 的 P50/P95 |

放在同一个调度器里而不是两个模块：它们共享同一个窗口对象与同一份"运行统计"，
拆开之后"这个进程现在有哪些后台任务"就要去两个地方看。

## 为什么 sweep 失败只记日志、下一轮重试

定时任务**没有调用方**可以返回错误码（不像 HTTP 接口）。sweep 失败的唯一后果是
"过期数据多留 60 秒"，而比它更糟的处置是让后台协程退出——此后**永不清理**，
窗口内存只增不减，最终把决策链路拖死。因此采用"本轮放弃 + 下轮重来"，
与 `core/metric_rollup.py`、`core/list_cleanup_task.py` 保持同一处置风格。

## 为什么统计基线的 `segment` 固定为 `"all"`

E21 的 `segment` 设计是"分组维度，当前按 `user_level`；预留 `province` 等"。
但**按 `user_level` 分组需要用户等级数据，而它归模块 09（画像图谱）**，
本项目 09 尚未落地。当前快照里虽然带了 `user_level` 特征值，用它可以按等级
分组，但那样会得到"等级也可能缺失"的混合分段：冷启动用户的快照会掉进
一个既不是 `normal` 也不是任何真实等级的空档里，而审核页按等级查基线时
会落空。**宁可先只给一个诚实的 `all` 分段**，也不要产出一批分段边界
说不清的数据。**报告里已登记为待 09 落地后扩展为 `user_level` 分组。**

## `sample_size < 100` 为什么"删掉这条基线"而不是标记不可用

E21 的编码约束是"查不到基线时界面展示 —"。若保留一条 `available=false`
的记录，读取方就必须判断两个字段，只要有一个调用方忘了判，界面上就会出现
一条**过期的 P95**。删掉之后"没有可用基线"只有一种表达（见
`feature_repo.delete_baselines` 的说明）。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Optional

from app.engine.feature_compute import FEATURE_DATA_TYPES, FEATURE_KEYS
from app.logging import get_logger
from app.repos.feature_repo import (
    DEFAULT_BASELINE_WINDOW_DAYS,
    MAX_BASELINE_SAMPLES,
    MIN_BASELINE_SAMPLE,
    FeatureRepo,
    percentile,
)
from app.utils.timeutil import now_ms, to_dt

log = get_logger("shop_risk_control.feature.sweep")

#: sweep 周期（BR-04-13：每 60 秒）
SWEEP_INTERVAL_SEC = 60.0

#: 统计基线的每日刷新时刻（Asia/Shanghai 凌晨 3:00）。
#:
#: 为什么选凌晨而不是 00:05：基线读的是"最近 7 天"的快照，凌晨 3 点时
#: 当天的数据已经积累了一小部分、但基数很小；真正的目的是**避开访问高峰**，
#: 因为刷新要扫描几万条快照。这与模块 11 的日桶补算（00:05）错开也避免了
#: 两个重活同时压在库上。
BASELINE_HOUR = 3
BASELINE_MINUTE = 0

#: 分段键。见模块 docstring：09 落地前固定为 `all`，不伪造 `user_level` 分组。
BASELINE_SEGMENT = "all"


def seconds_until(now: int, hour: int, minute: int) -> float:
    """距离**下一个** Asia/Shanghai `hour:minute` 的秒数（与 metric_rollup 同法）。

    按东八区算而不是 UTC：基线的 `window_days` 是"业务日"概念，
    调度时区与统计窗口的时区若不一致，会出现"刚刷新完又发现有新数据"的反复。
    """
    dt = to_dt(now)
    target = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= dt:
        target += timedelta(days=1)
    return max(1.0, (target - dt).total_seconds())


def _repo() -> FeatureRepo:
    """每次取新句柄：测试会切换数据库，缓存句柄会写错库。"""
    from app import db as db_module

    return FeatureRepo(db_module.get_db())


def _service():
    """取特征服务单例（延迟导入，避免 core ↔ services 的循环导入）。"""
    from app.services.feature_service import get_feature_service

    return get_feature_service()


def _numeric_samples(rows: list[dict]) -> dict[str, list[float]]:
    """从快照里抽出**可做分位统计**的特征样本。

    排除三类：
    - **布尔**（`ip_is_proxy`）：真假值的"P95"没有意义；
    - **字符串**（`user_level`）：类别不能比大小；
    - **非数值/缺失**：`None`、缺键、类型不符。

    用 `FEATURE_DATA_TYPES` 判定而不是硬编码特征名：将来新增一个数值特征时
    统计会自动覆盖它，不需要记得回来改这里。
    """
    samples: dict[str, list[float]] = {k: [] for k in FEATURE_KEYS}
    for row in rows:
        features = row.get("features")
        if not isinstance(features, dict):
            continue
        for key, bucket in samples.items():
            if FEATURE_DATA_TYPES.get(key) not in ("int", "float"):
                continue
            value = features.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                bucket.append(float(value))
            except (TypeError, ValueError):
                continue
    return samples


async def refresh_baselines(
    now: Optional[int] = None,
    *,
    window_days: int = DEFAULT_BASELINE_WINDOW_DAYS,
) -> dict[str, Any]:
    """重算 E21 统计基线（每日一次，幂等）。返回统计摘要。

    样本不足（`< MIN_BASELINE_SAMPLE`）的特征**不写入、并删除同名的旧记录**——
    见模块 docstring 的最后一段。
    """
    moment = now_ms() if now is None else int(now)
    since = moment - window_days * 24 * 60 * 60 * 1000
    result: dict[str, Any] = {
        "computed_at": moment,
        "window_days": window_days,
        "segment": BASELINE_SEGMENT,
        "scanned": 0,
        "truncated": False,
        "written": 0,
        "unavailable": [],
        "removed": 0,
        "error": None,
    }
    repo = _repo()
    try:
        rows, truncated = await repo.count_scanned(since, MAX_BASELINE_SAMPLES)
    except Exception as e:  # noqa: BLE001 - 定时任务只记日志，下一轮重试
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning("[FEA-5002] 统计基线刷新失败（读取快照），下一轮重试：%s", e)
        return result

    result["scanned"] = len(rows)
    result["truncated"] = truncated
    if truncated:
        # 如实汇报：本次分位只基于最近 N 条，不假装算的是全量
        log.warning("统计基线样本超过上限 %d 条，本次只统计最近 %d 条（结果仍可用，但非全量）",
                    MAX_BASELINE_SAMPLES, MAX_BASELINE_SAMPLES)

    samples = _numeric_samples(rows)
    docs: list[dict] = []
    unavailable: list[str] = []
    for key in FEATURE_KEYS:
        if FEATURE_DATA_TYPES.get(key) not in ("int", "float"):
            # 布尔/字符串特征本就不该有统计基线，也不删除旧记录之外的任何东西
            unavailable.append(key)
            continue
        values = samples.get(key) or []
        if len(values) < MIN_BASELINE_SAMPLE:
            unavailable.append(key)
            continue
        docs.append({
            "_id": repo.baseline_id(key, BASELINE_SEGMENT),
            "feature_name": key,
            "segment": BASELINE_SEGMENT,
            "p50": percentile(values, 0.5),
            "p95": percentile(values, 0.95),
            "sample_size": len(values),
            "window_days": window_days,
            "computed_at": moment,
        })

    try:
        result["written"] = await repo.upsert_baselines(docs)
        stale = [
            repo.baseline_id(key, BASELINE_SEGMENT) for key in unavailable
        ]
        result["removed"] = await repo.delete_baselines(stale)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning("[FEA-5002] 统计基线写入失败，下一轮重试：%s", e)
        return result

    result["unavailable"] = unavailable
    log.info(
        "统计基线已刷新 segment=%s：扫描 %d 条快照，写入 %d 项，"
        "不可用（样本<%d 或非数值）%d 项",
        BASELINE_SEGMENT, result["scanned"], result["written"],
        MIN_BASELINE_SAMPLE, len(unavailable),
    )
    return result


async def sweep_once(now: Optional[int] = None) -> dict[str, Any]:
    """执行一轮窗口清理 + 重试队列推进，返回统计摘要。

    重试队列搭在这里的原因：它需要一个**周期性的、无调用方的**执行时机，
    而 sweep 恰好每 60 秒跑一次；单独起一个协程只为重试几条写失败的快照
    不划算（任务越多，"进程里到底跑着什么"越难看清）。
    """
    moment = now_ms() if now is None else int(now)
    service = _service()
    result: dict[str, Any] = {
        "swept_at": moment, "dropped": 0, "retried": 0,
        "memory_warned": False, "error": None,
    }
    try:
        result["dropped"] = await service.window.sweep(moment)
        result["retried"] = await service.retry_pending()
        result["memory_warned"] = service.window.memory_warned()
        if result["memory_warned"]:
            # BR-04-15：单进程内存超阈值必须告警，而不是等它把进程撑爆
            stats = service.window.stats()
            log.warning(
                "[FEA-5003] 特征窗口内存估算 %.1fMB 已超过告警线 %.1fMB"
                "（条目 %d，键 %d）——考虑缩短长窗口或降低容量上限",
                stats["estimated_bytes"] / 1048576,
                stats["memory_warn_bytes"] / 1048576,
                stats["total_entries"], stats["distinct_keys"],
            )
        if result["dropped"]:
            log.info("特征窗口 sweep 完成：丢弃 %d 条超出长窗的条目", result["dropped"])
    except Exception as e:  # noqa: BLE001 - 单轮异常绝不能终止整个循环
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning("特征窗口 sweep 单轮异常，下一轮重试：%s", e)
    return result


class FeatureSweepScheduler:
    """两个后台协程：每 60 秒 sweep；每日刷新统计基线。

    结构照 `core/metric_rollup.RollupScheduler`：停机用 `asyncio.Event`
    而不是裸 `asyncio.sleep`——否则 `stop()` 之后还要等最长一个完整周期
    才能退出，进程关不掉。
    """

    def __init__(
        self,
        *,
        interval_sec: float = SWEEP_INTERVAL_SEC,
        baseline_hour: int = BASELINE_HOUR,
        baseline_minute: int = BASELINE_MINUTE,
    ) -> None:
        self.interval_sec = interval_sec
        self.baseline_hour = baseline_hour
        self.baseline_minute = baseline_minute
        self._tasks: list[asyncio.Task] = []
        self._stop: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stats: dict[str, int] = {
            "sweep_runs": 0, "baseline_runs": 0, "failures": 0,
            "dropped": 0, "retried": 0,
        }

    def _ensure_loop(self) -> None:
        """把停机事件绑定到当前循环（测试逐用例新建循环，参见 AuditService）。"""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._tasks = []
            self._stop = None

    async def start(self) -> None:
        """启动两个后台协程（**幂等**：已在跑则不重复启动）。

        第一个 sweep 周期**立即**跑一轮（不等 60 秒）：覆盖"停机期间攒下的
        过期数据"，也让刚启动时的内存占用尽快回到真实水平。
        """
        self._ensure_loop()
        if any(not t.done() for t in self._tasks):
            return
        self._stop = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._sweep_loop(), name="feature-sweep"),
            asyncio.create_task(self._baseline_loop(), name="feature-baseline"),
        ]
        log.info("特征窗口定时任务已启动（sweep 每 %.0fs；统计基线每日 %02d:%02d Asia/Shanghai）",
                 self.interval_sec, self.baseline_hour, self.baseline_minute)

    async def stop(self) -> None:
        """停止后台协程（**幂等**：没在跑时调用是安全的空操作）。"""
        self._ensure_loop()
        stop = self._stop
        if stop is not None:
            stop.set()
        tasks = self._tasks
        self._tasks = []
        self._stop = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if tasks:
            log.info("特征窗口定时任务已停止")

    async def _sleep(self, seconds: float) -> None:
        """可被 `stop()` 立刻打断的等待。"""
        if self._stop is None:  # pragma: no cover - start() 之前不会进入循环
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    async def _sweep_loop(self) -> None:
        while True:
            try:
                result = await sweep_once()
                self.stats["sweep_runs"] += 1
                self.stats["dropped"] += int(result["dropped"] or 0)
                self.stats["retried"] += int(result["retried"] or 0)
                if result["error"]:
                    self.stats["failures"] += 1
            except Exception as e:  # noqa: BLE001 - 单轮异常绝不能终止整个循环
                self.stats["failures"] += 1
                log.warning("特征窗口 sweep 单轮异常，下一轮重试：%s", e)
            if self._stopping():
                return
            await self._sleep(self.interval_sec)
            if self._stopping():
                return

    async def _baseline_loop(self) -> None:
        """先立即算一次（演示启动后就有基线，不必等到凌晨 3 点），再每日刷新。"""
        while True:
            try:
                await refresh_baselines()
                self.stats["baseline_runs"] += 1
            except Exception as e:  # noqa: BLE001
                self.stats["failures"] += 1
                log.warning("统计基线刷新异常，下一轮重试：%s", e)
            if self._stopping():
                return
            await self._sleep(seconds_until(now_ms(), self.baseline_hour, self.baseline_minute))
            if self._stopping():
                return


_SCHEDULER = FeatureSweepScheduler()


def get_sweep_scheduler() -> FeatureSweepScheduler:
    """取进程内调度器单例（由 `app/main.py` 的 lifespan 装配）。"""
    return _SCHEDULER


async def start_sweep() -> None:
    """启动 sweep 与基线刷新（lifespan 启动阶段调用；重复调用幂等）。"""
    await _SCHEDULER.start()


async def stop_sweep() -> None:
    """停止后台协程（lifespan 关闭阶段调用；未启动时调用安全）。"""
    await _SCHEDULER.stop()


__all__ = [
    "BASELINE_HOUR",
    "BASELINE_MINUTE",
    "BASELINE_SEGMENT",
    "FeatureSweepScheduler",
    "SWEEP_INTERVAL_SEC",
    "get_sweep_scheduler",
    "refresh_baselines",
    "seconds_until",
    "start_sweep",
    "stop_sweep",
    "sweep_once",
]
