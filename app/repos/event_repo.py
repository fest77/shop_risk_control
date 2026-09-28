# -*- coding: utf-8 -*-
"""E01 `risk_events` 仓储：写入与详情查询（模块 03 唯一写入的集合）。

## 为什么没有 `event_id` 索引

BR-03-13 要求「以 `risk_events._id` 的唯一索引作为最终兜底」。E01 的主键就是
`_id = event_id`（`EVT{yyyyMMdd}{12位}`），而 MongoDB 的 `_id` **天然唯一且自带
索引**，因此**不需要也不应该**再建一条 `event_id` 索引：多一条索引就多一份写入
开销与一份"两处定义可能不一致"的风险，却提供不了任何额外保障。

## 为什么 TTL 走 `expire_at` 而不是 `ts`

Spec §6 要求「`ts` 的 90 天 TTL」。但 MongoDB 的一个集合只有**一份**
`expireAfterSeconds`，直接建在 `ts` 上就固定了 90 天、无法与其它保留策略共存，
且"索引字段值 + 固定秒数"这种配置在排查时不如显式字段直观。本实现与模块 11 的
`metric_buckets` 保持一致（决策 D37 的同一条理由）：**每条文档写入时自带
`expire_at = received_at + 90d`**，TTL 索引建在 `expire_at` 上
（`expireAfterSeconds=0` 表示"以文档自带的时刻为准"）。
`tests/test_event_gateway.py` 有一条断言钉住"每条文档都有 `expire_at`"，
否则 TTL 索引会因为字段缺失而对该文档永久不生效（过期数据永远留着）。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import PyMongoError

from app.constants import (
    COLL_DECISION_HITS,
    COLL_DECISIONS,
    COLL_FEATURE_SNAPSHOTS,
    COLL_RISK_EVENTS,
)
from app.errors import AppError
from app.logging import get_logger

log = get_logger("shop_risk_control.event.repo")

# 事件保留 90 天（Spec §6 / E01）
EVENT_RETENTION_DAYS = 90
EVENT_RETENTION_MS = EVENT_RETENTION_DAYS * 24 * 60 * 60 * 1000


def expire_at_for(received_at: int) -> int:
    """`risk_events` 文档的 TTL 时刻（写库前算好，见模块 docstring）。"""
    return int(received_at) + EVENT_RETENTION_MS


class EventRepo:
    """`risk_events` 的写入与读取。

    `db` 句柄每次由服务层传入（不缓存），原因同 `audit_repo`：测试会切换数据库，
    缓存集合句柄会让写入落到错误的库上。
    """

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_RISK_EVENTS]

    # ---------------- 写入 ----------------
    async def insert(self, doc: dict) -> str:
        """插入一条事件（`_id` 即 `event_id`，天然唯一兜底）。

        失败时抛 `EVT-5004`（§5：异步落库失败是**内部告警**，不回滚决策、
        不影响 200 响应；详情查询暂时 404 并给重试建议）。
        """
        payload = dict(doc)
        payload.setdefault("expire_at", expire_at_for(payload.get("received_at") or 0))
        try:
            await self.col.insert_one(payload)
        except PyMongoError as e:
            raise AppError(
                "EVT-5004",
                "risk_events 落库失败（决策照常返回，转入重试队列）",
                200,
                {"detail": f"{type(e).__name__}: {e}", "event_id": payload.get("_id")},
            ) from e
        return str(payload["_id"])

    async def exists(self, event_id: str) -> bool:
        """该编号是否已落库（`_id` 唯一索引兜底路径的判定）。"""
        try:
            return await self.col.find_one({"_id": event_id}, {"_id": 1}) is not None
        except PyMongoError as e:
            log.warning("risk_events 查询失败 event_id=%s：%s", event_id, e)
            return False

    # ---------------- 查询 ----------------
    async def find_by_id(self, event_id: str) -> Optional[dict]:
        """按 `_id` 取事件原文（详情接口用）。

        查不到返回 `None`（由服务层翻译成 `EVT-4404` + 重试建议），
        而不是抛异常——"异步落库还没写完"是**正常**状态，不是错误。
        """
        try:
            return await self.col.find_one({"_id": event_id})
        except PyMongoError as e:
            log.warning("risk_events 详情查询失败 event_id=%s：%s", event_id, e)
            return None

    # ---------------- 详情聚合（§3.3：对 E02/E03/E04 只读） ----------------
    # 只读是边界要求（属于 04/05 的集合），因此这里只有 `find_*`，
    # **刻意不提供任何写方法**——本模块对这三个集合没有写权限。
    async def find_snapshot(self, event_id: str) -> Optional[dict]:
        """E02 特征快照（04 写入；本模块只读）。"""
        try:
            return await self.db[COLL_FEATURE_SNAPSHOTS].find_one({"event_id": event_id})
        except PyMongoError:
            return None

    async def find_decision(self, event_id: str) -> Optional[dict]:
        """E03 决策记录（05 写入；本模块只读）。"""
        try:
            return await self.db[COLL_DECISIONS].find_one({"event_id": event_id})
        except PyMongoError:
            return None

    async def find_hits(self, event_id: str) -> list[dict]:
        """E04 决策命中明细（05 写入；本模块只读）。"""
        try:
            cursor = self.db[COLL_DECISION_HITS].find({"event_id": event_id})
            return await cursor.to_list(length=200)
        except PyMongoError:
            return []


__all__ = ["EVENT_RETENTION_DAYS", "EVENT_RETENTION_MS", "EventRepo", "expire_at_for"]
