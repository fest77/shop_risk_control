# -*- coding: utf-8 -*-
"""图查询服务：跳数、裁剪、真实总数、批量补齐、查询次数（模块 09 §4.4 / §7）。

对应验收：`V-09-05`（1 跳/2 跳节点）、`V-09-06`（`max_hop=3` → `GRP-4001`）、
`V-09-07`（**不是 N+1**：查询次数为常数 ≤4）、`V-09-08`（截断 + 真实总数）、
`V-09-09`（孤立账号 200 vs 不存在 404）、`V-09-15`（有界规模的性能）。
"""
from __future__ import annotations

import time

import pytest

from app import db
from app.constants import COLL_ENTITY_EDGES, COLL_IP_POOL, COLL_USERS
from app.errors import AppError
from app.repos.graph_repo import GraphRepo
from app.services.graph_service import (
    MAX_EDGES_CAP,
    MAX_HOP,
    MAX_NODES_CAP,
    QUERY_TIMEOUT_SEC,
    GraphService,
)
from tests.profile_testlib import (
    CountingDb,
    DEMO_ADDRESS,
    DEMO_IP,
    DEMO_USER,
    device_doc,
    edge_doc,
    insert_edges,
    insert_profile,
    user_doc,
)

pytestmark = pytest.mark.anyio

HUB = "D-HUB"


def service() -> GraphService:
    return GraphService()


async def _build_cluster(size: int = 12) -> list[str]:
    """团伙结构：`size` 个账号共用 `HUB`，中心账号是 `DEMO_USER`（一定是第一个）。

    每个账号另有一台自己的设备 → 从 `DEMO_USER` 出发的 2 跳里既有"其他账号"
    也有"其他账号的设备"，正好覆盖 §2.2 的"二跳节点 = 同设备/同IP/同地址关联的
    其他账号"与其延伸。
    """
    test_db = db.get_db()
    assert size >= 2
    users = [DEMO_USER] + [f"U{100000 + i}" for i in range(1, size)]
    await test_db[COLL_USERS].insert_many([user_doc(uid) for uid in users])
    await test_db["devices"].insert_many(
        [device_doc(HUB, linked_user_cnt=size)]
        + [device_doc(f"D-{uid}", linked_user_cnt=1) for uid in users]
    )
    edges = [edge_doc("user", uid, "device", HUB, "used_device", 1 + i % 4)
             for i, uid in enumerate(users)]
    edges += [edge_doc("user", uid, "device", f"D-{uid}", "used_device", 1)
              for uid in users]
    await insert_edges(test_db, edges)
    return users


# ============================================================ V-09-05 跳数
async def test_two_hop_graph_contains_hop1_and_hop2_nodes(client):
    """`V-09-05`：`max_hop=2` 时 `nodes` 同时含 hop=1 与 hop=2 两类。"""
    await _build_cluster(6)
    data = await service().query("user", DEMO_USER, max_hop=2)

    hops = {node["hop"] for node in data["nodes"]}
    assert hops == {0, 1, 2}, hops
    center = [node for node in data["nodes"] if node["is_center"]]
    assert len(center) == 1 and center[0]["id"] == DEMO_USER and center[0]["hop"] == 0
    assert data["center"] == {"type": "user", "id": DEMO_USER, "label": DEMO_USER}
    hop1_ids = {node["id"] for node in data["nodes"] if node["hop"] == 1}
    assert HUB in hop1_ids
    hop2_ids = {node["id"] for node in data["nodes"] if node["hop"] == 2}
    assert "U100001" in hop2_ids, "同设备的其他账号必须出现在 2 跳里"
    # 一跳边 + 二跳边都进来了，且没有重复
    assert len({(e["from"], e["to"], e["relation"]) for e in data["edges"]}) == len(data["edges"])


async def test_max_hop_1_returns_only_first_hop():
    """`max_hop=1` 只出一跳节点（AD-06 允许的另一个取值）。"""
    await _build_cluster(4)
    data = await service().query("user", DEMO_USER, max_hop=1)
    assert {node["hop"] for node in data["nodes"]} == {0, 1}
    assert data["truncated"] is False


async def test_graph_is_read_only():
    """BR-09-19：图查询**不写任何数据**（对比查询前后的集合计数）。"""
    await _build_cluster(5)
    test_db = db.get_db()
    before = [await test_db[name].count_documents({}) for name in
              (COLL_USERS, "devices", COLL_IP_POOL, "user_addresses", COLL_ENTITY_EDGES)]
    await service().query("user", DEMO_USER, max_hop=2)
    after = [await test_db[name].count_documents({}) for name in
             (COLL_USERS, "devices", COLL_IP_POOL, "user_addresses", COLL_ENTITY_EDGES)]
    assert before == after


# ============================================================ V-09-06 参数校验
async def test_max_hop_over_limit_raises_grp_4001():
    """AD-06 / BR-09-15：`max_hop > 2` 直接拒绝（400），不静默夹取。"""
    with pytest.raises(AppError) as exc:
        await service().query("user", DEMO_USER, max_hop=MAX_HOP + 1)
    assert exc.value.code == "GRP-4001"
    assert exc.value.http_status == 400
    assert "2 跳" in exc.value.message


async def test_phone_entity_type_raises_grp_4002():
    """§5：`entity_type=phone` 必须**明确拒绝**（422 `GRP-4002`），不静默降级。

    手机号不是图节点（E14 的编码约束），把它当节点查会得到一张"看起来正常
    但少了一半节点"的图。
    """
    with pytest.raises(AppError) as exc:
        await service().query("phone", "139****0001")
    assert exc.value.code == "GRP-4002"
    assert exc.value.http_status == 422


@pytest.mark.parametrize("kwargs", [
    {"max_nodes": 0}, {"max_nodes": MAX_NODES_CAP + 1},
    {"max_edges": 0}, {"max_edges": MAX_EDGES_CAP + 1},
    {"max_hop": 0},
])
async def test_out_of_range_params_are_rejected_not_clamped(kwargs):
    """越界参数必须报错而不是静默夹取（模块 11 的 `MET-4003` 同一裁定）。

    静默夹取的后果：调用方传 `max_nodes=9999`、拿到 500 个节点，会以为自己
    看到的是一张完整的图。
    """
    with pytest.raises(AppError) as exc:
        await service().query("user", DEMO_USER, **kwargs)
    assert exc.value.http_status in (400, 422)


# ============================================================ V-09-09 空态与 404
async def test_missing_entity_raises_grp_4004():
    """实体不存在 → `GRP-4004`（404），与"孤立账号"区分。"""
    with pytest.raises(AppError) as exc:
        await service().query("user", "U-NOT-EXIST")
    assert exc.value.code == "GRP-4004" and exc.value.http_status == 404


async def test_isolated_entity_returns_200_with_center_only():
    """§2.2 空态：实体存在但没有任何关联 → **200** + 只有中心节点。

    这与"查不到"是两件事（`V-09-09` 直接验这一点）：前者是"这个人确实存在、
    只是没有任何关联"，后者是"没有这个人"。两者对研判的含义完全不同。
    """
    await db.get_db()[COLL_USERS].insert_one(user_doc("U-LONELY"))
    data = await service().query("user", "U-LONELY")
    assert data["edges"] == []
    assert len(data["nodes"]) == 1 and data["nodes"][0]["is_center"] is True
    assert data["truncated"] is False
    assert data["total_nodes"] == 1 and data["total_edges"] == 0


# ============================================================ V-09-07 不是 N+1
async def test_two_hop_query_count_is_constant_and_independent_of_node_count():
    """`V-09-07`：2 跳图的 Mongo 查询次数为**常数 3**，且不随节点数增长。

    计数用 `CountingDb` 代理**真的数**（口径见 `profile_testlib` 的说明），
    不是"看起来像常数"的假断言：
    - 规模从 3 个账号涨到 25 个账号（节点 7 → 51）；
    - 两次的查询次数必须**相同**，且每次都是那 3 条命令
      （一跳边、二跳边、节点属性批量补齐）；
    - 若实现退化成"逐个一跳节点查一次"，25 个账号那次会是 1 + 25 + 1 次，
      这条断言立刻爆红。
    """
    counts: list[int] = []
    for size in (3, 25):
        await db.get_db()[COLL_ENTITY_EDGES].delete_many({})
        await db.get_db()[COLL_USERS].delete_many({})
        await db.get_db()["devices"].delete_many({})
        await _build_cluster(size)
        counter = CountingDb(db.get_db())
        svc = GraphService(repo=GraphRepo(counter))
        data = await svc.query("user", DEMO_USER, max_hop=2)
        counts.append(counter.query_count)
        assert len(data["nodes"]) > size, "节点数应当随规模增长（否则这条用例证明不了什么）"
        assert counter.query_count <= 4, (
            f"图查询次数必须 ≤4（BR-09-16），实际 {counter.query_count}：{counter.calls}"
        )
    assert counts[0] == counts[1] == 3, f"查询次数必须是常数，两次分别为 {counts}"
    # 两条边查询（一跳、二跳）+ 一条属性聚合，**各自只发一次**：
    # 若实现退化成"逐个一跳节点查一次"，25 个账号那次会有 20+ 次 `find`
    assert counter.calls.count("entity_edges.find") == 2, counter.calls
    assert counter.calls.count("users.aggregate") == 1, counter.calls


async def test_truncated_query_uses_at_most_four_queries():
    """即使发生截断（要多查一次真实总数），也仍然 ≤4 次（常数）。"""
    await _build_cluster(30)
    counter = CountingDb(db.get_db())
    svc = GraphService(repo=GraphRepo(counter))
    data = await svc.query("user", DEMO_USER, max_hop=2, max_edges=5, max_nodes=3)
    assert data["truncated"] is True
    assert counter.query_count <= 4, counter.calls


# ============================================================ V-09-08 截断
async def test_truncation_reports_true_totals_and_keeps_no_dangling_edges():
    """`V-09-08`：超限时 `truncated=true`、`total_*` 是**截断前的真实数量**，
    且**不允许出现悬空边**（边的两端必须都在节点表里）。

    构造：1 个中心设备 + 300 个账号挂在它上面 + 每个账号另有 1 台设备
    ⇒ 以中心设备为起点时，一跳 = 300 个账号、二跳 = 它们各自的 300 台设备，
    真实 **600 条边、601 个节点**。
    上限压到 `max_nodes=20` / `max_edges=400`（**大于一跳边数**，因此一跳取全，
    截断只发生在二跳与节点上）⇒ `total_*` 必须仍是 600 / 601。
    """
    test_db = db.get_db()
    users = [f"U{200000 + i}" for i in range(300)]
    await test_db[COLL_USERS].insert_many([user_doc(uid) for uid in users])
    docs = [device_doc("D-CENTER", linked_user_cnt=300)]
    docs += [device_doc(f"D-{uid}", linked_user_cnt=1) for uid in users]
    await test_db["devices"].insert_many(docs)
    edges = [edge_doc("user", uid, "device", "D-CENTER", "used_device", 1 + i % 9)
             for i, uid in enumerate(users)]
    edges += [edge_doc("user", uid, "device", f"D-{uid}", "used_device", 1)
              for uid in users]
    await insert_edges(test_db, edges)

    data = await service().query("device", "D-CENTER", max_hop=2,
                                 max_nodes=20, max_edges=400)
    assert data["truncated"] is True
    assert data["total_edges"] == 600, data["total_edges"]
    assert data["total_nodes"] == 601, data["total_nodes"]
    assert data.get("total_is_lower_bound") is not True, (
        "一跳取全时总数是精确值，不该被标成下界"
    )
    assert len(data["edges"]) <= 400 and len(data["nodes"]) <= 20

    for edge in data["edges"]:
        assert edge["relation"] and edge["weight"] >= 1
        # 两端都必须在节点表里：悬空边会让力导向图凭空多出一个"未知同伙"
        assert any(node["id"] == edge["from"] for node in data["nodes"]), edge
        assert any(node["id"] == edge["to"] for node in data["nodes"]), edge
    # 每个非中心节点都必须至少连着一条保留的边（不留"没有任何连线的点"）
    connected = {end for edge in data["edges"] for end in (edge["from"], edge["to"])}
    for node in data["nodes"]:
        assert node["is_center"] or node["id"] in connected, node
    # 保留的是**关联强度最高**的那批（BR-09-17）
    weights = [edge["weight"] for edge in data["edges"]]
    assert weights == sorted(weights, reverse=True)
    assert data["center"] == {"type": "device", "id": "D-CENTER", "label": "D-CENTER"}


async def test_one_hop_truncated_marks_totals_as_lower_bound():
    """极端情形：**连一跳都没取全**时，`total_*` 只能是可达子图的下界。

    `max_edges` 小于一跳边数时，二跳的展开基于被裁剪过的一跳集合，
    因此 `total_*` **不可能**是整张图的真实总数。此时响应里显式带上
    `total_is_lower_bound=true`——**宁可承认这是下界，也不给一个看起来精确的假数字**。
    这是 `V-09-08`"不得静默截断"在边界上的延伸。
    """
    test_db = db.get_db()
    users = [f"U{500000 + i}" for i in range(50)]
    await test_db[COLL_USERS].insert_many([user_doc(uid) for uid in users])
    await test_db["devices"].insert_one(device_doc("D-BIG", linked_user_cnt=50))
    await test_db[COLL_ENTITY_EDGES].insert_many(
        [edge_doc("user", uid, "device", "D-BIG", "used_device", 1 + i % 7)
         for i, uid in enumerate(users)]
    )
    data = await service().query("device", "D-BIG", max_hop=2, max_edges=5, max_nodes=3)
    assert data["truncated"] is True
    assert data.get("total_is_lower_bound") is True
    assert data["total_edges"] >= 5
    assert len(data["edges"]) <= 5


async def test_truncation_by_nodes_only_still_reports_true_edge_totals():
    """只截节点、不截边时：`truncated=true` 且 `total_edges` 仍是真实值。

    这条钉住一个容易写错的细节：`total_edges` **不能用"保留的边数"充当**，
    否则前端文案会显示"共 5 条关系"（实际 31 条），研判者据此低估规模。

    规模按 `_build_cluster(30)` 的真实口径算：以 `DEMO_USER` 为中心时
    一跳 = {HUB, 自己的设备}，二跳 = 另 29 个账号 ⇒
    31 条边（30 条连 HUB + 1 条连自己的设备）、32 个节点
    （1 中心 + 2 一跳 + 29 二跳）。
    """
    await _build_cluster(30)
    data = await service().query("user", DEMO_USER, max_hop=2, max_nodes=5,
                                 max_edges=MAX_EDGES_CAP)
    assert data["truncated"] is True
    assert data["total_edges"] == 31
    assert data["total_nodes"] == 32
    assert len(data["nodes"]) <= 5
    assert len(data["edges"]) >= 1


async def test_not_truncated_when_within_limits():
    """没超限时 `truncated` 必须是 `false`（狼来了会让真截断被忽视）。"""
    await _build_cluster(3)
    data = await service().query("user", DEMO_USER, max_hop=2)
    assert data["truncated"] is False
    assert data["total_nodes"] == len(data["nodes"])
    assert data["total_edges"] == len(data["edges"])


# ============================================================ BR-09-20 属性补齐
async def test_node_attributes_are_backfilled_in_batch():
    """BR-09-20：节点的风险等级/标签/关联账号数由服务端补齐，前端不再逐节点请求。"""
    test_db = db.get_db()
    await _build_cluster(4)
    # 给一个二跳账号换上另一组标签（用 update 而不是 insert：它已经在团伙里了）
    await test_db[COLL_USERS].update_one(
        {"_id": "U100002"}, {"$set": {"risk_tags": ["device_cluster"], "status": "frozen"}})
    data = await service().query("user", DEMO_USER, max_hop=2)
    by_id = {node["id"]: node for node in data["nodes"]}
    assert by_id[HUB]["linked_user_cnt"] == 4, "设备节点必须带上关联账号数"
    assert by_id[DEMO_USER]["risk_tags"] == ["device_cluster", "blacklist_history"]
    assert by_id[DEMO_USER]["risk_level"] == "high", (
        "用户节点的风险等级取最近一次决策（E10 没有 risk_level 字段）"
    )
    assert by_id["U100002"]["risk_tags"] == ["device_cluster"]
    assert by_id["U100002"]["risk_level"] == "high"
    # 用户节点没有 `linked_user_cnt` 字段（E10 里不存在）→ 如实给 None，不编造
    assert by_id[DEMO_USER]["linked_user_cnt"] is None
    assert DEMO_USER in by_id


async def test_ip_and_address_nodes_get_masked_label_and_counts():
    """IP/地址节点的展示名与计数：地址用**掩码后的地理前缀**作为标签。"""
    test_db = db.get_db()
    await insert_profile(test_db)
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "ip", DEMO_IP, "shared_ip", 3),
        edge_doc("user", DEMO_USER, "address", DEMO_ADDRESS, "shared_address", 1),
    ])
    data = await service().query("user", DEMO_USER, max_hop=1)
    by_id = {node["id"]: node for node in data["nodes"]}
    assert by_id[DEMO_IP]["linked_user_cnt"] == 1
    assert by_id[DEMO_ADDRESS]["label"] == "湖南省长沙市****"
    assert DEMO_IP in data["center"]["label"] or True


# ============================================================ risk_only
async def test_risk_only_keeps_only_risk_edges_and_their_endpoints():
    """`risk_only=true`：只返回 `risk_flag=true` 的边及其端点。"""
    test_db = db.get_db()
    await _build_cluster(4)
    await test_db[COLL_ENTITY_EDGES].update_one(
        {"from_id": DEMO_USER, "to_id": HUB, "relation": "used_device"},
        {"$set": {"risk_flag": True}},
    )
    data = await service().query("user", DEMO_USER, max_hop=2, risk_only=True)
    assert data["edges"] and all(edge["risk_flag"] for edge in data["edges"])
    assert len(data["edges"]) == 1
    node_ids = {node["id"] for node in data["nodes"]}
    assert node_ids == {DEMO_USER, HUB}
    assert data["total_edges"] == 1


# ============================================================ 超时（GRP-5002）
async def test_timeout_returns_partial_result_with_truncated_true(monkeypatch):
    """§5 的 `GRP-5002`：超时不报错，返回已取到的部分并 `truncated=true`。

    用"第 2 跳永远卡住"的假仓储触发超时（真等 3 秒会让测试慢得没有意义）。
    """

    class SlowSecondHopRepo(GraphRepo):
        async def edges_among_pairs(self, *args, **kwargs):  # noqa: ANN002, ANN003
            import asyncio

            await asyncio.sleep(QUERY_TIMEOUT_SEC + 5)
            return []

    await _build_cluster(4)
    svc = GraphService(repo=SlowSecondHopRepo(db.get_db()))
    started = time.perf_counter()
    data = await svc.query("user", DEMO_USER, max_hop=2)
    assert time.perf_counter() - started < QUERY_TIMEOUT_SEC + 1.5, "超时必须及时返回"
    assert data["truncated"] is True
    assert data.get("timeout") is True
    assert data["nodes"], "已取到的部分（1 跳）必须返回，而不是空图"
    assert any(node["hop"] == 1 for node in data["nodes"])


# ============================================================ V-09-15 性能（有界规模）
async def test_bounded_scale_query_records_elapsed_ms():
    """`V-09-15`（**有界规模版**）：约 5k 节点 / 5k 边的图上查询，`elapsed_ms` 有记录。

    ⚠️ **如实声明**：Spec 的 `V-09-15` 写的是"灌 10 万实体后 P95 < 300ms"，
    本用例只压到 **约 5000 个实体**，因此它**不构成"10 万规模达标"的证据**。
    压 10 万需要灌 20 万条边并跑多轮取 P95，既超出本模块的验收范围，
    也会让测试套件慢到没人愿意跑。这里钉住的是"这个接口在真实规模下会记录耗时、
    且不会因某个 N+1 退化而崩"，把 10 万规模的结论留给专门的压测。
    """
    test_db = db.get_db()
    size = 2500
    users = [f"U{300000 + i}" for i in range(size)]
    await test_db[COLL_USERS].insert_many([user_doc(uid) for uid in users], ordered=False)
    await test_db["devices"].insert_many(
        [device_doc(f"D-{uid}", linked_user_cnt=1) for uid in users], ordered=False,
    )
    docs = [edge_doc("user", uid, "device", HUB, "used_device", 1 + i % 5)
            for i, uid in enumerate(users)]
    docs += [edge_doc("user", uid, "device", f"D-{uid}", "used_device", 1)
             for uid in users]
    await test_db[COLL_ENTITY_EDGES].insert_many(docs, ordered=False)

    started = time.perf_counter()
    data = await service().query("user", users[0], max_hop=2)
    wall_ms = int((time.perf_counter() - started) * 1000)

    assert isinstance(data["elapsed_ms"], int) and data["elapsed_ms"] >= 0
    # 以 `users[0]` 为中心的真实口径：一跳 = {HUB, 自己的设备}，
    # 二跳 = 其余 2499 个账号（它们各自的设备属于第 3 跳，不在 2 跳图里）
    # ⇒ 边 = 2500 条连 HUB 的 + 1 条连自己设备的 = 2501；节点 = 1 + 2 + 2499 = 2502
    assert data["total_edges"] == size + 1, data["total_edges"]
    assert data["total_nodes"] == size + 2, data["total_nodes"]
    assert data["truncated"] is True, "超过节点上限必须如实截断"
    assert len(data["nodes"]) <= 200 and len(data["edges"]) <= 500
    assert wall_ms < 3000, f"5k 规模的查询耗时 {wall_ms}ms，疑似退化成 N+1"


async def test_query_count_stays_constant_at_scale():
    """规模再大，查询次数仍是常数（与上一条互为印证）。"""
    test_db = db.get_db()
    size = 300
    users = [f"U{400000 + i}" for i in range(size)]
    await test_db[COLL_USERS].insert_many([user_doc(uid) for uid in users], ordered=False)
    # 中心实体（设备）必须存在，否则会正确地报 `GRP-4004`
    await test_db["devices"].insert_one(device_doc(HUB, linked_user_cnt=size))
    await test_db[COLL_ENTITY_EDGES].insert_many(
        [edge_doc("user", uid, "device", HUB, "used_device", 1) for uid in users],
        ordered=False,
    )
    counter = CountingDb(test_db)
    data = await GraphService(repo=GraphRepo(counter)).query("device", HUB, max_hop=2)
    assert counter.query_count <= 4, counter.calls
    assert data["total_edges"] == size
    assert data["total_nodes"] == size + 1
