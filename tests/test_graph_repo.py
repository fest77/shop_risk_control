# -*- coding: utf-8 -*-
"""E14 边仓储的读写语义（模块 09 §4.2 / §6）。

覆盖：BR-09-08（唯一性 + weight 累加）、BR-09-10（`risk_flag` 不允许自动清除）、
BR-09-16（批量二跳 + 精确过滤）、BR-09-17（截断时要能拿到真实总数）、
BR-09-20（节点属性批量补齐，一条命令）。

这些用例全部直接打仓储层：它们是**数据不变式**（"同一对端点只有一条边"
"风险边不会被洗白"），一旦破了，上层的图与计数会一起给出错误结论，
而页面看起来完全正常。
"""
from __future__ import annotations

import pytest
from pymongo.errors import PyMongoError

from app import db
from app.constants import COLL_ENTITY_EDGES, COLL_IP_POOL, COLL_USERS
from app.repos.graph_repo import GraphRepo, edge_endpoints, other_endpoint
from app.utils.timeutil import now_ms
from tests.profile_testlib import (
    CountingDb,
    address_doc,
    device_doc,
    edge_doc,
    insert_edges,
    ip_doc,
    user_doc,
)

pytestmark = pytest.mark.anyio

NOW = 1_800_000_000_000


def repo() -> GraphRepo:
    return GraphRepo(db.get_db())


# ============================================================ BR-09-08 唯一性
async def test_upsert_edge_accumulates_weight_and_reports_only_first_insert():
    """`V-09-11`：同一 user-device 上报 3 次 → 边数仍 1、`weight=3`。

    同时断言"只有第一次返回 True"——`linked_user_cnt` 的递增完全依赖这个信号
    （BR-09-12），若它每次都返回 True，计数会被重复上报刷爆。
    """
    r = repo()
    flags = [
        await r.upsert_edge("user", "U1", "device", "D1", "used_device", now=NOW + i)
        for i in range(3)
    ]
    assert flags == [True, False, False], "只有首次插入才是新边（计数只该加一次）"
    assert await r.count_edges() == 1, "重复上报不得新增边"
    doc = await r.find_edge("U1", "D1", "used_device")
    assert doc["weight"] == 3, "重复上报必须累加 weight"
    assert doc["first_seen_at"] == NOW, "first_seen_at 必须是首次时刻"
    assert doc["last_seen_at"] == NOW + 2, "last_seen_at 必须前移到最后一次"


async def test_upsert_edge_keeps_risk_flag_and_endpoint_types():
    """BR-09-10：普通上报**不得**把一条已判定风险的边洗白。"""
    r = repo()
    await r.upsert_edge("user", "U1", "device", "D1", "used_device", now=NOW)
    assert await r.mark_edge_risk("U1", "D1", "used_device") is True
    await r.upsert_edge("user", "U1", "device", "D1", "used_device", now=NOW + 10)
    doc = await r.find_edge("U1", "D1", "used_device")
    assert doc["risk_flag"] is True, "普通上报绝不能清除 risk_flag（BR-09-10）"
    assert doc["from_type"] == "user" and doc["to_type"] == "device"


async def test_same_pair_with_different_relation_is_a_different_edge():
    """唯一键是 `from_id + to_id + relation`：同一对端点换关系就是另一条边。"""
    r = repo()
    await r.upsert_edge("user", "U1", "user", "U2", "same_phone", now=NOW)
    await r.upsert_edge("user", "U1", "user", "U2", "transferred_to", now=NOW)
    assert await r.count_edges() == 2


# ============================================================ BR-09-16 查询
async def test_edges_touching_is_bidirectional_and_weight_sorted():
    """1 跳是**双向**查询（实体可能是起点也可能是终点），且按 weight 降序。"""
    r = repo()
    await insert_edges(db.get_db(), [
        edge_doc("user", "U1", "device", "D1", "used_device", 5),
        edge_doc("user", "U2", "device", "D1", "used_device", 9),
        edge_doc("device", "D1", "user", "U3", "used_device", 7),   # 反向也要命中
    ])
    rows = await r.edges_touching("device", "D1", limit=10)
    assert [row["weight"] for row in rows] == [9, 7, 5]
    assert len(rows) == 3


async def test_edges_touching_respects_limit_and_risk_only():
    """`limit` 必须真的生效（截断靠它），`risk_only` 只返回红边。"""
    r = repo()
    await insert_edges(db.get_db(), [
        edge_doc("user", "U1", "device", "D1", "used_device", 1, risk_flag=True),
        edge_doc("user", "U2", "device", "D1", "used_device", 2),
        edge_doc("user", "U3", "device", "D1", "used_device", 3),
    ])
    assert len(await r.edges_touching("device", "D1", limit=2)) == 2
    red = await r.edges_touching("device", "D1", limit=10, risk_only=True)
    assert len(red) == 1 and red[0]["from_id"] == "U1"


async def test_edges_among_pairs_filters_type_id_cross_product_precisely():
    """批量二跳用 `$in` 放宽成"类型 × id"的笛卡尔超集，返回前必须精确过滤。

    这是本仓储最容易出错的一处：放宽过滤是为了**一次往返**取回任意多个一跳节点的边
    （BR-09-16 禁止逐节点查询），但放宽后的超集里会有"类型对不上"的边
    （例如某个 device 的 id 恰好等于某个 ip 字符串）。若不精确过滤，
    图上会凭空出现一条边与一个节点——人工研判会把它当成真实关联。
    """
    r = repo()
    await insert_edges(db.get_db(), [
        # 真正想要的：连着 ("device","X") 这个一跳节点
        edge_doc("user", "U1", "device", "X", "used_device", 1),
        # 超集噪声：`("device","Y")` 与 `("device","Z")` 都不在一跳集合里
        # （Y 只作为 **ip** 出现在一跳集合里），但 `$in` 条件会把它捞出来
        edge_doc("device", "Y", "device", "Z", "used_device", 1),
    ])
    rows = await r.edges_among_pairs(
        [("device", "X"), ("ip", "Y")], limit=50,
    )
    pairs = {edge_endpoints(row) for row in rows}
    assert pairs == {
        (("user", "U1"), ("device", "X")),
    }, f"超集噪声必须被精确过滤掉，实际返回 {pairs}"


async def test_edges_among_pairs_empty_pairs_issues_no_query():
    """一跳集合为空时不该发查询（否则每次孤立账号查询都白花一次往返）。"""
    counter = CountingDb(db.get_db())
    rows = await GraphRepo(counter).edges_among_pairs([], limit=10)
    assert rows == [] and counter.query_count == 0


async def test_count_and_distinct_nodes_returns_exact_totals():
    """截断补真实总数用的一条聚合：边数 + **去重端点**数。"""
    r = repo()
    await insert_edges(db.get_db(), [
        edge_doc("user", "U1", "device", "D1", "used_device", 1),
        edge_doc("user", "U2", "device", "D1", "used_device", 1),
        edge_doc("user", "U1", "device", "D2", "used_device", 1),
    ])
    edges, nodes = await r.count_and_distinct_nodes({})
    assert edges == 3
    # 端点：U1、U2、D1、D2 —— 4 个（不是 6 个"边端点的个数"）
    assert nodes == 4
    edges, nodes = await r.count_and_distinct_nodes({"from_id": "U1"})
    assert (edges, nodes) == (2, 3)


# ============================================================ BR-09-20 属性补齐
async def test_load_node_attributes_fetches_four_collections_in_one_command():
    """四类节点的属性必须**一条命令**取回（`$unionWith`），且类型归属正确。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].replace_one({"_id": "U1"}, user_doc("U1"), upsert=True)
    await test_db["devices"].replace_one({"_id": "D1"}, device_doc("D1"), upsert=True)
    await test_db[COLL_IP_POOL].replace_one(
        {"_id": "1.2.3.4"}, ip_doc("1.2.3.4", region="广东省深圳市"), upsert=True)
    await test_db["user_addresses"].replace_one(
        {"_id": "A1"}, address_doc("A1", "U1"), upsert=True)

    counter = CountingDb(test_db)
    rows, partial = await GraphRepo(counter).load_node_attributes({
        "user": ["U1"], "device": ["D1"], "ip": ["1.2.3.4"], "address": ["A1"],
    })
    assert partial is False
    assert counter.query_count == 1, (
        f"四类节点属性必须一条命令取回，实际发了 {counter.calls}"
    )
    assert set(rows) == {("user", "U1"), ("device", "D1"),
                         ("ip", "1.2.3.4"), ("address", "A1")}
    assert rows[("user", "U1")]["risk_tags"] == ["device_cluster", "blacklist_history"]
    assert rows[("device", "D1")]["os"] == "Android 13"
    assert rows[("ip", "1.2.3.4")]["region"] == "广东省深圳市"
    # 地址的明文相关字段**不在投影里**（BR-09-05：图谱也不需要地址详情）
    assert "detail_hash" not in rows[("address", "A1")]


async def test_load_node_attributes_reports_partial_instead_of_raising():
    """`GRP-5004`：属性补齐失败时如实返回 `partial=True`，**不把整张图变成 500**。"""

    class BrokenDb:
        def __getitem__(self, name):  # noqa: ANN001, ANN204
            return self

        def aggregate(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise PyMongoError("simulated aggregate failure")

    rows, partial = await GraphRepo(BrokenDb()).load_node_attributes({"user": ["U1"]})
    assert rows == {} and partial is True


async def test_load_node_attributes_skips_query_when_no_ids():
    """没有任何节点 id 时不发查询。"""
    counter = CountingDb(db.get_db())
    rows, partial = await GraphRepo(counter).load_node_attributes({})
    assert (rows, partial) == ({}, False)
    assert counter.query_count == 0


# ============================================================ 纯函数
async def test_other_endpoint_returns_none_when_not_connected():
    """`other_endpoint` 连不上时必须返回 `None`（**不许猜一个端点**）。"""
    edge = edge_doc("user", "U1", "device", "D1", "used_device")
    assert other_endpoint(edge, "user", "U1") == ("device", "D1")
    assert other_endpoint(edge, "device", "D1") == ("user", "U1")
    assert other_endpoint(edge, "user", "U999") is None


async def test_edge_docs_have_stable_keys_for_assertions():
    """`edge_endpoints` 的返回顺序固定为 `(from, to)`（断言依赖它）。"""
    edge = edge_doc("user", "U1", "device", "D1", "used_device")
    assert edge_endpoints(edge) == (("user", "U1"), ("device", "D1"))
    assert now_ms() > 0
    assert COLL_ENTITY_EDGES == "entity_edges"
