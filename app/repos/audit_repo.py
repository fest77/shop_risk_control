# -*- coding: utf-8 -*-
"""E16 `audit_logs` 仓储：插入（含链头读取）、条件查询、分段扫描。

**只读 + 只追加**（BR-12-11）：本文件**不提供任何** `update_*` / `delete_*` 方法。
不是"忘了写"，而是刻意的——审计一旦可改，"不可篡改"就只剩口号。
`tests/test_audit.py` 有一条静态断言守住这一点（扫描 `app/` 下对该集合的写改调用）。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo import DESCENDING
from pymongo.errors import PyMongoError

from app.constants import COLL_AUDIT_LOGS
from app.engine.audit_hash import GENESIS_HASH
from app.utils.ids import next_seq_id


class AuditRepo:
    def __init__(self, db: Any):
        # 保留 db 句柄：业务编号（LOG…）由 seq_counters 集合的原子自增生成，
        # 而那个集合不属于本仓储，需要经 db 访问
        self.db = db
        self.col = db[COLL_AUDIT_LOGS]

    async def next_id(self) -> str:
        """生成 `LOG{yyyyMMdd}{12位序列}`（§3.1 约定 log_id 以 `LOG` 开头）。

        用集中式序列而不是随机值：审计编号的单调性本身就是"只追加"的一个旁证，
        而且人工核对"第 N 条"时能直接对上。
        """
        return await next_seq_id(self.db, "LOG", 12)

    # ---------------- 链头 ----------------
    async def head(self) -> Optional[dict]:
        """取链上最后一条。

        **为什么用 `$natural` 逆序而不是按 `ts` 排序**：审计记录可能在同一毫秒内
        写入多条，按 `ts` 排序时它们的相对顺序是**未定义**的，链头会因此随机漂移，
        表现为"偶发校验失败"。`$natural` 反映真实插入顺序，是 append-only 集合
        唯一可靠的顺序来源。
        """
        return await self.col.find_one({}, sort=[("$natural", -1)])

    async def genesis_or_head_hash(self) -> str:
        """当前应作为新记录 `prev_hash` 的值：空库时为创世哈希（BR-12-01/09）。"""
        last = await self.head()
        return (last or {}).get("hash") or GENESIS_HASH

    # ---------------- 写入 ----------------
    async def insert(self, doc: dict) -> str:
        """插入一条审计记录（只追加）。"""
        await self.col.insert_one(doc)
        return doc["_id"]

    async def find_by_id(self, log_id: str) -> Optional[dict]:
        """按编号读**一条**审计记录（只读，不改不删）。

        用途：业务模块写出一条 `strict=True` 的审计之后，需要在响应里回传
        `{log_id, hash, prev_hash}` 供前端展示"这条留痕的指纹"（模块 08 §3.1
        的 `audit` 字段）。**必须经本方法读**，而不是让业务模块自己去
        `db[COLL_AUDIT_LOGS].find_one(...)`：审计集合的读写口径归模块 12，
        多一处直接访问就多一处"将来改集合名/加字段时漏改"的地方。
        """
        return await self.col.find_one({"_id": str(log_id)})

    # ---------------- 查询 ----------------
    @staticmethod
    def build_filter(
        *,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
    ) -> dict:
        """构造查询条件。

        `action` 支持**前缀匹配**（§3.1）：传 `rule.` 命中 `rule.create`/`rule.update`。
        用 `re.escape` 转义，避免调用方传入正则元字符被当表达式执行。
        """
        import re

        flt: dict[str, Any] = {}
        if actor:
            flt["actor"] = actor
        if action:
            flt["action"] = {"$regex": f"^{re.escape(action)}"}
        if target_type:
            flt["target_type"] = target_type
        if target_id:
            flt["target_id"] = target_id
        if from_ms is not None or to_ms is not None:
            span: dict[str, int] = {}
            if from_ms is not None:
                span["$gte"] = int(from_ms)
            if to_ms is not None:
                span["$lte"] = int(to_ms)
            flt["ts"] = span
        return flt

    async def query(self, flt: dict, page: int, page_size: int) -> tuple[list[dict], int]:
        """按 `ts desc` 分页查询（BR-12-20：不允许按 hash 排序）。"""
        total = await self.col.count_documents(flt)
        cursor = (
            self.col.find(flt)
            .sort("ts", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        return await cursor.to_list(length=page_size), total

    async def scan_chain(self, *, from_seq: int = 0, limit: Optional[int] = None,
                         up_to_total: Optional[int] = None) -> list[dict]:
        """按**插入顺序**取链上的记录（供校验逐条重算）。

        `up_to_total` 是"校验开始时的总条数"：BR-12-02/§3.2 要求校验期间不得把
        新写入的记录计入，否则并发写入会被误报成篡改。因此先用 `count` 固定终点，
        再只取前 N 条。
        """
        cursor = self.col.find({}, sort=[("$natural", 1)]).skip(max(0, from_seq))
        take = limit
        if up_to_total is not None:
            remaining = max(0, up_to_total - max(0, from_seq))
            take = remaining if take is None else min(take, remaining)
        if take is not None:
            cursor = cursor.limit(take)
        return await cursor.to_list(length=take if take is not None else 100_000)

    async def count(self) -> int:
        return await self.col.count_documents({})

    async def scan_filtered(self, flt: dict, limit: int) -> list[dict]:
        """按筛选条件取用于导出的记录（同样按 `ts desc`，与页面顺序一致）。"""
        cursor = self.col.find(flt).sort("ts", DESCENDING).limit(limit)
        return await cursor.to_list(length=limit)

    async def distinct_actors(self, limit: int = 100) -> list[str]:
        """操作人字典（§2.2 的操作人下拉）。"""
        try:
            return sorted(await self.col.distinct("actor"))[:limit]
        except PyMongoError:
            return []
