# -*- coding: utf-8 -*-
"""E17 `sim_cases` 仓储：仿真用例模板的持久化（模块 10 §6）。

## 只做数据库交互，业务判断全在 `SimService`

与 `RuleAdminRepo` 同一条分层：唯一性、长度、事件体合法性、软删语义都在
服务层，本类只提供读写原语。这样"用例名重复 → `SIM-4003`"这条不变量可以
注入假仓储直接单测，而不必起 Mongo。

唯一的例外是 `insert` 对 `DuplicateKeyError` 的**透传**：那是数据库层的
唯一约束信号（`uq_sim_case_name` 部分唯一索引），必须由服务层决定怎么处置
（翻成 `SIM-4003` 还是"并发下其实是同一次保存"）——在仓储里吞掉它就再也
分不清"重名"与"写库失败"。

## 读失败为什么给 `COM-5001` 而不是 `SIM-`

与 `RuleAdminRepo` 的裁定一致（ER-02：引用别人的码保持原前缀）：用例列表
读不出来只是"页面加载失败"，不是"仿真跑不了"。借 `SIM-5001`（决策引擎不可用）
会让告警侧把一次列表刷新失败统计成一次仿真失败，而两者的处置完全不同。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.constants import COLL_SIM_CASES
from app.errors import AppError
from app.logging import get_logger
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.sim_case_repo")

#: 用例状态（E17）。`active` / `archived`——BR-10-20 规定删除是软删，
#: 保留历史 `sim_runs` 的可追溯性（`run.case_id` 仍能指向那条用例）。
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

#: 用例列表的返回上限。用例是**人工保存**的模板（个位数到几十条），
#: 200 只是一个"绝不可能是正常量"的兜底上界，防止一次查询把内存拉满。
LIST_LIMIT = 200

_MSG_READ_UNAVAILABLE = "仿真用例数据暂时不可用，请稍后重试"


def _read_error(op: str, e: Exception) -> AppError:
    log.error("sim_cases 读取失败 op=%s：%s", op, f"{type(e).__name__}: {e}")
    return AppError("COM-5001", _MSG_READ_UNAVAILABLE, 503)


class SimCaseRepo:
    """`sim_cases` 的读写（模块 10 独占，03/05 不读它）。"""

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_SIM_CASES]

    # ---------------- 读 ----------------
    async def list_active(self, limit: int = LIST_LIMIT) -> list[dict]:
        """列出未归档的用例，按 `created_at` **降序**（新存的排前面）。

        排序键用 `created_at` 而不是 `_id`：`_id` 是当日序列，同一天内与创建
        顺序一致，跨天之后要靠字典序碰运气；而 `created_at` 是唯一"语义上就是
        时间"的字段。第二键 `_id` 保证同一毫秒内创建的两条也有确定顺序。

        **不返回已归档的用例**：BR-10-20 的软删是为了让历史 `sim_runs` 追溯得到，
        不是为了让它们在左栏列表里继续占位（与 `RuleService.list_rules` 过滤
        `deleted` 完全同一口径）。
        """
        try:
            cursor = (
                self.col.find({"status": STATUS_ACTIVE})
                .sort([("created_at", -1), ("_id", 1)])
                .limit(int(limit))
            )
            return await cursor.to_list(length=int(limit))
        except PyMongoError as e:
            raise _read_error("list_active", e) from e

    async def count_active(self) -> int:
        """未归档用例条数（列表的 `total`）。"""
        try:
            return int(await self.col.count_documents({"status": STATUS_ACTIVE}))
        except PyMongoError as e:
            raise _read_error("count_active", e) from e

    async def find_by_id(self, case_id: str) -> Optional[dict]:
        """按编号取一条（**含已归档**：历史 `sim_runs` 的用例回填要能读到它）。"""
        try:
            return await self.col.find_one({"_id": str(case_id)})
        except PyMongoError as e:
            raise _read_error("find_by_id", e) from e

    async def find_active_by_name(self, name: str) -> Optional[dict]:
        """按名字取未归档用例（`SIM-4003` 的友好判定，真正的防线是唯一索引）。"""
        try:
            return await self.col.find_one({"name": name, "status": STATUS_ACTIVE})
        except PyMongoError as e:
            raise _read_error("find_active_by_name", e) from e

    # ---------------- 写 ----------------
    async def insert(self, doc: dict) -> str:
        """插入一条用例。并发重名时由 `uq_sim_case_name` 抛 `DuplicateKeyError`。"""
        await self.col.insert_one(doc)
        return str(doc["_id"])

    async def archive(self, case_id: str) -> int:
        """软删（BR-10-20）：`status=archived`，文档保留。

        **本模块不暴露 DELETE 端点**（Spec §3 的接口清单里没有），但仓储层提供
        这个方法：BR-10-20 是**业务规则**，而"没有端点"只是当前接口面的选择。
        把它写在这里而不是等将来实现端点时再补，是为了让"删除是软删"这件事
        在代码里有一个具体落点，而不是一句口头约定。
        """
        try:
            result = await self.col.update_one(
                {"_id": str(case_id), "status": STATUS_ACTIVE},
                {"$set": {"status": STATUS_ARCHIVED, "archived_at": now_ms()}},
            )
            return int(result.modified_count)
        except PyMongoError as e:
            raise _read_error("archive", e) from e

    async def delete_by_id(self, case_id: str) -> int:
        """**物理删除**。仅用于"插入成功但审计留不下痕迹"时的补偿回滚。

        与 `RuleAdminRepo.delete_by_code` 同一立场：这次写入业务上等于**从未发生**
        （D41 / BR-06-36：宁可不做，不可无痕地做），留一条孤儿用例反而会让
        "库里为什么有一条没审计的用例"成为排查负担。用户的主动删除走软删。
        """
        try:
            result = await self.col.delete_one({"_id": str(case_id)})
            return int(result.deleted_count)
        except PyMongoError as e:
            raise _read_error("delete_by_id", e) from e


__all__ = [
    "LIST_LIMIT",
    "STATUS_ACTIVE",
    "STATUS_ARCHIVED",
    "DuplicateKeyError",
    "SimCaseRepo",
]
