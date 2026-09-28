# -*- coding: utf-8 -*-
"""旁路副作用的重试队列与重启恢复（模块 08 §4.6，BR-08-31 / 32）。

## 队列里装的是"已经生效、但还没同步出去"的事

处置的三类副作用性质完全不同：

| 副作用 | 失败后的处置 | 依据 |
|---|---|---|
| 名单写入（**紧操作**） | 整体失败 + 回滚，**不允许静默成功** | BR-08-30 / `DSP-5002` |
| `BizAdapter` 同步（旁路） | 处置保留，标记 + 入队重试，`degraded=true` | BR-08-31 / `DSP-5003` |
| 审计落库（旁路） | 处置保留，置 `audit_pending=true` + 入队重试 | BR-08-32 / `DSP-5004` |

旁路失败**不能撤销**已经发生的拦截：撤销会造成"先拦后放"的二次伤害
（Spec §8「新增 2」的裁定）。但"不撤销"绝不等于"不管"——那会变成静默成功。
因此本模块负责把这两类失败**显式记下来并可重试**。

## 为什么是"内存队列 + 文档标记"两件套

- **文档标记**（`case_actions.biz_sync_result.status="failed"`、
  `risk_cases.audit_pending=true`）是**持久**的：进程崩溃/重启之后，
  重启扫描（`recover_from_db()`）据此把待办重新装回队列。
- **内存队列**是**快**的：同一进程内的重试不必每次查库。

只有队列 → 重启即丢（这正是 Spec 明令禁止的"状态丢失"）；
只有标记 → 每次重试都要全表扫描。两者互补，且**标记是权威**：队列是它的缓存。

## 有界

队列有上限（`QUEUE_MAX`）。满了之后**丢弃最旧**而不是拒绝新的：正在发生的
处置比已经发生过很多次的那一条更值得保留；丢弃必须计数 + 告警，绝不静默。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable, Optional

from app.logging import get_logger
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.case.biz_sync_retry")

#: 队列上限。它的量级按"演示环境一次答辩期间的处置量"取，够宽；真正决定
#: 会不会丢的是**持久标记**——队列只是缓存（见模块 docstring）。
QUEUE_MAX = 2000

#: 重试任务类别
KIND_BIZ_SYNC = "biz_sync"
KIND_AUDIT = "audit"


def _make_key(kind: str, case_no: str, action_id: str) -> str:
    """同一个待办只排一条（幂等入队）。

    没有它，一次处置失败后"页面点两次重试"就会让队列里出现两条同样的待办，
    重试次数 `attempt_no` 会跳号——而 `attempt_no` 是页面展示"已重试 N 次"的
    唯一依据。
    """
    return f"{kind}:{case_no}:{action_id}"


class RetryQueue:
    """`(kind, case_no, action_id) -> 待办` 的有界 FIFO 队列。"""

    def __init__(self, maxsize: int = QUEUE_MAX) -> None:
        self.maxsize = int(maxsize)
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        self.stats = {"enqueued": 0, "dropped": 0, "retried": 0, "recovered": 0}

    # ---------------- 写 ----------------
    def enqueue(
        self,
        *,
        kind: str,
        case_no: str,
        action_id: str,
        operator: str = "system",
        action_types: Optional[Iterable[str]] = None,
        detail: str = "",
    ) -> dict:
        """登记一条待重试项。返回该条目（供调用方回传 `retry_hint`）。"""
        key = _make_key(kind, case_no, action_id)
        item = {
            "kind": kind,
            "case_no": str(case_no),
            "action_id": str(action_id),
            "operator": str(operator or "system"),
            "action_types": [str(a) for a in (action_types or [])],
            "detail": str(detail),
            "enqueued_at": now_ms(),
            "attempt_no": int(self._items[key]["attempt_no"]) if key in self._items else 0,
        }
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = item
        self.stats["enqueued"] += 1
        while len(self._items) > self.maxsize:
            dropped_key, dropped = self._items.popitem(last=False)
            self.stats["dropped"] += 1
            log.error(
                "[DSP-5003] 重试队列已满（%d），丢弃最旧待办 %s —— 该条内容仍可由"
                "持久标记恢复（recover_from_db）", self.maxsize, dropped_key,
            )
            del dropped
        return item

    def take(self, case_no: Optional[str] = None, action_id: Optional[str] = None) -> list[dict]:
        """取出（并移除）匹配的待办。不带条件即取出全部。"""
        picked: list[dict] = []
        for key in list(self._items.keys()):
            item = self._items[key]
            if case_no is not None and item["case_no"] != str(case_no):
                continue
            if action_id is not None and item["action_id"] != str(action_id):
                continue
            picked.append(self._items.pop(key))
        return picked

    def peek(self, *, case_no: Optional[str] = None) -> list[dict]:
        """只看不取（供 `/health`、排障与测试断言）。"""
        return [
            dict(item) for item in self._items.values()
            if case_no is None or item["case_no"] == str(case_no)
        ]

    def mark_retried(self) -> None:
        """记一次"确实发起过重试"（无论成功与否），供 `/health` 与排障看趋势。"""
        self.stats["retried"] += 1

    def mark_recovered(self, count: int) -> None:
        self.stats["recovered"] += int(count)

    def size(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        """清空（测试夹具逐用例复位；生产无调用点——它等于放弃已登记的待办）。"""
        self._items.clear()

    def snapshot(self) -> dict[str, Any]:
        return {"size": self.size(), "maxsize": self.maxsize, **self.stats}


# ============================================================
# 进程内单例 + 模块级入口
# ============================================================
_QUEUE = RetryQueue()


def get_queue() -> RetryQueue:
    return _QUEUE


def enqueue_biz_sync(
    case_no: str, action_id: str, *, operator: str = "system",
    action_types: Optional[Iterable[str]] = None, detail: str = "",
) -> dict:
    """登记一条业务同步待办（BR-08-31 ②）。"""
    return _QUEUE.enqueue(
        kind=KIND_BIZ_SYNC, case_no=case_no, action_id=action_id,
        operator=operator, action_types=action_types, detail=detail,
    )


def enqueue_audit(case_no: str, *, action: str = "case.dispose", detail: str = "") -> dict:
    """登记一条审计落库待办（BR-08-32）。

    `action_id` 用审计动作名：一个案件的同一动作只会有一条待重试项，
    避免同一次失败被反复登记（入队是幂等的）。
    """
    return _QUEUE.enqueue(
        kind=KIND_AUDIT, case_no=case_no, action_id=action, detail=detail,
    )


async def recover_from_db(*, limit: int = 200) -> dict[str, int]:
    """重启后按**持久标记**恢复待办（BR-08-31 ②：「重启后按标记恢复」）。

    扫两处：
    1. `case_actions.biz_sync_result.status == "failed"`（业务同步未成功）；
    2. `risk_cases.audit_pending == True`（审计落库待重试）。

    只做"装回队列"，**不在这里真正重试**：启动期的重试要和业务系统的可用性
    赛跑，放在启动路径上会让服务起得慢甚至起不来。真正的重试由
    `/biz-sync/retry` 接口或维护任务触发。

    扫描失败只告警并返回 0：启动期的一次 Mongo 抖动不该让整个服务起不来。
    """
    from pymongo.errors import PyMongoError

    from app import db as db_module
    from app.constants import COLL_CASE_ACTIONS, COLL_RISK_CASES

    result = {"biz_sync": 0, "audit": 0, "error": 0}
    try:
        db = db_module.get_db()
        cursor = db[COLL_CASE_ACTIONS].find(
            {"biz_sync_result.status": "failed"},
            {"case_no": 1, "action_type": 1, "biz_sync_result": 1},
        ).limit(limit)
        for row in await cursor.to_list(length=limit):
            enqueue_biz_sync(
                str(row.get("case_no") or ""), str(row.get("_id") or ""),
                detail="启动恢复：biz_sync_result.status=failed",
            )
            result["biz_sync"] += 1
        cursor = db[COLL_RISK_CASES].find(
            {"audit_pending": True}, {"_id": 1}
        ).limit(limit)
        for row in await cursor.to_list(length=limit):
            enqueue_audit(str(row.get("_id") or ""), detail="启动恢复：audit_pending=true")
            result["audit"] += 1
    except PyMongoError as e:
        result["error"] = 1
        log.warning("[DSP-5004] 待重试项恢复扫描失败（不阻断启动，下一轮再扫）：%s", e)
    except Exception as e:  # noqa: BLE001 - 启动路径绝不允许被这一项拖死
        result["error"] = 1
        log.warning("待重试项恢复扫描出现未预期异常：%s", e)
    _QUEUE.mark_recovered(result["biz_sync"] + result["audit"])
    if result["biz_sync"] or result["audit"]:
        log.info(
            "待重试项已按持久标记恢复：业务同步 %d 条、审计 %d 条",
            result["biz_sync"], result["audit"],
        )
    return result


def reset_queue() -> None:
    """复位队列（测试夹具逐用例使用；生产无调用点）。"""
    _QUEUE.clear()
    _QUEUE.stats = {"enqueued": 0, "dropped": 0, "retried": 0, "recovered": 0}


__all__ = [
    "KIND_AUDIT",
    "KIND_BIZ_SYNC",
    "QUEUE_MAX",
    "RetryQueue",
    "enqueue_audit",
    "enqueue_biz_sync",
    "get_queue",
    "recover_from_db",
    "reset_queue",
]
