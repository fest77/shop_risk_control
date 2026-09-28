# -*- coding: utf-8 -*-
"""E14 `entity_edges` 仓储：边的 upsert（累加 weight）与双向索引查询（模块 09 §6）。

## 唯一性怎么落地（BR-09-08）

`from_id + to_id + relation` 唯一。落地点是 `constants.INDEX_SPECS` 里的
**唯一索引 `uq_edge`**，本仓储的 upsert 用它作为查询条件：

    update_one({from_id, to_id, relation}, {"$inc": {"weight": 1}, ...}, upsert=True)

于是"重复上报"只有一种结果：命中同一条边、`weight` 累加、`last_seen_at` 前移，
**不会新增边**（`V-09-11`：同一 user-device 上报 3 次 → 边数仍 1、`weight=3`）。

## 为什么返回"是否插入"而不是计数本身

`linked_user_cnt` 这类冗余计数**只允许在边首次插入时递增**（BR-09-08 +
BR-09-12）。`UpdateResult.upserted_id` 是 MongoDB 给出的**权威**插入/命中信号，
用它判断而不是"先查有没有再写"：后者在并发下必然漏判（两个请求都查到"没有"，
双双递增，计数被刷爆）。因此 `upsert_edge()` 返回 `bool`，调用方据此决定
要不要动计数——这条链路上只有一个判定点。

## 为什么并发插入要重试一次

两个并发请求同时 upsert 同一对新端点时，可能双双"没找到"并各自尝试插入，
其中一个会撞上 `uq_edge` 唯一索引（`DuplicateKeyError`）。这是**预期内**的竞态，
不是数据错误：重做一次 update（此时边已存在）即可得到与串行执行完全一致的结果。
若把异常直接抛给调用方，一次正常的并发上报会变成一次 `GRP-5003` 告警。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.constants import (
    COLL_DEVICES,
    COLL_ENTITY_EDGES,
    COLL_IP_POOL,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.errors import ProfileUnavailableError
from app.logging import get_logger

log = get_logger("shop_risk_control.graph.repo")

#: 节点属性批量补齐的实体类型顺序（聚合管道里的 `$unionWith` 顺序固定，
#: 保证同一份数据每次得到同一个结果顺序，便于比对与排查）
ATTRIBUTE_SOURCE_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("user", COLL_USERS),
    ("device", COLL_DEVICES),
    ("ip", COLL_IP_POOL),
    ("address", COLL_USER_ADDRESSES),
)


def _unavailable(detail: str, *, op: str) -> ProfileUnavailableError:
    """图查询依赖故障 → `GRP-5001`（图与画像共用同一个依赖：MongoDB）。"""
    return ProfileUnavailableError(detail=detail, op=op)


def other_endpoint(edge: Mapping[str, Any], entity_type: str,
                   entity_id: str) -> Optional[tuple[str, str]]:
    """取边的**另一端**（给定一端）。

    返回 `None` 表示这条边根本没连着给定的那一端——调用方据此丢弃它，
    而不是"猜一个端点出来"。团伙图谱的正确性依赖这一条：把边另一端猜错，
    图上会出现一个**凭空多出来的账号**，而人工研判会把它当成真实同伙。
    """
    for end in ("from", "to"):
        if str(edge.get(f"{end}_type")) == str(entity_type) and \
                str(edge.get(f"{end}_id")) == str(entity_id):
            other = "to" if end == "from" else "from"
            return str(edge.get(f"{other}_type")), str(edge.get(f"{other}_id"))
    return None


def edge_endpoints(edge: Mapping[str, Any]) -> tuple[tuple[str, str], tuple[str, str]]:
    """边的两个端点 `((from_type, from_id), (to_type, to_id))`。"""
    return (
        (str(edge.get("from_type")), str(edge.get("from_id"))),
        (str(edge.get("to_type")), str(edge.get("to_id"))),
    )


class GraphRepo:
    """`entity_edges` 的唯一读写出口。"""

    def __init__(self, db: Any):
        self.db = db
        self.col = db[COLL_ENTITY_EDGES]

    # ---------------- 边 ID（供日志与排障） ----------------
    @staticmethod
    def edge_key(from_id: str, to_id: str, relation: str) -> str:
        """边的业务标识 `from_id -> to_id (relation)`（**只用于日志/断言**）。

        刻意**不用它当 `_id`**：`_id` 由 MongoDB 生成、唯一性交给 `uq_edge` 索引。
        理由是可读的字符串主键必然要选一个分隔符，而 `device_id`/`address_id`
        的字符集没有约束（BR-03-04 只限长度），任何分隔符都可能被端点里的同名
        字符伪造出碰撞（`("A|B","C")` 与 `("A","B|C")` 会拼成同一个键）。
        唯一约束交给索引表达，就不存在这类"拼接歧义"。
        """
        return f"{from_id} -> {to_id} ({relation})"

    # ---------------- 写入（BR-09-08 / 09-10） ----------------
    async def upsert_edge(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relation: str,
        *,
        now: int,
    ) -> bool:
        """上报一次共现。返回 `True` 表示**这是首次插入**（边是新的）。

        `risk_flag` 只在插入时置 `False`，命中时**绝不触碰**：BR-09-10 要求
        "已判定风险的边不允许自动清除"，任何一次普通上报都不该把一条红边洗白。
        """
        flt = {"from_id": str(from_id), "to_id": str(to_id), "relation": str(relation)}
        update = {
            "$setOnInsert": {
                "from_type": str(from_type),
                "to_type": str(to_type),
                "first_seen_at": int(now),
                "risk_flag": False,
            },
            "$set": {"last_seen_at": int(now)},
            # `weight` 只出现在 `$inc` 里：Mongo 不允许同一字段同时出现在
            # `$setOnInsert` 与 `$inc`（路径冲突），而插入时 `$inc` 会从 0 起算 → 1
            "$inc": {"weight": 1},
        }
        try:
            result = await self.col.update_one(flt, update, upsert=True)
        except DuplicateKeyError:
            # 并发首次插入：另一路已经建好这条边，重做一次更新（见模块 docstring）
            result = await self.col.update_one(flt, update, upsert=True)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="upsert_edge") from e
        return result.upserted_id is not None

    async def mark_edge_risk(self, from_id: str, to_id: str, relation: str) -> bool:
        """把边标记为风险关联（BR-09-10，由 05/08 触发）。

        **只能置 `true`**：没有 `risk_flag=false` 的入口——"红边"表示已经有人
        为这条关联下过结论，自动或手工清除都必须是显式管理操作（与 BR-09-04
        的标签"不自动移除"同源）。
        """
        try:
            result = await self.col.update_one(
                {"from_id": str(from_id), "to_id": str(to_id), "relation": str(relation)},
                {"$set": {"risk_flag": True}},
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="mark_edge_risk") from e
        return int(getattr(result, "modified_count", 0)) > 0

    # ---------------- 读取（BR-09-16 的两批查询） ----------------
    @staticmethod
    def _touching_filter(entity_type: str, entity_id: str) -> dict:
        """双向命中：实体既可能是边的起点，也可能是终点。

        一次 `$or` 而不是查两次：`from_id` 与 `to_id` 各有索引（`ix_from`/`ix_to`），
        Mongo 会对 `$or` 的两个分支分别走索引再合并。这是"1 跳只花一次往返"的关键，
        也是 `V-09-07`（常数次查询）成立的前提。
        """
        return {"$or": [
            {"from_type": entity_type, "from_id": entity_id},
            {"to_type": entity_type, "to_id": entity_id},
        ]}

    async def edges_touching(
        self, entity_type: str, entity_id: str, *, limit: int, risk_only: bool = False,
        sort_by: str = "weight",
    ) -> list[dict]:
        """第 1 跳：与该实体直接相连的全部边（按 `sort_by` 降序，取前 `limit` 条）。

        `sort_by` 有两个受支持的取值，对应两种真实用途：
        - `weight`（默认）：图查询要"关联强度最高的前若干条"（BR-09-17）——
          强关联才是团伙证据；
        - `last_seen_at`：画像卡要"**当前**挂在哪个设备/IP/地址上"——
          半年前刷出来的强关联不该被显示成当前设备（见 `profile_service`）。
        两者不能混用一个排序：按 weight 排会漏掉"最近刚出现但只共现过一次"的
        新设备，而那恰恰是研判最需要看到的。
        """
        flt = self._touching_filter(str(entity_type), str(entity_id))
        if risk_only:
            flt["risk_flag"] = True
        return await self._find_sorted(flt, limit, op="edges_touching", sort_by=sort_by)

    async def edges_among_pairs(
        self, pairs: Iterable[tuple[str, str]], *, limit: int, risk_only: bool = False,
    ) -> list[dict]:
        """第 2 跳：与**任意一个一跳节点**相连的边 —— 一条命令取回全部。

        Spec BR-09-16 要求"先取 1 跳，再**批量**取 2 跳，不允许递归逐节点查询"。
        实现上把 `(type, id)` 集合拆成 `from_type/from_id $in` + `to_type/to_id $in`
        两个分支（各走自己的复合索引），**一次往返**即可覆盖任意多个一跳节点。

        代价是条件放宽成"类型集合 × id 集合"的笛卡尔超集（例如某个 device_id
        恰好等于某个 ip 字符串时会多取到一条）。因此本方法**在返回前按精确的
        `(type, id)` 集合再过滤一遍**——超集只发生在服务器侧，调用方拿到的永远是
        精确结果，且查询次数仍是 1。
        """
        wanted = {(str(t), str(i)) for t, i in pairs}
        if not wanted:
            return []
        types = sorted({t for t, _ in wanted})
        ids = sorted({i for _, i in wanted})
        flt: dict[str, Any] = {"$or": [
            {"from_type": {"$in": types}, "from_id": {"$in": ids}},
            {"to_type": {"$in": types}, "to_id": {"$in": ids}},
        ]}
        if risk_only:
            flt["risk_flag"] = True
        rows = await self._find_sorted(flt, limit, op="edges_among_pairs")
        return [
            row for row in rows
            if (str(row.get("from_type")), str(row.get("from_id"))) in wanted
            or (str(row.get("to_type")), str(row.get("to_id"))) in wanted
        ]

    async def _find_sorted(self, flt: dict, limit: int, *, op: str,
                           sort_by: str = "weight") -> list[dict]:
        """按指定键降序取边（`_id` 升序做稳定的次序兜底）。

        **必须有 `limit`**：`$or` 查询用不上单一的复合索引来排序，Mongo 会在内存里
        排（`SORT` 阶段）。不设上限时，一个大团伙能把整个集合拉进内存排序——
        这正是 BR-09-16 要避免的那类"图一大就崩"的实现。
        `limit` 由服务层按 `max_edges` 传入并留出截断判定所需的余量。
        """
        key = "last_seen_at" if sort_by == "last_seen_at" else "weight"
        try:
            cursor = (
                self.col.find(flt)
                .sort([(key, -1), ("_id", 1)])
                .limit(max(1, int(limit)))
            )
            return await cursor.to_list(length=max(1, int(limit)))
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op=op) from e

    async def find_edge(self, from_id: str, to_id: str,
                        relation: str) -> Optional[dict]:
        """按唯一键取一条边（测试与排障用）。"""
        try:
            return await self.col.find_one(
                {"from_id": str(from_id), "to_id": str(to_id), "relation": str(relation)}
            )
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="find_edge") from e

    async def count_edges(self, flt: Optional[dict] = None) -> int:
        """边数统计（供测试断言"边数仍为 1"与运维核对）。"""
        try:
            return int(await self.col.count_documents(flt or {}))
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}", op="count_edges") from e

    async def count_and_distinct_nodes(self, flt: dict) -> tuple[int, int]:
        """**一条命令**同时算出"边总数"与"去重端点数"（截断时补真实总数用）。

        为什么不能用两次 `count_documents`：去重端点数不是文档数
        （`count_documents` 数的是边），要算"这张图一共有多少个节点"必须把
        每条边的两个端点摊开再按 `(type, id)` 去重——那是聚合的活。
        `$facet` 让"边数"与"端点数"共享同一次 `$match`，因此仍然是**一次往返**，
        这保证了即使发生截断，整张图的查询次数仍是常数 4（`V-09-07` 的 ≤4）。

        **不返回端点明细**：截断场景下的节点可能上万，把它们拉回进程内存正是
        BR-09-16 要避免的事。这里只要两个数字。
        """
        pipeline = [
            {"$match": flt},
            {"$facet": {
                "edges": [{"$count": "n"}],
                "nodes": [
                    {"$project": {"_ends": [
                        {"type": "$from_type", "id": "$from_id"},
                        {"type": "$to_type", "id": "$to_id"},
                    ]}},
                    {"$unwind": "$_ends"},
                    {"$group": {"_id": {"type": "$_ends.type", "id": "$_ends.id"}}},
                    {"$count": "n"},
                ],
            }},
        ]
        try:
            cursor = await self.col.aggregate(pipeline)
            rows = await cursor.to_list(length=1)
        except PyMongoError as e:
            raise _unavailable(f"{type(e).__name__}: {e}",
                               op="count_and_distinct_nodes") from e
        facet = rows[0] if rows else {}
        edges = int(((facet.get("edges") or [{}])[0]).get("n") or 0)
        nodes = int(((facet.get("nodes") or [{}])[0]).get("n") or 0)
        return edges, nodes

    # ---------------- 节点属性批量补齐（BR-09-20，一条命令） ----------------
    async def load_node_attributes(
        self, ids_by_type: Mapping[str, Sequence[str]],
    ) -> tuple[dict[tuple[str, str], dict], bool]:
        """批量取四类节点的属性，返回 `({(type, id): doc}, partial)`。

        **为什么用聚合管道 + `$unionWith` 而不是四次 `find`**：四张表是四个集合，
        常规写法必须各查一次（4 次往返）。`$unionWith`（MongoDB 4.4+，本机 7.0）
        允许在**一条命令**里把另外三个集合的结果并进来，于是"属性补齐"从 4 次
        往返压到 1 次，整张 2 跳图的查询次数固定为 **3 次**且与节点数无关
        （`V-09-07`）。

        `partial=True` 表示本条命令失败（`GRP-5004`）：节点仍会返回，只是
        `risk_level` / `linked_user_cnt` 等属性为 `null`，前端显示「—」并按告警
        排查。**不因为一次属性查询失败就把整张图报成 500**——图结构本身仍然是
        有价值的证据，而"少几个属性"与"看不到图"在研判上是两个量级的损失。
        """
        ids_by_type = {t: list(dict.fromkeys(map(str, ids))) for t, ids in ids_by_type.items()}
        if not any(ids_by_type.values()):
            return {}, False

        first_type, first_coll = ATTRIBUTE_SOURCE_COLLECTIONS[0]
        pipeline: list[dict] = [
            {"$match": {"_id": {"$in": ids_by_type.get(first_type, [])}}}
        ]
        for entity_type, coll in ATTRIBUTE_SOURCE_COLLECTIONS[1:]:
            pipeline.append({"$unionWith": {
                "coll": coll,
                "pipeline": [{"$match": {"_id": {"$in": ids_by_type.get(entity_type, [])}}}],
            }})
        # 只取需要的字段：`detail_hash` 等字段对图谱无用，少传一点就少一次
        # "把敏感字段带出数据库"的机会。
        # `latest_decision` 必须带上：§2.1 规定用户节点的 `risk_level` **来自最近
        # 一次决策**（E10 里没有 `risk_level` 字段），漏掉它会让所有用户节点在图上
        # 显示为"无风险等级"——那正是团伙里最该被标红的那批节点。
        pipeline.append({"$project": {
            "linked_user_cnt": 1, "risk_level": 1, "risk_tags": 1,
            "os": 1, "is_proxy": 1, "is_idc": 1, "region": 1, "isp": 1,
            "first_seen_at": 1, "aftersale_cnt": 1, "province": 1,
            "city": 1, "district": 1, "latest_decision": 1,
        }})
        try:
            cursor = await self.db[first_coll].aggregate(pipeline)
            rows = await cursor.to_list(length=2000)
        except PyMongoError as e:
            log.warning("[GRP-5004] 节点属性批量补齐失败（缺失属性显示 —）：%s", e)
            return {}, True

        # 类型归属靠"节点 id 属于哪个集合"，因此逐类型回填；
        # 同一个 id 出现在两个集合里（理论上不该发生）时以 `ATTRIBUTE_SOURCE_COLLECTIONS`
        # 的顺序为准（user > device > ip > address），保证结果可复现
        out: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = str(row.get("_id"))
            for entity_type, _ in ATTRIBUTE_SOURCE_COLLECTIONS:
                if key in ids_by_type.get(entity_type, []) and (entity_type, key) not in out:
                    out[(entity_type, key)] = row
                    break
        missing = [
            (t, i) for t, ids in ids_by_type.items() for i in ids
            if (t, i) not in out
        ]
        if missing:
            # 节点在边集合里出现、却找不到画像行：**如实记录**（`GRP-5004` 的场景之一）。
            # 不在这里补一行：那会用一个空画像掩盖"画像行缺失"这个真问题。
            log.warning("[GRP-5004] %d 个节点的属性缺失（显示 —），例如 %s",
                        len(missing), missing[:3])
        return out, False


__all__ = ["GraphRepo"]
