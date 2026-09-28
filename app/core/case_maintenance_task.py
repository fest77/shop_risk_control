# -*- coding: utf-8 -*-
"""案件维护定时任务（模块 08 §4.2 / §4.7：BR-08-10 / 38 + D5 的补建案兜底）。

每 60 秒一轮，做三件事：

1. **超时回收**（BR-08-10）：`reviewing` 且超过 `claim_deadline_at` 的案件
   回收到 `pending`、清空 `assignee` / `claimed_at`，写审计 `case.recycle`（BR-08-11）。
2. **自动归档**（BR-08-38）：`disposed` 超过 7 天（`CASE_ARCHIVE_AFTER_DAYS`）
   置 `archived` + `archived_at`，写审计 `case.archive`（BR-08-09）。
3. **补建案**（决策 D5 的兜底）：把"应当建案却还没建"的 `review`/`reject`
   决策补上。建案的正路是 05 落库后触发（`decision.py` 的钩子），
   但那条路是**异步**的，任何一次失败（Mongo 抖动、进程重启）都会让一个
   不确定的请求**无人处理**——这正是 D5 要防的结果，因此必须有兜底。

## 为什么用"每 60s 一轮"而不是精确到秒

BR-08-10 明写"每 60s 扫一次"。它的业务含义是"案件最多在某人名下多挂 1 分钟"
（阈值 30 分钟本身就有分钟级余量）。把周期压到 1 秒只会把同样的几次查询
白跑 60 倍。

## 为什么失败只记日志、下一轮重试

定时任务没有调用方可以接收错误码。而这里三件事的失败后果都是
"晚一轮生效"，比"抛出异常让后台协程退出（此后永不回收）"轻得多。
与 `core/list_cleanup_task.py` / `core/metric_rollup.py` 保持同一处置风格。

## `case_claim_timeout_min = 0` 的处理

BR-08-12：取 0 表示**关闭超时回收**。此时 `claim_deadline_at` 根本不写
（见 `CaseService.claim_deadline`），扫描条件里也显式排除了 `null`
（Mongo 比较时 `null` 小于任何数字，漏掉这个条件会把"关闭超时"变成
"认领即回收"）。本任务**不需要**额外分支：阈值关闭时自然扫不到任何案件。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.logging import get_logger
from app.services.case_service import get_case_service

log = get_logger("shop_risk_control.case.maintenance")

#: 扫描周期（BR-08-10 明文：每 60s 一次）
INTERVAL_SEC = 60.0

#: 单轮补建案的上限（有界，避免一次积压把一轮拉成几分钟）
RECOVER_LIMIT = 200


async def run_once(now: Optional[int] = None) -> dict[str, Any]:
    """执行一轮维护，返回 `{recycled, archived, cases_created, error}`。

    返回值是给测试与排障用的：三个计数各自可断言，"这一轮到底做了什么"
    不必去翻日志。
    """
    result: dict[str, Any] = {
        "recycled": 0, "archived": 0, "cases_created": 0, "error": None,
    }
    service = get_case_service()
    try:
        result["recycled"] = len(await service.recycle_timeouts(now=now))
        result["archived"] = len(await service.auto_archive(now=now))
        result["cases_created"] = len(
            await service.recover_missing_cases(limit=RECOVER_LIMIT)
        )
    except Exception as e:  # noqa: BLE001 - 后台任务绝不允许因单轮异常而退出
        result["error"] = f"{type(e).__name__}: {e}"
        log.exception("案件维护单轮异常，下一轮重试：%s", e)
    return result


class CaseMaintenanceScheduler:
    """每 `interval_sec` 跑一轮的后台协程（结构照 `ListCleanupScheduler`）。

    停机用 `asyncio.Event` 而不是裸 `asyncio.sleep`：否则 `stop()` 之后还要
    等最长一个完整周期才能退出，进程关不掉（测试里表现为"关服卡住"）。
    """

    def __init__(self, *, interval_sec: float = INTERVAL_SEC):
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stats: dict[str, int] = {
            "runs": 0, "recycled": 0, "archived": 0, "cases_created": 0,
            "failures": 0,
        }

    def _ensure_loop(self) -> None:
        """把停机事件绑定到当前循环（测试逐用例新建循环，参见 AuditService）。"""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._task = None
            self._stop = None

    async def start(self) -> None:
        """启动（**幂等**：已在跑就不重复启动，避免两份任务同时回收）。"""
        self._ensure_loop()
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop_run(), name="case-maintenance")
        log.info("案件维护定时任务已启动（每 %.0fs 一轮）", self.interval_sec)

    async def stop(self) -> None:
        """停止（幂等：没在跑时调用是安全的空操作）。"""
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
        log.info("案件维护定时任务已停止")

    async def _sleep(self, seconds: float) -> None:
        if self._stop is None:  # pragma: no cover - start() 之前不会进入循环
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop_run(self) -> None:
        """先立即跑一轮（覆盖上一次停机期间到期的案件），再按周期循环。"""
        while True:
            try:
                result = await run_once()
                self.stats["runs"] += 1
                self.stats["recycled"] += int(result["recycled"] or 0)
                self.stats["archived"] += int(result["archived"] or 0)
                self.stats["cases_created"] += int(result["cases_created"] or 0)
                if result["error"]:
                    self.stats["failures"] += 1
            except Exception as e:  # noqa: BLE001 - 单轮异常绝不能终止整个循环
                self.stats["failures"] += 1
                log.warning("案件维护单轮异常，下一轮重试：%s", e)
            if self._stop is not None and self._stop.is_set():
                return
            await self._sleep(self.interval_sec)
            if self._stop is not None and self._stop.is_set():
                return


_SCHEDULER = CaseMaintenanceScheduler()


def get_scheduler() -> CaseMaintenanceScheduler:
    """取进程内调度器单例（由 `app/main.py` 的 lifespan 装配）。"""
    return _SCHEDULER


async def start_maintenance() -> None:
    """启动案件维护（lifespan 启动阶段调用；重复调用幂等）。"""
    await _SCHEDULER.start()


async def stop_maintenance() -> None:
    """停止案件维护（lifespan 关闭阶段调用；未启动时调用安全）。"""
    await _SCHEDULER.stop()


__all__ = [
    "INTERVAL_SEC",
    "RECOVER_LIMIT",
    "CaseMaintenanceScheduler",
    "get_scheduler",
    "run_once",
    "start_maintenance",
    "stop_maintenance",
]
