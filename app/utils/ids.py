# -*- coding: utf-8 -*-
"""业务编号与 trace_id 生成（模块 00 §6）。

编号格式**完全取自 `01_数据实体`**，不得各模块自造：

| 编号 | 格式 | 来源 |
|---|---|---|
| 事件 | `EVT{yyyyMMdd}{12位序列}` | E01 `_id` |
| 特征快照 | `SNP{yyyyMMdd}{12位序列}` | E02 `_id` |
| 决策 | `DEC{yyyyMMdd}{12位序列}` | E03 `_id` |
| 案件 | `CASE{yyyyMMdd}{6位序列}` | E08 `_id` |
| 仿真用例 | `SIMC{yyyyMMdd}{6位序列}` | E17 `_id` |
| 仿真执行 | `SIMR{yyyyMMdd}{12位序列}` | E18 `_id` |
| trace | `tr_{yyyyMMdd}_{HHmmss}_{4位随机}` | BR-00-09 |

**序列号为什么落库而不是用进程内计数器**：编号是"每日 12 位序列"，若只用
进程内 `itertools.count`，服务一重启序列就从 0 开始，必然与库里已有编号
撞车（Mongo 主键冲突、或更糟——决策记录被覆盖）。因此这里用 `seq_counters`
集合的原子 `$inc` 取号：`_id = "{前缀}:{日期}"`，天然按天分组、并发安全。
"""
from __future__ import annotations

import secrets
from typing import Any

from pymongo import ReturnDocument

from app.constants import COLL_SEQ_COUNTERS
from app.utils.timeutil import date_key, now_ms, time_key


def new_trace_id(at_ms: int | None = None) -> str:
    """`tr_{yyyyMMdd}_{HHmmss}_{4位随机}`（BR-00-09）。

    后 4 位随机用 `secrets` 而非 `random`：并发下 `random` 的种子碰撞概率
    更高，而 trace_id 重复会直接导致"日志检索定位到别人的请求"。
    """
    ms = now_ms() if at_ms is None else at_ms
    return f"tr_{date_key(ms)}_{time_key(ms)}_{secrets.token_hex(2)}"


async def next_seq_id(
    db: Any,
    prefix: str,
    width: int,
    at_ms: int | None = None,
) -> str:
    """取下一个 `{prefix}{yyyyMMdd}{width位序列}` 编号。

    原子性由 `find_one_and_update($inc)` 保证：单条文档的更新在 Mongo 中是
    原子的，因此并发请求拿到的一定是互不相同的序号。
    """
    ms = now_ms() if at_ms is None else at_ms
    day = date_key(ms)
    counter_id = f"{prefix}:{day}"
    doc = await db[COLL_SEQ_COUNTERS].find_one_and_update(
        {"_id": counter_id},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = int(doc["seq"])
    return f"{prefix}{day}{seq:0{width}d}"


async def new_event_id(db: Any, at_ms: int | None = None) -> str:
    """E01 事件编号 `EVT{yyyyMMdd}{12位}`。"""
    return await next_seq_id(db, "EVT", 12, at_ms)


async def new_snapshot_id(db: Any, at_ms: int | None = None) -> str:
    """E02 特征快照编号 `SNP{yyyyMMdd}{12位}`。"""
    return await next_seq_id(db, "SNP", 12, at_ms)


async def new_decision_id(db: Any, at_ms: int | None = None) -> str:
    """E03 决策编号 `DEC{yyyyMMdd}{12位}`。"""
    return await next_seq_id(db, "DEC", 12, at_ms)


async def new_case_id(db: Any, at_ms: int | None = None) -> str:
    """E08 案件编号 `CASE{yyyyMMdd}{6位}`。"""
    return await next_seq_id(db, "CASE", 6, at_ms)


async def new_sim_case_id(db: Any, at_ms: int | None = None) -> str:
    """E17 仿真用例编号 `SIMC{yyyyMMdd}{6位}`。

    为什么用**当日序列**而不是像 E07 名单条目那样的随机 `{32位十六进制}`：
    用例编号会大量出现在前端与排障对话里（"把 SIMC20260101000001 那条给我看看"），
    而 E17 的 `_id` 没有"不可枚举"的要求——用例是人为保存的模板，条数以个位数计。
    反过来，随机 32 位十六进制在页面上根本没法念、也没法手打。
    """
    return await next_seq_id(db, "SIMC", 6, at_ms)


async def new_sim_run_id(db: Any, at_ms: int | None = None) -> str:
    """E18 仿真执行编号 `SIMR{yyyyMMdd}{12位}`（Spec §3.3 的 `run_id` 以 `SIMR` 开头）。

    12 位序列（而不是用例的 6 位）：批量回放一次就是 200 条，一天下来
    `sim_runs` 的条数会明显多于用例；6 位在批量场景下更容易被耗尽
    （虽然 100 万仍然够用，但两个集合的编号宽度对齐语义：**条数规模差一个量级**）。
    """
    return await next_seq_id(db, "SIMR", 12, at_ms)


def new_opaque_id(prefix: str = "") -> str:
    """无格式约定的编号（如 E07 名单条目 ID）：`{prefix}{32位十六进制}`。

    用随机而非自增：这些集合没有对外暴露的编号语义，随机值不会像自增序列
    那样泄露"系统里一共有多少条数据"。
    """
    return f"{prefix}{secrets.token_hex(16)}"
