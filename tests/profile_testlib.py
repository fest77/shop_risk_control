# -*- coding: utf-8 -*-
"""模块 09 测试的公共助手：可计数的 DB 代理、必失败依赖、画像/边数据构造。

## 为什么要"可计数的 DB 代理"（`V-09-07`）

`V-09-07` 要求证明图查询**不是 N+1**：对 2 跳图统计 MongoDB 查询次数，
断言为常数次（≤4）而非随节点增长。要证明这一点只能**真的数**——
"看起来像常数"的假断言（例如数一数代码里写了几次 `find`）在重构后会立刻失效，
而它保护的正是"图一大就崩"这类最贵的缺陷。

计数口径写明在 `CountingDb` 上：只数**向 Mongo 发出的查询命令**
（`find` / `find_one` / `aggregate` / `count_documents`）。游标的 `to_list()`
不另计一次：MongoDB 的 `find` 首次取回就带回了第一批文档（默认 batch），
把它算两次会让"查询次数"与真实的往返次数不符。

## 为什么必失败的依赖要单独造一个类

`GRP-5001`（画像聚合查询失败 → 503）与 `GRP-4004`（用户不存在 → 404）是
**两条完全不同的响应**，而二者的触发点都在仓储层。用"注入一个必失败的仓储"
来验证 503，比 `monkeypatch` 掉 `find_one` 更接近真实故障（真实故障发生在
驱动层：连接断开、超时），也不会因为仓储内部实现变化而失去意义。
"""
from __future__ import annotations

from typing import Any, Optional

from app.constants import (
    COLL_DEVICES,
    COLL_ENTITY_EDGES,
    COLL_IP_POOL,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.errors import ProfileUnavailableError
from app.utils.timeutil import now_ms
from pymongo.errors import PyMongoError

#: 测试用的固定用户编号（与 `scripts/seed.py` 的演示用户一致，便于对照）
DEMO_USER = "U000128"
DEMO_DEVICE = "D8F2A1C4"
DEMO_IP = "117.136.12.88"
DEMO_ADDRESS = "ADDR-7712"


# ============================================================
# 可计数的 DB 代理
# ============================================================
#: 会被计数的**查询命令**（读操作）。写操作不计：`V-09-07` 关心的是"图查询"
#: 的往返次数，而图查询按定义是只读的（BR-09-19）
COUNTED_OPS: frozenset[str] = frozenset({
    "find", "find_one", "aggregate", "count_documents",
})


class CountingCollection:
    """集合代理：转发一切属性访问，并记录查询命令。"""

    def __init__(self, owner: "CountingDb", name: str, col: Any) -> None:
        # 用 `object.__setattr__` 之外的方式赋值会触发自己的 `__getattr__`
        self.__dict__["_owner"] = owner
        self.__dict__["_name"] = name
        self.__dict__["_col"] = col

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self.__dict__["_col"], item)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if item in COUNTED_OPS:
                self.__dict__["_owner"].calls.append(f"{self.__dict__['_name']}.{item}")
            return attr(*args, **kwargs)

        return wrapper


class CountingDb:
    """数据库代理：`calls` 里是逐次查询命令，`query_count` 是次数。"""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.calls: list[str] = []

    def __getitem__(self, name: str) -> CountingCollection:
        return CountingCollection(self, name, self._db[name])

    @property
    def query_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()


class ExplodingRepo:
    """画像仓储的故障替身：任何读都抛 `GRP-5001`（依赖故障，不是"查不到"）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_user(self, user_id: str) -> Optional[dict]:
        self.calls += 1
        raise ProfileUnavailableError(detail="simulated mongo down", op="get_user")

    async def get_device(self, device_id: str) -> Optional[dict]:
        raise ProfileUnavailableError(detail="simulated mongo down", op="get_device")

    async def get_ip(self, ip: str) -> Optional[dict]:
        raise ProfileUnavailableError(detail="simulated mongo down", op="get_ip")

    async def get_address(self, address_id: str) -> Optional[dict]:
        raise ProfileUnavailableError(detail="simulated mongo down", op="get_address")

    async def find_address_of_user(self, user_id: str) -> Optional[dict]:
        raise ProfileUnavailableError(detail="simulated mongo down",
                                      op="find_address_of_user")

    async def find_users_by_phone(self, *args: Any, **kwargs: Any) -> list[dict]:
        raise ProfileUnavailableError(detail="simulated mongo down",
                                      op="find_users_by_phone")


class ExplodingGraphRepo:
    """图仓储的故障替身：任何查询都抛 `GRP-5001`。"""

    def __init__(self) -> None:
        self.calls = 0

    async def edges_touching(self, *args: Any, **kwargs: Any) -> list[dict]:
        self.calls += 1
        raise ProfileUnavailableError(detail="simulated mongo down", op="edges_touching")

    async def edges_among_pairs(self, *args: Any, **kwargs: Any) -> list[dict]:
        raise ProfileUnavailableError(detail="simulated mongo down", op="edges_among_pairs")

    async def load_node_attributes(self, ids_by_type: Any) -> tuple[dict, bool]:
        raise ProfileUnavailableError(detail="simulated mongo down",
                                      op="load_node_attributes")


class FailingGraphRepo:
    """写边永远失败的图仓储（BR-09-11：入重试队列 + 告警，**不影响决策**）。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def upsert_edge(self, *args: Any, **kwargs: Any) -> bool:
        self.attempts += 1
        raise PyMongoError("simulated edge write failure")


# ============================================================
# 数据构造（直接写测试库，绕开服务层，便于精确控制形状）
# ============================================================
def user_doc(user_id: str = DEMO_USER, *, now: Optional[int] = None, **over: Any) -> dict:
    """一份"内容饱满"的 E10 文档（覆盖 §3.1 的每个字段）。"""
    moment = now if now is not None else now_ms()
    doc: dict[str, Any] = {
        "_id": user_id,
        "phone": "139****0001",
        "register_at": moment - 42 * 86_400_000,
        "level": "normal",
        "status": "active",
        "risk_tags": ["device_cluster", "blacklist_history"],
        "risk_score_history": [{"score": 55.0, "decided_at": moment - 3_600_000}],
        "stat": {"order_cnt": 3, "aftersale_cnt": 1, "block_cnt": 1, "total_amount": 20000},
        "latest_decision": {"risk_score": 72.0, "risk_level": "high",
                            "decision": "review", "decided_at": moment - 60_000},
    }
    doc.update(over)
    return doc


def device_doc(device_id: str = DEMO_DEVICE, *, now: Optional[int] = None, **over: Any) -> dict:
    moment = now if now is not None else now_ms()
    doc = {
        "_id": device_id, "fingerprint": "fp-demo",
        "first_seen_at": moment - 86_400_000, "last_seen_at": moment,
        "os": "Android 13", "ua": "Mozilla/5.0",
        "linked_user_cnt": 1, "risk_level": "high",
    }
    doc.update(over)
    return doc


def ip_doc(ip: str = DEMO_IP, *, now: Optional[int] = None, **over: Any) -> dict:
    moment = now if now is not None else now_ms()
    doc = {
        "_id": ip, "region": "湖南省长沙市", "isp": "中国移动",
        "is_proxy": False, "is_idc": False, "linked_user_cnt": 1,
        "first_seen_at": moment - 86_400_000,
    }
    doc.update(over)
    return doc


def address_doc(address_id: str = DEMO_ADDRESS, user_id: str = DEMO_USER, **over: Any) -> dict:
    doc = {
        "_id": address_id, "user_id": user_id, "receiver": "张伟",
        "phone": "139****0001", "province": "湖南省", "city": "长沙市",
        "district": "岳麓区", "detail_hash": "hash-demo",
        "linked_user_cnt": 1, "aftersale_cnt": 2,
    }
    doc.update(over)
    return doc


def edge_doc(from_type: str, from_id: str, to_type: str, to_id: str, relation: str,
             weight: int = 1, *, risk_flag: bool = False,
             last_seen_at: Optional[int] = None) -> dict:
    moment = now_ms() if last_seen_at is None else last_seen_at
    return {
        "from_type": from_type, "from_id": from_id,
        "to_type": to_type, "to_id": to_id,
        "relation": relation, "weight": weight,
        "first_seen_at": moment - 1000, "last_seen_at": moment,
        "risk_flag": risk_flag,
    }


async def insert_profile(db: Any, *, user: bool = True, device: bool = True,
                         ip: bool = True, address: bool = True) -> None:
    """把四类画像塞进测试库（默认全塞，便于组装 §3.1 的完整响应）。"""
    if user:
        await db[COLL_USERS].replace_one({"_id": DEMO_USER}, user_doc(), upsert=True)
    if device:
        await db[COLL_DEVICES].replace_one({"_id": DEMO_DEVICE}, device_doc(), upsert=True)
    if ip:
        await db[COLL_IP_POOL].replace_one({"_id": DEMO_IP}, ip_doc(), upsert=True)
    if address:
        await db[COLL_USER_ADDRESSES].replace_one(
            {"_id": DEMO_ADDRESS}, address_doc(), upsert=True,
        )


async def insert_edges(db: Any, edges: list[dict]) -> None:
    for doc in edges:
        await db[COLL_ENTITY_EDGES].replace_one(
            {"from_id": doc["from_id"], "to_id": doc["to_id"],
             "relation": doc["relation"]},
            dict(doc), upsert=True,
        )


async def insert_edges_touching(db: Any, device_id: str, user_ids: list[str],
                                *, relation: str = "used_device",
                                weight_of: Any = None) -> list[dict]:
    """批量造 `user → device` 的边（团伙规模的构造器，`V-09-04/05/07/08` 都用它）。

    `weight_of(user_id, index)` 可自定义权重；默认 `1`。
    """
    edges = []
    for index, user_id in enumerate(user_ids):
        weight = 1 if weight_of is None else int(weight_of(user_id, index))
        edges.append(edge_doc("user", user_id, "device", device_id,
                              relation, weight))
    await insert_edges(db, edges)
    return edges


async def fan_out_cluster(db: Any, *, hub_device: str, users: list[str],
                          spokes_per_user: int = 1) -> None:
    """构造"1 个中心设备 + N 个账号 + 每个账号各自一台设备"的两跳结构。

    用于 `V-09-07`：节点数随 `users` 增长，而查询次数必须**不变**。
    """
    edges: list[dict] = []
    for index, user_id in enumerate(users):
        edges.append(edge_doc("user", user_id, "device", hub_device, "used_device",
                              1 + index % 3))
        for spoke in range(spokes_per_user):
            edges.append(edge_doc("user", user_id, "device",
                                  f"D-{user_id}-{spoke}", "used_device", 1))
    await insert_edges(db, edges)


__all__ = [
    "COUNTED_OPS",
    "CountingCollection",
    "CountingDb",
    "DEMO_ADDRESS",
    "DEMO_DEVICE",
    "DEMO_IP",
    "DEMO_USER",
    "ExplodingGraphRepo",
    "ExplodingRepo",
    "FailingGraphRepo",
    "address_doc",
    "device_doc",
    "edge_doc",
    "fan_out_cluster",
    "insert_edges",
    "insert_edges_touching",
    "insert_profile",
    "ip_doc",
    "user_doc",
]
