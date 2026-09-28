# -*- coding: utf-8 -*-
"""模块 05（规则决策引擎）测试的公共助手：事件/规则/名单构造，假仓储，状态复位。

## 为什么单独一个文件（沿用模块 03/04 的踩坑结论）

文件名不以 `test_` 开头，pytest 不会收集它，因此可以被多个测试文件安全导入。
两个**带 autouse 夹具**的测试模块互相导入时，pytest 的夹具缓存会跨模块复用
实例并触发 `assert not self._finalizers`（`tests/event_testlib.py` 记录过这个坑）。

## 为什么助手要能精确控制"命中多少分"

模块 05 的核心命题是**分值累加 + 仲裁分档**（BR-05-15~17）：三档的边界
（59/60、79/80）与截断（>100 记 100）只能靠"构造出精确分值"来验证。
因此 `rule_doc(score=...)` 把分值做成显式参数，而 `install_rules()` 直接写库
（绕过 06 的写路径——本模块只读 `rules`，写路径属 06）。
"""
from __future__ import annotations

from typing import Any, Optional

from app import db as db_module
from app.constants import COLL_DECISIONS, COLL_DECISION_HITS, COLL_LIST_ENTRIES, COLL_RULES
from app.engine import decision as decision_engine
from app.engine import list_filter
from app.utils.timeutil import now_ms

#: 固定锚点（毫秒），让"是否过期"的断言完全确定、不依赖 `now_ms()`
ANCHOR_TS = 1_800_000_000_000


# ============================================================
# 事件构造
# ============================================================
def make_event(
    event_type: str = "login",
    *,
    event_id: str = "EVT20240101000000000001",
    user_id: str = "U000001",
    device_id: Optional[str] = None,
    ip: Optional[str] = None,
    phone: Optional[str] = None,
    address_id: Optional[str] = None,
    ts: Optional[int] = None,
    **extra: Any,
) -> dict:
    """构造一条**内部事件 dict**（形状与 `event_service.validate_event` 的产物一致）。

    刻意不走 HTTP 校验：`decide()` 的用例不该被编号/脱敏/幂等拖慢。
    走真实校验的用例在 `test_engine_api.py` 里（那里必须走，因为接口的入参
    校验本身就是被测对象）。
    """
    event: dict[str, Any] = {
        "_id": event_id,
        "event_type": event_type,
        "user_id": user_id,
        "ts": now_ms() if ts is None else int(ts),
        "received_at": now_ms(),
        "scene_extra": {},
        "source": "mock_biz",
    }
    for key, value in (
        ("device_id", device_id), ("ip", ip), ("phone", phone), ("address_id", address_id),
    ):
        if value is not None:
            event[key] = value
    event.update(extra)
    return event


# ============================================================
# E05 规则构造
# ============================================================
def leaf(field: str, op: str, value: Any = ...) -> dict:
    """叶节点。`value` 省略即**不写这个键**（`exists` 的合法形态）。"""
    node: dict[str, Any] = {"field": field, "op": op}
    if value is not ...:
        node["value"] = value
    return node


def branch(logic: str, *children: dict) -> dict:
    """非叶节点（`and` / `or`）。"""
    return {"logic": logic, "children": list(children)}


def rule_doc(
    rule_code: str,
    condition: dict,
    *,
    score: int = 40,
    scene_code: str = "common",
    name: Optional[str] = None,
    status: str = "enabled",
    priority: int = 10,
    version: int = 1,
) -> dict:
    """构造一条 E05 规则文档（`_id` 即规则编码）。"""
    return {
        "_id": rule_code,
        "name": name or f"测试规则-{rule_code}",
        "scene_code": scene_code,
        "description": "测试用规则",
        "condition": condition,
        "score": score,
        "status": status,
        "priority": priority,
        "version": version,
        "is_system": False,
    }


async def install_rules(*rules: dict) -> list[dict]:
    """把规则写进 `rules` 集合（绕过 06 的写路径：本模块只读）。"""
    col = db_module.get_db()[COLL_RULES]
    for row in rules:
        await col.replace_one({"_id": row["_id"]}, dict(row), upsert=True)
    return list(rules)


async def install_rule_scenes(*codes: str) -> None:
    """写入 E06 场景行（`common` 必须是**真实存在的数据行**，D10）。

    用例里需要它是因为 `rule_repo` 的查询是 `scene_code IN (场景, common)`：
    若 `common` 场景行不存在，只说明"数据里没有通用场景"——但为了不把
    "数据行缺失"与"代码特判"混为一谈，凡涉及 `common` 规则的用例都先灌行。
    """
    from app.constants import COLL_RULE_SCENES

    col = db_module.get_db()[COLL_RULE_SCENES]
    for code in codes:
        await col.replace_one(
            {"_id": code},
            {"_id": code, "name": code, "event_types": [], "sort": 90},
            upsert=True,
        )


# ============================================================
# E07 名单构造
# ============================================================
def list_doc(
    list_type: str,
    entity_type: str,
    entity_value: str,
    *,
    status: str = "active",
    expire_at: Optional[int] = None,
    entry_id: Optional[str] = None,
) -> dict:
    """构造一条 E07 名单条目。"""
    return {
        "_id": entry_id or f"L{list_type[:1].upper()}{entity_type[:2].upper()}{abs(hash(entity_value)) % 10 ** 8:08d}",
        "list_type": list_type,
        "entity_type": entity_type,
        "entity_value": entity_value,
        "reason": "测试用名单条目",
        "source": "manual",
        "effective_at": ANCHOR_TS,
        "expire_at": expire_at,
        "status": status,
        "operator": "tester",
    }


async def install_lists(*rows: dict) -> None:
    """写名单条目并**清空 05 的缓存**（写入侧的真实失效点见 `invalidate_list_cache`）。"""
    col = db_module.get_db()[COLL_LIST_ENTRIES]
    for row in rows:
        await col.replace_one({"_id": row["_id"]}, dict(row), upsert=True)
    list_filter.LIST_CACHE.clear()


# ============================================================
# 假仓储（依赖故障路径）
# ============================================================
class ExplodingListRepo:
    """名单仓储：任何查询都抛异常（V-05-08 的 RUL-5001 路径）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def find_active(self, list_type: str, entity_type: str, entity_value: str):
        self.calls += 1
        raise RuntimeError("simulated mongo failure on list_entries")


class ExplodingRuleRepo:
    """规则仓储：读取即抛异常（RUL-5002 路径）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def list_enabled_rules(self, scene_code: str):
        self.calls += 1
        raise RuntimeError("simulated mongo failure on rules")


class CountingDecisionRepo:
    """记录写入而不真的落库（验证 E03/E04 文档内容，不受 Mongo 时序影响）。"""

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.hits: list[dict] = []
        self.fail = False

    async def new_id(self, at_ms: Optional[int] = None) -> str:
        return f"DEC20240101{len(self.decisions) + 1:012d}"

    async def insert_decision(self, doc: dict) -> str:
        if self.fail:
            raise RuntimeError("simulated decision persist failure")
        self.decisions.append(dict(doc))
        return str(doc["_id"])

    async def insert_hits(self, docs) -> int:
        rows = list(docs)
        self.hits.extend(dict(r) for r in rows)
        return len(rows)


# ============================================================
# 状态复位与落库收尾
# ============================================================
def reset_engine_state() -> None:
    """复位 05 的进程内状态（缓存 / 统计）。

    名单缓存是**进程内 TTL 缓存**（默认 10s，AD-02），而用例之间相隔远小于
    10s：不复位就会出现"上一个用例刚拉黑的人，本用例查出来还在名单里"。
    """
    list_filter.LIST_CACHE.clear()
    decision_engine.reset_stats()


async def flush_decisions(timeout: float = 5.0) -> bool:
    """等待决策落库收尾（AD-01 的异步旁路）。

    任何断言 `decisions`/`decision_hits` 内容的用例**都必须先调它**：
    决策是同步返回、异步落库的，不等待就查库必然偶发地查不到。
    """
    return await decision_engine.flush(timeout)


async def clear_engine_collections() -> None:
    """清空 E03/E04/E05（供不依赖 conftest 的纯单测使用）。"""
    for coll in (COLL_DECISIONS, COLL_DECISION_HITS, COLL_RULES):
        await db_module.get_db()[coll].delete_many({})


__all__ = [
    "ANCHOR_TS",
    "CountingDecisionRepo",
    "ExplodingListRepo",
    "ExplodingRuleRepo",
    "branch",
    "clear_engine_collections",
    "flush_decisions",
    "install_lists",
    "install_rule_scenes",
    "install_rules",
    "leaf",
    "list_doc",
    "make_event",
    "reset_engine_state",
    "rule_doc",
]
