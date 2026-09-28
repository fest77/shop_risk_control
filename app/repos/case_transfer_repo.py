# -*- coding: utf-8 -*-
"""E08 `risk_cases` 的**只读/转交**访问口（模块 13 专用，BR-13-15 / BR-13-17）。

## 为什么单独一个文件，而不是加进模块 08 的 `case_repo`

模块 08 的 `case_repo` 承载的是**处置状态机**（认领、锁、处置、超时回收），
它的每个方法都与 08 的业务不变量绑定。本模块只需要问两个问题——
"这个审核员名下还有几个没处置完的案件"与"把它们转给别人"——把它们塞进
`case_repo` 会让那个文件同时承担两个模块的语义，而将来 08 改状态机时
很容易顺手改坏这两条与它无关的查询。

因此这里**只做这两件事**，且刻意**不碰** `status`：转交只改 `assignee`
（案件仍在原状态，处置流程不被打断）。**不由本模块写 E14 的
`transferred_to` 边**：图的写入口是 09 的 `edge_writer`（BR-09-11/12 要求
冗余计数与边一致），绕过它写边会让 `linked_user_cnt` 与边数对不上。
"""
from __future__ import annotations

from typing import Any

from pymongo.errors import PyMongoError

from app.constants import COLL_RISK_CASES
from app.errors import AccountWriteFailedError

#: "未处置"的状态集合（E08 `status` 的四态里前两态）
PENDING_STATUSES: tuple[str, ...] = ("pending", "reviewing")

#: 转交清单回给页面的字段（够页面选案件即可，不回传整份案件文档）
LIST_FIELDS: dict[str, int] = {
    "_id": 1, "status": 1, "assignee": 1, "risk_level": 1,
    "user_id": 1, "created_at": 1, "decision": 1, "final_score": 1,
}

#: 一次响应最多回传多少条待办案件（防"某审核员积压几千条"把响应撑爆）
PENDING_LIST_LIMIT = 50


class CaseTransferRepo:
    def __init__(self, db: Any):
        self.col = db[COLL_RISK_CASES]

    def _pending_filter(self, username: str) -> dict:
        return {"assignee": str(username), "status": {"$in": list(PENDING_STATUSES)}}

    async def count_pending(self, username: str) -> int:
        """该账号名下未处置案件数（BR-13-15 的停用前置条件、BR-13-17 的删除前置条件）。"""
        try:
            return int(await self.col.count_documents(self._pending_filter(username)))
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"查询待处置案件失败：{type(e).__name__}: {e}"
            ) from e

    async def list_pending(self, username: str, limit: int = PENDING_LIST_LIMIT) -> list[dict]:
        """待处置案件清单（`SYS-4007` 要回传给页面做转交选择）。"""
        try:
            cursor = (
                self.col.find(self._pending_filter(username), LIST_FIELDS)
                .sort("created_at", -1)
                .limit(int(limit))
            )
            rows = await cursor.to_list(length=int(limit))
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"查询待处置案件失败：{type(e).__name__}: {e}"
            ) from e
        return [
            {
                "case_no": str(r.get("_id")),
                "status": r.get("status"),
                "risk_level": r.get("risk_level"),
                "user_id": r.get("user_id"),
                "created_at": r.get("created_at"),
                "decision": r.get("decision"),
                "final_score": r.get("final_score"),
            }
            for r in rows
        ]

    async def transfer_pending(self, from_user: str, to_user: str, ts: int) -> int:
        """把 `from_user` 名下全部未处置案件转给 `to_user`，返回转交条数。

        **只改 `assignee`**（外加两个可追溯字段）：状态、认领时间、超时截止时间
        一律不动——转交不是"重新认领"，把它做成 `status=pending` + 清空
        `claimed_at` 会让案件莫名退回待认领队列（正在处置的人一刷新就发现
        自己的案件没了）。`transferred_from` / `transferred_at` 让"这条案件
        为什么换了人"在数据上可回答，而不是只在审计里。
        """
        try:
            result = await self.col.update_many(
                self._pending_filter(from_user),
                {"$set": {
                    "assignee": str(to_user),
                    "transferred_from": str(from_user),
                    "transferred_at": int(ts),
                }},
            )
        except PyMongoError as e:
            raise AccountWriteFailedError(
                f"案件转交失败：{type(e).__name__}: {e}"
            ) from e
        return int(result.modified_count)

    async def revert_transfer(self, from_user: str, to_user: str, ts: int) -> bool:
        """补偿：把**本次**转交出去的案件还回去（`transferred_at == ts` 才匹配）。

        条件里带 `transferred_at`，因此只回滚本次那一批，不会把别人后来的
        转交一并撤销。停用账号的最后一步失败（或"降级后没有管理员了"）时用它
        把状态还原，避免留下"案件已经转走、账号其实没停用"的半成品。
        """
        try:
            result = await self.col.update_many(
                {"assignee": str(to_user), "transferred_from": str(from_user),
                 "transferred_at": int(ts)},
                {"$set": {"assignee": str(from_user)},
                 "$unset": {"transferred_from": "", "transferred_at": ""}},
            )
        except PyMongoError:
            return False
        return int(result.modified_count) > 0
