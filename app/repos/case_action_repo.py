# -*- coding: utf-8 -*-
"""E09 `case_actions` 仓储：处置流水的写入与按案件读取（模块 08 §6）。

## 一条动作一条流水（BR-08-37）

一次处置勾了 3 个动作 → **写 3 条** `case_actions`，各自带自己的
`action_type`；审计只写 1 条汇总的 `case.dispose`。两条要求合起来保证
"每一个动作都可单独追溯"，同时审计不被动作数放大。

## `_id` 为什么是随机流水号而不是"每日序列"

E09 的 `_id` 在 `01_数据实体` 里没有格式约定（只写"流水号"），且**流水不是
业务编号**：它不会被外部引用、不需要"每天从 1 开始"的可读性。
用 `ACT` + 随机十六进制与 E07 名单条目（`L…`）同一口径——比多占一个
`seq_counters` 键更简单，也不会在 `--reset` 之外留下"序号被消耗过"的痕迹。

## `list_by_case` 的排序被 07 冻结（Spec §3.2）

`acted_at` **升序**：处置流水是时间线，倒序会让"先拉黑后封禁"的顺序读起来
是反的。E09 的 `_id` 随机，不承载时间语义，因此排序必须靠 `acted_at`——
这也是 `constants.INDEX_SPECS` 为 `(case_no, acted_at)` 建索引的原因。
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from app.constants import COLL_CASE_ACTIONS
from app.logging import get_logger

log = get_logger("shop_risk_control.case_action_repo")


def new_action_id() -> str:
    """流水号 `ACT` + 12 位十六进制（E09 未规定格式，见模块 docstring）。"""
    return "ACT" + uuid.uuid4().hex[:12].upper()


class CaseActionRepo:
    """`case_actions` 的读写出口（**04/07 复用 `list_by_case`**）。"""

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_CASE_ACTIONS]

    async def new_id(self) -> str:
        return new_action_id()

    async def insert_many(self, docs: list[dict]) -> int:
        """批量写入流水，返回写入条数。

        `ordered=False`：一次处置的 N 条流水之间**没有顺序依赖**，
        一条键冲突不该连带丢弃其余 N-1 条（与 `decision_repo.insert_hits`
        同一取舍）。空列表直接返回 0——pymongo 对空列表抛 `InvalidOperation`，
        而"某个动作没有流水"在 `pass` 这种无副作用动作下是正常的。
        """
        rows = [dict(d) for d in docs]
        if not rows:
            return 0
        result = await self.col.insert_many(rows, ordered=False)
        return len(result.inserted_ids)

    async def find_by_id(self, action_id: str) -> Optional[dict]:
        return await self.col.find_one({"_id": str(action_id)})

    async def list_by_case(self, case_no: str, *, limit: int = 200) -> list[dict]:
        """按 `acted_at` **升序**取该案件的全部流水（Spec §3.2 冻结给 07 的契约）。"""
        cursor = self.col.find({"case_no": str(case_no)}).sort([("acted_at", 1)]).limit(limit)
        return await cursor.to_list(length=limit)

    async def count_by_case(self, case_no: str) -> int:
        return await self.col.count_documents({"case_no": str(case_no)})

    async def list_failed_biz_sync(
        self, case_no: Optional[str] = None, *, limit: int = 200
    ) -> list[dict]:
        """取业务同步失败的流水（BR-08-31 的重试对象，也是重启恢复扫描的数据源）。"""
        flt: dict[str, Any] = {"biz_sync_result.status": "failed"}
        if case_no:
            flt["case_no"] = str(case_no)
        cursor = self.col.find(flt).sort([("acted_at", 1)]).limit(limit)
        return await cursor.to_list(length=limit)

    async def update_biz_sync(self, action_id: str, result: dict) -> int:
        """覆盖 `biz_sync_result` 并把本次尝试**追加**进 `biz_sync_history`。

        为什么要 history：`biz_sync_result` 是"最后一次尝试"的快照，
        而 Spec §3.1 要求 `attempt_no` 递增、且"每次重试**追加**一条审计记录，
        不覆盖原记录"。审计链里追加，业务流水侧留一份历史，
        两边才对得上（只留最后一次，排查时无法回答"前几次为什么失败"）。
        """
        payload = dict(result)
        result_doc = await self.col.find_one_and_update(
            {"_id": str(action_id)},
            {
                "$set": {"biz_sync_result": payload},
                "$push": {"biz_sync_history": {
                    "attempt_no": int(payload.get("attempt_no") or 0),
                    "status": payload.get("status"),
                    "message": payload.get("message"),
                    "at": payload.get("retried_at"),
                }},
            },
        )
        return 1 if result_doc is not None else 0

    async def set_list_writes(self, action_id: str, list_writes: list[dict]) -> int:
        """把本次联动写入的名单条目挂到对应流水上（E09.`list_writes`）。"""
        result = await self.col.update_one(
            {"_id": str(action_id)}, {"$set": {"list_writes": list(list_writes)}}
        )
        return int(result.modified_count)

    async def delete_by_ids(self, action_ids: list[str]) -> int:
        """**物理删除**若干流水。只用于"写入成功但随后的紧操作失败"时的补偿。

        为什么可以物理删：BR-08-30 / DSP-5001 要求"整体失败、本次未生效"。
        留一批孤儿流水会让页面出现"流水说处置了、案件还是审核中"的自相矛盾，
        而这次处置在业务上等于**从未发生**（它的 `case.dispose` 审计也没写）。
        用户的**主动**删除路径不存在（E09 是 append-only 的处置留痕），
        因此本方法不会被任何业务入口调用，只有失败补偿会走到。
        """
        ids = [str(a) for a in action_ids]
        if not ids:
            return 0
        result = await self.col.delete_many({"_id": {"$in": ids}})
        return int(result.deleted_count)


__all__ = ["CaseActionRepo", "new_action_id"]
