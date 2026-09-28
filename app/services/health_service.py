# -*- coding: utf-8 -*-
"""吞吐与接口健康度服务（模块 13 §4.4 / `hcCard`）。

## 一条硬约束：**不自己聚合任何指标**

BR-13-25 写得非常直白：「`decision_p95_ms` 来源于 11 的运行时统计（或进程内
滚动窗口），**不在本模块自己聚合**」。因此本服务里的每个数字都有唯一来源：

| 字段 | 来源 | 说明 |
|---|---|---|
| `qps` | 11 的 `/metrics/throughput` → `qps_1m` | "近 1 分钟事件 QPS"，与大盘同一口径 |
| `decision_p50/p95/p99_ms` | 同一个直方图 `elapsed_hist` + 11 的 `percentile()` | p95 直接取 11 算好的 `p95_elapsed_ms`；p50/p99 用**同一个近似函数**、**同一份直方图**算，不另建口径 |
| `list_cache_hit_rate` | 05 的 `LIST_CACHE.stats()`（hits/misses） | 名单缓存的真实命中率，不是估算 |
| `window` | 04 的 `FeatureWindow.stats()` | 长/短窗口、条目数、内存估算、上次 sweep |
| `components` | `app/tasks/health_probe.py` | 逐组件 ≤1s 超时（BR-13-28） |
| `mongo` | `db.ping()` | 与 `/health` 同源 |

## 缺指标 / 缺样本时**返回 `null`**，而不是 0

BR-11-08 的原话是「没有样本时返回 0 会让大盘显示"P95 延迟 0ms"，被误读成
"系统飞快"」。同一原则适用于本卡片：`list_cache_hit_rate` 在没有查询样本时
返回 `null`，让页面显示「—」。0% 会被 §2.2 的配色规则判成橙色告警，
而"还没有人查过名单"不是故障。

## 与 `/health` 的分工（BR-13-29）

`/health` 是**存活探针**（轻量、无鉴权、供监控系统高频调用）；
`/system/stats` 是**运维看板**（含统计、需 `sys:config` 权限）。因此这里可以
放心做多次查询与并发探测，而 `/health` 不行。
"""
from __future__ import annotations

from typing import Optional

from app import config
from app.constants import HEALTH_PROBE_TIMEOUT_SEC
from app.engine.metric_percentile import percentile
from app.logging import get_logger
from app.tasks import health_probe
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.system.health")

#: 探测超时预算（随响应可见，便于运维核对；取值来自 constants）
PROBE_TIMEOUT_SEC = HEALTH_PROBE_TIMEOUT_SEC

#: 进程启动时刻（`uptime_sec` 用它算）。与 `common_api` 同一手法放在模块级：
#: 即使只做 ASGI 测试（不跑 lifespan）也能拿到合理值。
_STARTED_AT_MS = now_ms()

#: p95 超标线（§2.2 的配色规则：> 50ms 标红，对齐概要设计 §6 的目标）。
#: 由后端下发而不是前端硬编码——配色阈值改一次要能只改一处。
DECISION_P95_WARN_MS = 50.0
#: 名单缓存命中率告警线（§2.2：< 90% 标橙）
LIST_CACHE_HIT_RATE_WARN = 0.9


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), digits)


class HealthService:
    """`GET /system/stats` 的组装（只读、无副作用）。"""

    async def stats(self) -> dict:
        notices: list[dict] = []
        metrics, metric_notices = await self._metrics()
        notices += metric_notices

        components, probe_notices = await health_probe.probe_components()
        notices += probe_notices

        mongo = self._mongo_from(components)
        window = self._window()
        hit_rate, samples = self._cache_hit_rate()

        return {
            "qps": metrics.get("qps"),
            "decision_p50_ms": metrics.get("p50"),
            "decision_p95_ms": metrics.get("p95"),
            "decision_p99_ms": metrics.get("p99"),
            "list_cache_hit_rate": hit_rate,
            "list_cache_samples": samples,
            "window": window,
            "mongo": mongo,
            "components": components,
            "uptime_sec": int((now_ms() - _STARTED_AT_MS) / 1000),
            # 阈值随响应下发（前端不硬编码配色线）
            "thresholds": {
                "decision_p95_warn_ms": DECISION_P95_WARN_MS,
                "list_cache_hit_rate_warn": LIST_CACHE_HIT_RATE_WARN,
            },
            "metrics_source": {
                "endpoint": f"{config.API_PREFIX}/metrics/throughput",
                "window": "5m",
                "note": "模块 11 的现成口径（BR-13-25：本模块不自己聚合）",
                **health_probe.probe_components_declaration(),
            },
            "notices": notices,
            "probed_at": now_ms(),
        }

    # ---------------- 11 的指标（复用，不另算） ----------------
    async def _metrics(self) -> tuple[dict[str, Optional[float]], list[dict]]:
        """取吞吐与延迟分位；11 不可用时**降级为 null + 告警**而不是整卡失败。

        为什么不让 `MET-5001` 把 `/system/stats` 也变成 503：这张卡还有
        Mongo 连通性、组件状态、窗口大小三项**与指标库无关**的信息，它们
        恰恰是"指标查不出来时"最需要看的（大概率就是 Mongo 出问题了）。
        整卡 503 会把唯一有用的证据一起藏起来。因此走 `notices`（引用 11 的
        原码 `MET-5001`，ER-02：引用别人的码保持原前缀）。
        """
        from app.services import metric_service

        try:
            data = await metric_service.get_metric_service().throughput(window="5m")
        except Exception as e:  # noqa: BLE001 - 指标不可用不该拖垮整张卡
            log.warning("[MET-5001] /system/stats 读取 11 的吞吐指标失败：%s: %s",
                        type(e).__name__, e)
            code = getattr(e, "code", "MET-5001")
            return (
                {"qps": None, "p50": None, "p95": None, "p99": None},
                [{"code": code, "message": "指标数据暂不可用，延迟分位为空",
                  "source": "11 /metrics/throughput"}],
            )

        hist = data.get("elapsed_hist") or {}
        return (
            {
                # `qps_1m` 是"最近一个已闭合分钟桶"的 QPS（§3.6 的口径）
                "qps": data.get("qps_1m"),
                # p95 直接用 11 算好的值；p50/p99 用同一个 `percentile()` 在
                # **同一份直方图**上求——同源同法，不存在第二套口径
                "p95": data.get("p95_elapsed_ms"),
                "p50": percentile(hist, 0.50) if hist else None,
                "p99": percentile(hist, 0.99) if hist else None,
            },
            [],
        )

    # ---------------- 04 的窗口状态 ----------------
    @staticmethod
    def _window() -> dict:
        """04 的窗口运行状态（BR-04-15 的估算口径，本模块只做展示）。"""
        try:
            from app.services.feature_service import get_feature_service

            stats = get_feature_service().window.stats()
        except Exception as e:  # noqa: BLE001 - 窗口是进程内状态，取不到就不显示
            log.warning("读取特征窗口状态失败：%s: %s", type(e).__name__, e)
            return {"total_events": 0, "per_dimension_max": 0, "est_mem_mb": None,
                    "last_sweep_at": None, "capacity": 0, "distinct_keys": 0,
                    "dimensions": {}, "error": f"{type(e).__name__}: {e}"}

        dimensions = dict(stats.get("dimensions") or {})
        per_dimension_max = 0
        try:
            from app.services.feature_service import get_feature_service

            per_dimension_max = get_feature_service().window.max_queue_length()
        except Exception as e:  # noqa: BLE001 - 辅助信息，取不到不影响其余字段
            log.warning("读取单队列最大长度失败：%s: %s", type(e).__name__, e)
        return {
            "total_events": int(stats.get("total_entries") or 0),
            "per_dimension_max": int(per_dimension_max),
            "est_mem_mb": _round(int(stats.get("estimated_bytes") or 0) / (1024 * 1024), 2),
            "last_sweep_at": stats.get("last_sweep_at"),
            "capacity": int(stats.get("capacity") or 0),
            "distinct_keys": int(stats.get("distinct_keys") or 0),
            "dimensions": {k: int(v) for k, v in dimensions.items()},
            "memory_warned": bool(stats.get("memory_warned")),
        }

    # ---------------- 05 的名单缓存 ----------------
    @staticmethod
    def _cache_hit_rate() -> tuple[Optional[float], int]:
        """名单缓存命中率（BR-13-04 的 TTL 就在这个缓存上）。

        **无样本时返回 `None`**（不是 0）：见模块 docstring 的 BR-11-08 引用。
        """
        try:
            from app.engine.list_filter import LIST_CACHE

            stats = LIST_CACHE.stats()
            hits = int(stats.get("hits") or 0)
            misses = int(stats.get("misses") or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("读取名单缓存统计失败：%s: %s", type(e).__name__, e)
            return None, 0
        total = hits + misses
        if total <= 0:
            return None, 0
        return round(hits / total, 4), total

    # ---------------- Mongo（从组件探测结果里取，避免 ping 两次） ----------------
    @staticmethod
    def _mongo_from(components: list[dict]) -> dict:
        """把 Mongo 组件的探测结果整理成 `data.mongo`（§2.2 的第 4 张指标卡）。

        读的是探测项上的**结构化字段**（`connected`/`latency_ms`/`collections`），
        不是从中文 `detail` 里正则抠数字：抠字符串的实现在改一句文案之后就会
        静默退化成"永远没有集合数"，而那种缺陷没人会立刻发现。
        """
        for item in components:
            if item.get("name") != health_probe.NAME_MONGO:
                continue
            status = str(item.get("status") or "")
            return {
                "connected": bool(item.get("connected", status == "ok")),
                "latency_ms": item.get("latency_ms"),
                "db": config.MONGO_DB_NAME or None,
                "collections": item.get("collections"),
                "error": item.get("error"),
                "timeout": status == "timeout",
            }
        return {"connected": False, "latency_ms": None, "db": config.MONGO_DB_NAME or None,
                "collections": None, "error": "MongoDB 探测缺失", "timeout": False}


def build_health_service() -> HealthService:
    """装配（无状态，直接返回实例）。"""
    return HealthService()


__all__ = [
    "DECISION_P95_WARN_MS", "HealthService", "LIST_CACHE_HIT_RATE_WARN",
    "PROBE_TIMEOUT_SEC", "build_health_service",
]
