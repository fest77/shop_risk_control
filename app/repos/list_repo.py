# -*- coding: utf-8 -*-
"""list_entries 仓储层：只负责与 MongoDB 交互，不含业务判断。

对齐模块 06 §6 文件规划中的 `repos/list_repo.py`（写入侧；读取侧归模块 05，
本切片为验证全链路，一并在本仓储内提供最小只读能力）。
"""
from __future__ import annotations

import uuid
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.constants import COLL_LIST_ENTRIES


def new_entry_id() -> str:
    """名单条目 ID。E07 未规定格式，采用 `L` + 12 位十六进制，保证可读且不冲突。"""
    return "L" + uuid.uuid4().hex[:12].upper()


class ListRepo:
    def __init__(self, db):
        self.col = db[COLL_LIST_ENTRIES]

    # ---------- 读 ----------
    async def find_active(
        self, list_type: str, entity_type: str, entity_value: str
    ) -> Optional[dict]:
        return await self.col.find_one(
            {
                "list_type": list_type,
                "entity_type": entity_type,
                "entity_value": entity_value,
                "status": "active",
            }
        )

    async def find_active_other_lists(
        self, entity_type: str, entity_value: str, exclude_list_type: str
    ) -> list[dict]:
        cursor = self.col.find(
            {
                "entity_type": entity_type,
                "entity_value": entity_value,
                "status": "active",
                "list_type": {"$ne": exclude_list_type},
            }
        )
        return await cursor.to_list(length=10)

    async def query(
        self, flt: dict, sort: list[tuple[str, int]], skip: int, limit: int
    ) -> list[dict]:
        cursor = self.col.find(flt).sort(sort).skip(skip).limit(limit)
        return await cursor.to_list(length=limit)

    async def count(self, flt: dict) -> int:
        return await self.col.count_documents(flt)

    async def counts_by_list_type(self) -> dict[str, int]:
        """三 tab 的 active 计数，一次聚合拿回（模块 06 §3.2 要求附带 counts）。

        注意 pymongo 异步 API 的差异：`find()` 直接返回 AsyncCursor，
        而 `aggregate()` 是**协程**，必须先 await 才能拿到游标。
        """
        cursor = await self.col.aggregate(
            [
                {"$match": {"status": "active"}},
                {"$group": {"_id": "$list_type", "n": {"$sum": 1}}},
            ]
        )
        rows = await cursor.to_list(length=10)
        out = {"black": 0, "white": 0, "gray": 0}
        for r in rows:
            out[r["_id"]] = int(r["n"])
        return out

    # ---------- 写 ----------
    async def insert(self, doc: dict) -> str:
        """插入一条。并发重复时由部分唯一索引 `uq_active_entry` 抛 DuplicateKeyError。"""
        await self.col.insert_one(doc)
        return doc["_id"]

    async def count_active_by_entity(self, entity_type: str, entity_value: str) -> int:
        """某实体当前生效的名单条目数（供二次确认弹窗展示影响面）。"""
        return await self.col.count_documents(
            {"entity_type": entity_type, "entity_value": entity_value, "status": "active"}
        )

    async def find_by_id(self, entry_id: str) -> Optional[dict]:
        """按 id 取条目（无论状态）。移除流程必须先读一次：既要给出 `404 CFG-4011`
        与 `403 CFG-4032`，也要把 `source`/`entity_value` 写进审计的 `before`。"""
        return await self.col.find_one({"_id": entry_id})

    async def soft_remove(self, entry_id: str, removed_at: int, removed_by: str) -> int:
        """软删：**带 `status=active` 前提的条件更新**，返回匹配条数。

        为什么必须是条件更新而不是 `update_one({"_id": id})`：E07 没有 `version`
        字段，删除的乐观锁只能靠"状态前提"表达。匹配 0 条意味着这条在本次请求
        读到它之后被别处改过了（另一个策略师已移除、或清理任务已置 expired），
        必须报 `409 CFG-4006` 而不是静默成功——否则页面会说"移除成功"，
        而库里其实早就不是 active 了，对不上账。
        """
        result = await self.col.update_one(
            {"_id": entry_id, "status": "active"},
            {"$set": {
                "status": "removed",
                "removed_at": removed_at,
                "removed_by": removed_by,
            }},
        )
        return int(result.modified_count)

    async def restore_active(self, entry_id: str) -> int:
        """回滚软删：把条目改回 `active` 并清掉移除痕迹（BR-06-25）。

        条件同样带 `status=removed`：只回滚"本次刚写下的那次软删"。
        若期间有人把它改成了别的状态，本次回滚就不该覆盖别人的变更。
        """
        result = await self.col.update_one(
            {"_id": entry_id, "status": "removed"},
            {"$set": {"status": "active"},
             "$unset": {"removed_at": "", "removed_by": ""}},
        )
        return int(result.modified_count)

    async def delete_by_id(self, entry_id: str) -> int:
        """物理删除（仅用于导入写成功但审计留不下痕迹时的补偿回滚）。"""
        result = await self.col.delete_one({"_id": entry_id})
        return int(result.deleted_count)

    # ---------- 案件处置的自动写入（模块 08 → 06，BR-08-21 / 22） ----------
    async def touch_auto(
        self, entry_id: str, *, reason: str, related_case_no: str,
        effective_at: int, operator: str,
    ) -> int:
        """复用既有 `active` 条目：刷新它的 `reason` / `related_case_no` / `effective_at`。

        这是 BR-08-22 的落点——同 `list_type+entity_type+entity_value` 已有
        `active` 条目时**不新增**（E07 的部分唯一索引 `uq_active_entry` 本来也
        不允许），而是把"这次处置的理由与案件号"写上去。

        **条件带 `status=active`**：只刷新仍然生效的那一条。若它在这条处置的
        名单写入过程中刚好被清理任务置成 `expired`，本次刷新就会匹配 0 条——
        此时服务层按"没写进去"处理（fail-closed 回滚），而不是硬把它改回 active：
        "复活一条已过期条目"与"写一条新条目"对风控的含义完全不同。
        """
        result = await self.col.update_one(
            {"_id": entry_id, "status": "active"},
            {"$set": {
                "reason": str(reason),
                "related_case_no": str(related_case_no),
                "effective_at": int(effective_at),
                "operator": str(operator),
            }},
        )
        return int(result.modified_count)

    # ---------- 过期清理（BR-06-31） ----------
    async def mark_expired(self, now: int, limit: int) -> int:
        """把 `status=active` 且已过期的条目批量置为 `expired`，返回影响条数。

        条件更新一次覆盖一批：清理任务是后台循环，逐条更新会在条目多时
        把一次扫描拖成上千次往返；而"带 `status=active` 前提"保证了它与
        用户侧移除（软删）并发时不会互相覆盖——先到者改状态，后到者匹配 0 条。

        `expire_at: {"$ne": None}` 是必需的：永久条目（黑白名单默认）根本没有
        过期时刻，而 Mongo 在比较时会把 `null` 当作小于任何数字。漏掉这个条件，
        就等于**静默清空全部永久黑名单**——风控最严重的失效形态且无任何报错。

        `limit` 由调用方传入，因为 Mongo 的 `update_many` **没有** limit：只能先
        按条件取一批 `_id` 再更新。这样"单轮最多改多少条"是可配的，异常批量
        （如把秒级时间戳写成 expire_at）不会一次性改掉整个集合。
        """
        cursor = self.col.find(
            {"status": "active", "expire_at": {"$ne": None, "$lte": now}},
            {"_id": 1},
        ).limit(limit)
        rows = await cursor.to_list(length=limit)
        ids = [r["_id"] for r in rows]
        if not ids:
            return 0
        # 第二次仍带 `status=active` 前提：两次往返之间条目可能已被用户移除，
        # 不带前提就会把"用户刚移除"覆盖成 expired，抹掉移除留痕。
        result = await self.col.update_many(
            {"_id": {"$in": ids}, "status": "active"},
            {"$set": {"status": "expired", "expired_at": now}},
        )
        return int(result.modified_count)
