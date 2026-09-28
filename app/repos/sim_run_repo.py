# -*- coding: utf-8 -*-
"""E18 `sim_runs` 仓储：仿真执行记录的写入与查询（模块 10 §6）。

## `sim_runs` 是仿真唯一被写入的集合（Spec BR-10-08）

其余动作全部**只读**：不写 `risk_events` / `decisions` / `decision_hits` /
`risk_cases`、不改名单、不建图、**不写 04 的特征窗口**（BR-10-05/06）。
因此这个文件的存在本身就是那条边界的一部分——凡是需要落库的东西，
它的归属只有这里。

## 写入失败为什么不抛（`SIM-5002`）

Spec §5 把 `SIM-5002`（链路记录落库失败）的 HTTP 定为 **200**：
"结果照常展示，提示可能无法回看"。这个处置是对的——一次 Mongo 抖动不该
让一次**已经算出来**的仿真结论消失（与 05 的 AD-01「落库失败不回滚决策」
同一原则）。因此 `insert` **照常抛**（把"写不进去"这个事实交给调用方），
而"要不要因此让请求失败"由 `SimService` 决定：它捕获异常、置
`record_saved=false`、照常返回结果。

仓储层不吞异常是刻意的：吞掉之后服务层就再也分不清"写成功了"与
"写失败了但被无声忽略"，而 `record_saved` 这个字段正是靠那个异常存在的。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import PyMongoError

from app.constants import COLL_SIM_RUNS
from app.errors import AppError
from app.logging import get_logger
from app.utils.ids import new_sim_run_id

log = get_logger("shop_risk_control.sim_run_repo")

_MSG_READ_UNAVAILABLE = "仿真执行记录暂时不可用，请稍后重试"

#: `GET /sim/runs/{run_id}` 的单次读取上限（`_id` 查询，恒为 0 或 1 条；
#: 这里只是把"绝不可能是正常量"的兜底写出来）。
FIND_LIMIT = 1


def _read_error(op: str, e: Exception) -> AppError:
    log.error("sim_runs 读取失败 op=%s：%s", op, f"{type(e).__name__}: {e}")
    return AppError("COM-5001", _MSG_READ_UNAVAILABLE, 503)


class SimRunRepo:
    """`sim_runs` 的写入与按编号查询。"""

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_SIM_RUNS]

    async def new_id(self, at_ms: Optional[int] = None) -> str:
        """取一个 `SIMR{yyyyMMdd}{12位}` 编号（走 `seq_counters` 的原子序列）。

        为什么在仓储上暴露而服务层不自己拼：E18 的 `_id` 是**落库用的编号**，
        与集合的写入路径同源；把它放在这里，将来换编号规则（例如带分片前缀）
        只需改一处。E17 的用例编号同理（但那条走 `SimCaseRepo` 之外的时机生成，
        见 `SimService.save_case`——它要先校验再取号）。
        """
        return await new_sim_run_id(self.db, at_ms)

    async def insert(self, doc: dict) -> str:
        """写入一条执行记录。**写不进去就抛**（处置见模块 docstring）。"""
        await self.col.insert_one(doc)
        return str(doc["_id"])

    async def find_by_id(self, run_id: str) -> Optional[dict]:
        """按编号取一条（`GET /sim/runs/{run_id}`；查不到由服务层给 `SIM-4004`）。"""
        try:
            return await self.col.find_one({"_id": str(run_id)})
        except PyMongoError as e:
            raise _read_error("find_by_id", e) from e

    async def count_by_case(self, case_id: str) -> int:
        """某个用例被执行过几次（排障与"这个用例跑过没有"的旁证）。"""
        try:
            return int(await self.col.count_documents({"case_id": str(case_id)}))
        except PyMongoError as e:
            raise _read_error("count_by_case", e) from e

    async def list_recent(self, limit: int = 50) -> list[dict]:
        """最近若干条执行记录（按 `run_at` 倒序；供排障与自检脚本比对）。"""
        try:
            cursor = self.col.find({}).sort([("run_at", -1), ("_id", -1)]).limit(int(limit))
            return await cursor.to_list(length=int(limit))
        except PyMongoError as e:
            raise _read_error("list_recent", e) from e


__all__ = ["FIND_LIMIT", "SimRunRepo"]
