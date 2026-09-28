# -*- coding: utf-8 -*-
"""E08 `risk_cases` 仓储：**并发安全全部靠条件更新表达**（模块 08 §6，决策 D42）。

## 一条贯穿全文件的原则：条件更新，不"先查后写"

案件是**多人在同一个页面上抢**的对象（两个审核员同时点认领、一个人连点两次
处置、后台回收任务与人工认领同时发生）。"先查状态、再写状态"在任何一次
`await` 之间都会失效，本项目已多次记录这一类缺陷（`list_repo.soft_remove`
的注释、决策 D42）。

因此本文件的每一个状态迁移都是**一条带前置条件的 `update_one` /
`find_one_and_update`**，返回**匹配条数**，由服务层决定"0 条"是什么意思
（已被别人认领 → `DSP-4005`；状态不对 → `DSP-4003`；已处置 → `DSP-4004`）。

## `find_one_and_update` 与 `update_one` 的分工

| 方法 | 用途 | 为什么 |
|---|---|---|
| `find_one_and_update` | **认领**（BR-08-04 明令 `findAndModify`） | 认领成功时必须拿到**认领后**的文档（`assignee` / `claimed_at` / 截止时间）一次性回给前端；分两步会多一次往返，且多一次往返就多一个"别人又改了"的窗口 |
| `update_one` | 处置、归档、回收、内部锁 | 这些迁移的响应字段来自服务层自己算出的值，不需要回读 |

## 内部锁为什么不是一个新 `status` 值

BR-08-29 ① 要求"原子锁定案件（`reviewing → disposing` 内部态）"。本实现把
这个内部态落成**独立字段** `dispose_lock`（`{operator, at}`）而**不是**
`status="disposing"`：`status` 是 E08 的对外枚举（`CaseStatus` 只有四个取值），
往里塞第五个值会让 07 的案件列表筛选、`/common/enums` 下发的字典、以及
`test_enums.py` 三处同时失真——而"处置进行中"是**几十毫秒**的内部态，
不该出现在任何对外枚举里。字段带 `at`，超过
`constants.CASE_DISPOSE_LOCK_STALE_MS` 视为持锁进程已死、允许接管，
否则一次崩溃会让案件永远卡在"另一个处置进行中"。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo import ReturnDocument

from app.constants import COLL_RISK_CASES
from app.logging import get_logger
from app.utils.ids import new_case_id

log = get_logger("shop_risk_control.case_repo")


class CaseRepo:
    """`risk_cases` 的唯一读写出口。只做数据库交互，不含业务判断。"""

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_RISK_CASES]

    # ---------------- 编号与写入 ----------------
    async def new_id(self, at_ms: Optional[int] = None) -> str:
        """取下一个案件编号 `CASE{yyyyMMdd}{6位}`（E08 主键）。

        走 `seq_counters` 的原子 `$inc`（`app/utils/ids.py`）：编号是"每日 6 位
        序列"，用进程内计数器会在重启后从 1 重来并与库里已有编号撞主键——
        撞主键的后果是**旧案件被静默覆盖**，比报错严重得多。
        """
        return await new_case_id(self.db, at_ms)

    async def insert(self, doc: dict) -> str:
        """插入一个案件。`event_id` 上的唯一索引是建案幂等的**真正落点**。

        `DuplicateKeyError` **向上抛**：它表达的是"这个事件已经有案件了"，
        由服务层决定返回既有案件（BR-08-02）——仓储层不该知道业务语义。
        """
        await self.col.insert_one(doc)
        return str(doc["_id"])

    # ---------------- 读 ----------------
    async def find_by_id(self, case_no: str) -> Optional[dict]:
        return await self.col.find_one({"_id": str(case_no)})

    async def find_by_event(self, event_id: str) -> Optional[dict]:
        """按事件取案件（BR-08-02 的幂等键：E08 的 `event_id` 唯一）。"""
        return await self.col.find_one({"event_id": str(event_id)})

    async def find_by_decision(self, decision_id: str) -> Optional[dict]:
        return await self.col.find_one({"decision_id": str(decision_id)})

    # ---------------- 状态迁移（全部条件更新） ----------------
    async def claim(
        self, case_no: str, assignee: str, claimed_at: int,
        claim_deadline_at: Optional[int],
    ) -> Optional[dict]:
        """**原子认领**：`{_id, status:"pending"} → {status:"reviewing", assignee, ...}`。

        返回认领后的文档；匹配 0 条返回 `None`（说明不是 `pending` 了——
        可能已被别人认领，也可能已处置/已归档，由服务层回读后区分错误码）。
        """
        update: dict[str, Any] = {
            "$set": {
                "status": "reviewing",
                "assignee": str(assignee),
                "claimed_at": int(claimed_at),
                "claim_deadline_at": claim_deadline_at,
            }
        }
        return await self.col.find_one_and_update(
            {"_id": str(case_no), "status": "pending"},
            update,
            return_document=ReturnDocument.AFTER,
        )

    async def lock_for_disposal(
        self, case_no: str, operator: str, at_ms: int,
        *, stale_before: int,
    ) -> int:
        """处置前置的**内部锁**（BR-08-29 ①）。返回匹配条数。

        条件 = `status=reviewing` + `assignee=operator`
        + **锁为空或已过期**（`stale_before` 之前的锁视为持锁进程已死）。

        为什么必须把 `assignee` 也写进条件：`dispose` 的第一步锁与第三步
        "写 `disposed`"是两次独立的写操作，中间隔着若干 `await`（写名单、调
        业务系统）。若锁的条件里不含 `assignee`，一次"认领被超时回收后又
        被别人认领"的并发就能让**别人**的处置执行在**当前**人名下。
        """
        result = await self.col.update_one(
            {
                "_id": str(case_no),
                "status": "reviewing",
                "assignee": str(operator),
                "$or": [
                    {"dispose_lock": None},
                    {"dispose_lock": {"$exists": False}},
                    {"dispose_lock.at": {"$lte": int(stale_before)}},
                ],
            },
            {"$set": {"dispose_lock": {"operator": str(operator), "at": int(at_ms)}}},
        )
        return int(result.modified_count)

    async def release_lock(self, case_no: str, operator: str) -> int:
        """释放内部锁（处置失败/回滚时调用）。

        条件带 `dispose_lock.operator`：只释放**自己**的锁。
        期间若锁已被别人接管（我的锁过期了），本次释放不该把它清掉。
        """
        result = await self.col.update_one(
            {"_id": str(case_no), "dispose_lock.operator": str(operator)},
            {"$unset": {"dispose_lock": ""}},
        )
        return int(result.modified_count)

    async def mark_disposed(
        self, case_no: str, operator: str, at_ms: int, *,
        biz_sync_pending: bool = False, audit_pending: bool = False,
    ) -> int:
        """`reviewing → disposed`（BR-08-08 的唯一合法迁移之一）。返回匹配条数。

        条件同时含 `status` / `assignee` / `dispose_lock.operator`：
        三者缺一，都可能把"别人的处置"或"已被回收的认领"写成我处置的。
        """
        result = await self.col.update_one(
            {
                "_id": str(case_no),
                "status": "reviewing",
                "assignee": str(operator),
                "dispose_lock.operator": str(operator),
            },
            {
                "$set": {
                    "status": "disposed",
                    "disposed_at": int(at_ms),
                    "biz_sync_pending": bool(biz_sync_pending),
                    "audit_pending": bool(audit_pending),
                },
                "$unset": {"dispose_lock": ""},
            },
        )
        return int(result.modified_count)

    async def mark_archived(self, case_no: str, at_ms: int, *, by: str) -> int:
        """`disposed → archived`。返回匹配条数（0 = 状态已被别处改过）。"""
        result = await self.col.update_one(
            {"_id": str(case_no), "status": "disposed"},
            {"$set": {
                "status": "archived",
                "archived_at": int(at_ms),
                "archived_by": str(by),
            }},
        )
        return int(result.modified_count)

    async def rollback_archive(self, case_no: str, at_ms: int) -> int:
        """归档审计失败后的补偿：把 `archived` 退回 `disposed`（BR-08-09 + D41）。

        条件带 `archived_at` 的值：只回滚**本次**那一次归档。
        若期间有人重新处置/改过该字段，说明那不是我的变更，不该被我覆盖。
        """
        result = await self.col.update_one(
            {"_id": str(case_no), "status": "archived", "archived_at": int(at_ms)},
            {"$set": {"status": "disposed"},
             "$unset": {"archived_at": "", "archived_by": ""}},
        )
        return int(result.modified_count)

    async def rollback_claim(self, case_no: str, assignee: str, claimed_at: int) -> int:
        """认领审计失败后的补偿（D41：审计失败则回滚该次写）。

        条件带 `status=reviewing` + `assignee` + `claimed_at`：只回滚**本次**
        那一次认领。若期间案件已被处置或被回收，那是别人的变更，
        回滚它会抹掉别人的成果（与 `list_repo.restore_active` 同一立场）。
        """
        result = await self.col.update_one(
            {
                "_id": str(case_no),
                "status": "reviewing",
                "assignee": str(assignee),
                "claimed_at": int(claimed_at),
            },
            {"$set": {"status": "pending"},
             "$unset": {"assignee": "", "claimed_at": "", "claim_deadline_at": ""}},
        )
        return int(result.modified_count)

    async def set_audit_pending(self, case_no: str, pending: bool) -> int:
        """标记"审计落库待重试"（BR-08-32）。"""
        result = await self.col.update_one(
            {"_id": str(case_no)}, {"$set": {"audit_pending": bool(pending)}}
        )
        return int(result.modified_count)

    async def set_biz_sync_pending(self, case_no: str, pending: bool) -> int:
        """标记"业务同步待重试"（BR-08-31 ②）。"""
        result = await self.col.update_one(
            {"_id": str(case_no)}, {"$set": {"biz_sync_pending": bool(pending)}}
        )
        return int(result.modified_count)

    # ---------------- 后台扫描 ----------------
    async def find_claim_timeouts(self, moment: int, limit: int) -> list[dict]:
        """取超时未处置的案件（BR-08-10 的扫描）。

        `claim_deadline_at` 为 `null` 的**永不超时**（BR-08-12：阈值为 0 = 关闭
        超时回收）。Mongo 比较时 `null` 小于任何数字，因此条件必须显式
        排除 `null`——否则"关闭超时"会被这条扫描**静默变成 1 秒回收**，
        审核员刚认领的案件立刻回到待审队列。
        """
        cursor = self.col.find(
            {
                "status": "reviewing",
                "claim_deadline_at": {"$ne": None, "$lte": int(moment)},
            },
            {"_id": 1, "assignee": 1, "claimed_at": 1},
        ).limit(limit)
        return await cursor.to_list(length=limit)

    async def recycle(self, case_no: str, at_ms: int) -> Optional[dict]:
        """把超时案件置回 `pending` 并清空认领信息（BR-08-10）。返回 before 文档。

        返回**回收到的那一份**文档（`find_one_and_update` 的 BEFORE 语义）：
        审计要写 `before={assignee, status}`（BR-08-11），回读一次既多一次
        往返、又多一个"期间有人改过"的窗口。
        """
        return await self.col.find_one_and_update(
            {
                "status": "reviewing",
                "claim_deadline_at": {"$ne": None, "$lte": int(at_ms)},
            },
            {
                "$set": {"status": "pending", "recycled_at": int(at_ms)},
                "$unset": {"assignee": "", "claimed_at": "", "claim_deadline_at": "",
                           "dispose_lock": ""},
            },
            return_document=ReturnDocument.BEFORE,
        )

    async def find_archivable(self, deadline: int, limit: int) -> list[dict]:
        """取到期待归档的已处置案件（BR-08-38：`disposed` 后 N 天）。"""
        cursor = self.col.find(
            {"status": "disposed", "disposed_at": {"$ne": None, "$lte": int(deadline)}},
            {"_id": 1, "disposed_at": 1},
        ).limit(limit)
        return await cursor.to_list(length=limit)

    async def find_decisions_without_case(
        self, decision_ids: list[str]
    ) -> list[str]:
        """从一批决策编号里筛出**还没有案件**的那些（补建案用）。

        由调用方（维护任务）用 `decisions` 里的 id 列表来问；这里只回答
        "哪些还没建案"。用 `$in` + 只投影 `decision_id`，一次往返。
        """
        if not decision_ids:
            return []
        cursor = self.col.find(
            {"decision_id": {"$in": [str(d) for d in decision_ids]}},
            {"decision_id": 1},
        )
        rows = await cursor.to_list(length=len(decision_ids))
        done = {str(r.get("decision_id")) for r in rows}
        return [str(d) for d in decision_ids if str(d) not in done]


__all__ = ["CaseRepo"]
