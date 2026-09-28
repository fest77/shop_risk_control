# -*- coding: utf-8 -*-
"""E03 `decisions` / E04 `decision_hits` 的**写入**仓储（模块 05 §6，AD-01）。

## AD-01：决策同步返回、落库异步

决策算完就立即返回给调用方（03 的 200ms 预算内），落库丢到后台任务。
因此本仓储的方法都是"被后台任务 await"的**异步写**，不参与同步决策耗时。
这不代表写失败可以忽略：写失败要进重试队列 + 告警（`RUL` 侧的告警由
`app/engine/decision.py` 的调度器记录），因为**没有决策记录的事件在 07/08
眼里等于"从没发生过"**——审核员看不到它，也就永远不会处理它。

## 为什么 `decision_hits` 要冗余存 `rule_name` 与 `score`（BR-05-21）

若只存 `rule_code`，命中明细就必须联查 `rules` 才能显示规则名与分值。而
`rules` 是**可变的**：规则改名、改分之后，历史决策的展示会跟着变——
一条当时按"同设备 12 个账号"扣了 40 分的决策，在规则被改成 20 分之后
会在页面上显示成 20 分。**那等于篡改了历史证据**，而决策记录的全部意义
就是"当时到底按什么判的"。因此这里存的是**当时的快照**：规则怎么变，
历史明细都不动。

## 为什么 `rule_versions` 也要写进 `decisions`（BR-05-20）

`decision_hits` 存的是"命中了哪些规则、各扣多少分"，但没有存**规则的完整
条件与版本**。要重放"换回当时的规则版本，分数是否一致"（V-05-09），就必须
知道当时每条规则是哪个 `version`。`{rule_code: version}` 很小（一次决策几条
到几十条），却让历史决策可验证——这是本模块唯一的防腐设计。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from pymongo.errors import PyMongoError

from app.constants import COLL_DECISION_HITS, COLL_DECISIONS
from app.errors import AppError
from app.logging import get_logger
from app.utils.ids import new_decision_id
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.decision_repo")


def _persist_error(op: str, detail: str) -> AppError:
    """落库失败统一走 `COM-5001`（数据库不可用）。

    这里**不复用 05 的 `RUL-5002`**：那个码的含义是"规则集读不出来"，属于
    **决策输入缺失**（会导致 fail-closed 降级）；本函数描述的是"决策已经算完、
    只是没写进库"，属于**输出落库失败**（决策照常返回、进重试队列）。两者
    对调用方的处置完全不同，混用一个码会让"降级"与"丢了记录"再也分不开。
    """
    return AppError("COM-5001", "决策落库失败，请稍后重试", 503, {"op": op, "detail": detail})


class DecisionRepo:
    """`decisions` 与 `decision_hits` 的唯一写出口。"""

    def __init__(self, db: Any):
        self.db = db
        self.decisions = db[COLL_DECISIONS]
        self.hits = db[COLL_DECISION_HITS]

    async def new_id(self, at_ms: Optional[int] = None) -> str:
        """取下一个决策编号 `DEC{yyyyMMdd}{12位}`（E03 主键）。

        走 `seq_counters` 的原子 `$inc`（`app/utils/ids.py`）：编号是"每日 12 位
        序列"，用进程内计数器会在重启后从 1 重来并与库里已有编号撞主键——
        撞主键的后果是**决策记录被静默覆盖**，那比报错严重得多。
        """
        return await new_decision_id(self.db, at_ms)

    async def insert_decision(self, doc: dict) -> str:
        """写入一条决策（E03）。`_id` 必须由调用方用 `new_id()` 取好。

        **`_id` 与业务字段并存**：`decisions` 里既写 `_id`（主键，供 E04 的
        `decision_id` 引用）也写 `event_id`（供按事件反查），两者不是同一件事，
        不能合并——一个事件理论上可以被重算产生多条决策（人工复算/规则回滚），
        而 `event_id` 上的唯一性会禁止这一点。E03 也确实没在 `event_id` 上
        声明唯一索引。
        """
        payload = dict(doc)
        payload.setdefault("decided_at", now_ms())
        try:
            await self.decisions.insert_one(payload)
        except PyMongoError as e:
            raise _persist_error("insert_decision", f"{type(e).__name__}: {e}") from e
        return str(payload["_id"])

    async def insert_hits(self, docs: Iterable[dict]) -> int:
        """批量写入命中明细（E04），返回写入条数。

        用 `insert_many(ordered=False)` 而不是逐条 `insert_one`：命中明细是
        一次决策的**一个整体**，逐条写在高频路径上会放大往返开销；`ordered=False`
        让一条明细的键冲突不会连带丢弃同批其余明细（明细之间没有顺序依赖）。

        空列表直接返回 0 且**不调用** `insert_many`：pymongo 对空列表会抛
        `InvalidOperation`，而"没有命中"是完全正常的情况（`pass` 决策就是这样）。
        """
        rows = list(docs)
        if not rows:
            return 0
        try:
            result = await self.hits.insert_many(rows, ordered=False)
        except PyMongoError as e:
            raise _persist_error("insert_hits", f"{type(e).__name__}: {e}") from e
        return len(result.inserted_ids)

    async def find_decision(self, decision_id: str) -> Optional[dict]:
        """按决策编号取一条（供 V-05-09/10 的验证与排障）。"""
        return await self.decisions.find_one({"_id": decision_id})

    async def find_by_event(self, event_id: str) -> Optional[dict]:
        """按事件取**最近一条**决策（供验证与排障；03 的详情接口读的是事件文档）。"""
        cursor = self.decisions.find({"event_id": event_id}).sort([("decided_at", -1)])
        rows = await cursor.to_list(length=1)
        return rows[0] if rows else None

    async def list_hits(self, decision_id: str) -> list[dict]:
        """取某次决策的全部命中明细（供 V-05-10 断言"冗余快照不失真"）。"""
        cursor = self.hits.find({"decision_id": decision_id}).sort([("score", -1)])
        return await cursor.to_list(length=200)


__all__ = ["DecisionRepo"]
