# -*- coding: utf-8 -*-
"""名单过期清理定时任务（模块 06 `BR-06-31`，每 10 分钟一轮）。

## 为什么必须是**条件更新**而不是"先查再改"

清库任务与用户操作、以及它自己的多轮执行之间天然并发。若写成
"查出一批 -> 逐条 update"，则在"查出"与"改"之间用户可能刚把某条移除，
任务又会把它改成 `expired`——覆盖掉用户的移除留痕（`removed_by` 丢失），
审计上表现为"谁都没删，它自己变成了 expired"。因此这里用
`update_many({status: active, expire_at <= now}, {$set: {status: expired}})`
一次带前提地改：先到者改状态，后到者匹配 0 条，互相不会覆盖。

`expire_at: {"$ne": None}` 是这条语句里最要紧的一个条件：黑/白名单默认
**永久**（`expire_at = null`）。若漏掉它，Mongo 在比较时会把 `null` 当作
小于任何数字，"永久黑名单"会被这轮清理**静默清空**——风控最严重的失效形态
（该拦的不拦）且没有任何报错。

## 为什么失败只记日志、下一轮重试

定时任务没有调用方可以返回错误码（不像 HTTP 接口）。而清理失败的唯一后果是
"过期条目多活 10 分钟"——比它更糟的处置是：抛出异常让后台协程退出
（此后**永不再清理**，过期名单无限期生效），或反复重试同一条卡住的文档
（把一轮 10 分钟的节奏拖成忙等）。因此采用"本轮放弃 + 下轮重来"，
与 `core/metric_rollup.py` 的定时任务保持同一处置风格。

## 与缓存失效的关系

清完一批后调用一次 `invalidate_list_cache()`：决策侧若还缓存着"该实体在名单里"，
过期清理就等于没做（BR-06-31 明确要求置 `expired` **并**失效缓存）。
失效失败只置降级标记 + 告警，不回滚 `expired`——回滚会让条目继续生效，
那是比"缓存陈旧"更危险的方向；而置降级能促使 05 回源查库。
"""
from __future__ import annotations

import asyncio
import importlib
from typing import Any, Optional

from pymongo.errors import PyMongoError

from app.core.degraded import DEGRADED
from app.logging import get_logger
from app.repos.list_repo import ListRepo
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.list.cleanup")

# 扫描周期（BR-06-31）：10 分钟。取值依据是"过期精度"与"库负载"的折中：
# 名单过期对判定的影响是"多拦/多放 10 分钟"，而调成 1 分钟只是把同样的
# update_many 白跑 10 倍。
CLEANUP_INTERVAL_SEC = 600.0

# 单轮清理最多更新多少条：非法批量（例如误把 expire_at 写成秒级时间戳，
# 导致全表都"已过期"）一次性改掉全部条目，事后极难还原。
# 加上限后，异常批次最多影响这么多条，剩下的下一轮继续，从日志计数上能看出来。
# 实现方式见 `_mark_expired_capped`：Mongo 的 `update_many` 没有 limit，
# 因此按上限切成若干次带 `status=active` 前提的批量更新。
BATCH_LIMIT = 5000
# 一轮内最多切几批（5000 × 100 = 50 万条/轮）。上限存在的意义是"清理任务
# 不能无限期占着事件循环"，而不是业务上限：撞到它会留下 remaining>0 的日志。
MAX_BATCHES = 100


def _repo() -> ListRepo:
    """每次执行都重新取数据库句柄。

    **不缓存 repo**：测试会切换数据库（`db.use_database`），缓存的集合句柄
    会让清理任务把测试库的数据改到业务库去（与 audit_service._repo 同因）。
    """
    from app import db as db_module

    return ListRepo(db_module.get_db())


def _invalidate_cache() -> None:
    """通过**模块属性**调用失效函数，而不是 `from ... import invalidate_list_cache`。

    这样测试可以 monkeypatch `app.services.list_service.invalidate_list_cache`
    来验证"失效失败也不回滚 expired、只置降级"这条分支；直接 import 名字会把
    函数对象固化在本模块的命名空间里，桩替换对它无效，那条分支就测不到了。
    """
    module = importlib.import_module("app.services.list_service")
    module.invalidate_list_cache()


async def run_cleanup_once(now: Optional[int] = None) -> dict[str, Any]:
    """执行一轮清理，返回 `{scanned_at, expired, remaining, error}`。

    返回体是给测试与人工排障用的：`remaining > 0` 说明这一轮撞上了批次上限，
    下一轮会接着清（不会漏）；`error` 非空说明本轮失败，已放弃并等下一轮。
    """
    moment = now if now is not None else now_ms()
    result: dict[str, Any] = {
        "scanned_at": moment, "expired": 0, "remaining": 0, "error": None,
    }
    try:
        repo = _repo()
        total = 0
        batches = 0
        while batches < MAX_BATCHES:
            changed = await repo.mark_expired(moment, BATCH_LIMIT)
            total += changed
            batches += 1
            if changed < BATCH_LIMIT:
                break
        result["expired"] = total
        result["remaining"] = total if batches >= MAX_BATCHES else 0
        if result["remaining"]:
            log.warning("名单过期清理撞上单轮上限，剩余条目留待下一轮：已置 %d 条", total)
    except PyMongoError as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning("[CFG-5002] 名单过期清理失败，下一轮重试：%s", e)
        return result
    except Exception as e:  # noqa: BLE001 - 后台任务绝不允许因单轮异常而退出
        result["error"] = f"{type(e).__name__}: {e}"
        log.exception("名单过期清理出现未预期异常，下一轮重试：%s", e)
        return result

    if result["expired"]:
        try:
            _invalidate_cache()
        except Exception as e:  # noqa: BLE001 - 见模块 docstring：不回滚，只降级
            DEGRADED.mark(f"过期清理后缓存失效失败：{e}")
            log.error("过期清理后缓存失效失败（已置降级）：%s", e)
        log.info("名单过期清理完成：置 expired %d 条", result["expired"])
    return result


class ListCleanupScheduler:
    """每 `interval_sec` 跑一轮清理的后台协程。

    结构照 `core/metric_rollup.py` 的 `RollupScheduler`：
    停机用 `asyncio.Event` 而不是裸 `asyncio.sleep`——否则 `stop()` 之后
    还要等最长一个完整周期（10 分钟）才能退出，进程关不掉。
    """

    def __init__(self, *, interval_sec: float = CLEANUP_INTERVAL_SEC):
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stats: dict[str, int] = {"runs": 0, "expired": 0, "failures": 0}

    def _ensure_loop(self) -> None:
        """把停机事件绑定到当前循环（测试逐用例新建循环，参见 AuditService）。"""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._task = None
            self._stop = None

    async def start(self) -> None:
        """启动后台协程（**幂等**：已在跑则不重复启动，避免两份任务同时清理）。"""
        self._ensure_loop()
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop_run(), name="list-cleanup")
        log.info("名单过期清理定时任务已启动（每 %.0fs 一轮）", self.interval_sec)

    async def stop(self) -> None:
        """停止后台协程（**幂等**：没在跑时调用是安全的空操作）。"""
        self._ensure_loop()
        stop = self._stop
        if stop is not None:
            stop.set()
        task = self._task
        self._task = None
        self._stop = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        log.info("名单过期清理定时任务已停止")

    async def _sleep(self, seconds: float) -> None:
        """可被 `stop()` 立刻打断的等待。"""
        if self._stop is None:  # pragma: no cover - start() 之前不会进入循环
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop_run(self) -> None:
        """先立即跑一轮（覆盖上一次停机期间到期的条目），再按周期循环。"""
        while True:
            try:
                result = await run_cleanup_once()
                self.stats["runs"] += 1
                self.stats["expired"] += int(result["expired"] or 0)
                if result["error"]:
                    self.stats["failures"] += 1
            except Exception as e:  # noqa: BLE001 - 单轮异常绝不能终止整个循环
                self.stats["failures"] += 1
                log.warning("名单过期清理单轮异常，下一轮重试：%s", e)
            if self._stop is not None and self._stop.is_set():
                return
            await self._sleep(self.interval_sec)
            if self._stop is not None and self._stop.is_set():
                return


_SCHEDULER = ListCleanupScheduler()


def get_cleanup_scheduler() -> ListCleanupScheduler:
    """取进程内调度器单例（由 `app/main.py` 的 lifespan 装配）。"""
    return _SCHEDULER


async def start_cleanup() -> None:
    """启动过期清理（lifespan 启动阶段调用；重复调用幂等）。"""
    await _SCHEDULER.start()


async def stop_cleanup() -> None:
    """停止过期清理（lifespan 关闭阶段调用；未启动时调用安全）。"""
    await _SCHEDULER.stop()


__all__ = [
    "BATCH_LIMIT", "CLEANUP_INTERVAL_SEC", "ListCleanupScheduler",
    "get_cleanup_scheduler", "run_cleanup_once", "start_cleanup", "stop_cleanup",
]
