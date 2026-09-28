# -*- coding: utf-8 -*-
"""画像与图谱的 HTTP 契约（模块 09 §3.1 / §3.2 / §5）。

对应验收：`V-09-01`、`V-09-03`、`V-09-04`（后端数据面）、`V-09-06`、`V-09-08`、
`V-09-09`、`V-09-14`，外加权限与统一响应包这些横切契约。

前端断言类（配色、标红、空态文案）由前端负责，这里只保证**它们依赖的数据面成立**：
例如 `V-09-04` 的"关联 12 个账号"必须有 `linked_user_cnt=12` 这个后端字段支撑。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import COLL_DEVICES, COLL_IP_POOL, COLL_USERS
from app.services import profile_service
from tests.conftest import READER, WRITER
from tests.profile_testlib import (
    DEMO_ADDRESS,
    DEMO_DEVICE,
    DEMO_IP,
    DEMO_USER,
    ExplodingRepo,
    address_doc,
    device_doc,
    edge_doc,
    insert_edges,
    insert_profile,
    user_doc,
)

pytestmark = pytest.mark.anyio

PROFILE_URL = f"/api/v1/profiles/{DEMO_USER}"
GRAPH_URL = "/api/v1/graph/user/" + DEMO_USER
ENVELOPE_KEYS = {"ok", "code", "message", "trace_id", "data"}


async def _seed_profile_graph() -> None:
    """一份"内容饱满"的画像 + 一条风险边（前端演示的基线数据）。"""
    test_db = db.get_db()
    await insert_profile(test_db)
    await test_db[COLL_DEVICES].update_one(
        {"_id": DEMO_DEVICE}, {"$set": {"linked_user_cnt": 12}})
    await test_db[COLL_IP_POOL].update_one(
        {"_id": DEMO_IP}, {"$set": {"linked_user_cnt": 12}})
    await test_db["user_addresses"].update_one(
        {"_id": DEMO_ADDRESS}, {"$set": {"linked_user_cnt": 8}})
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "device", DEMO_DEVICE, "used_device", 3,
                 risk_flag=True),
        edge_doc("user", DEMO_USER, "ip", DEMO_IP, "shared_ip", 2),
        edge_doc("user", DEMO_USER, "address", DEMO_ADDRESS, "shared_address", 1),
    ])


# ============================================================ §3.1 画像
async def test_profile_endpoint_returns_full_contract(client):
    """`V-09-01`：`/profiles/{id}` 返回 `user/stat/device/ip/address` + 标签数组。"""
    await _seed_profile_graph()
    r = await client.get(PROFILE_URL, headers=READER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == ENVELOPE_KEYS and body["ok"] is True and body["code"] == "OK"
    assert r.headers["X-Trace-Id"] == body["trace_id"]
    data = body["data"]
    assert set(data) == {"user", "stat", "latest_decision", "device", "ip",
                         "address", "tag_meta"}
    assert data["user"]["user_id"] == DEMO_USER
    assert data["user"]["phone_masked"] == "139****0001"
    assert data["user"]["age_days"] == 42
    assert data["user"]["risk_tags"] == ["device_cluster", "blacklist_history"]
    assert data["stat"] == {"order_cnt": 3, "aftersale_cnt": 1, "block_cnt": 1,
                            "total_amount": 20000}
    assert data["latest_decision"]["risk_level"] == "high"
    assert data["device"]["device_id"] == DEMO_DEVICE
    assert data["ip"]["ip"] == DEMO_IP and data["ip"]["is_proxy"] is False
    assert data["address"]["address_id"] == DEMO_ADDRESS
    assert len(data["tag_meta"]) == 8
    assert data["tag_meta"]["device_cluster"]["color"] == "red"


async def test_profile_supports_frontend_red_highlight_rule(client):
    """`V-09-04` 的后端数据面：聚集度 ≥5 时字段值必须能支撑"标红 + 关联 N 个账号"。"""
    await _seed_profile_graph()
    data = (await client.get(PROFILE_URL, headers=READER)).json()["data"]
    assert data["device"]["linked_user_cnt"] == 12 >= 5
    assert data["ip"]["linked_user_cnt"] == 12 >= 5
    assert data["address"]["linked_user_cnt"] == 8 >= 5
    assert "12" in str(data["device"]["linked_user_cnt"])


async def test_profile_masks_phone_and_hides_address_plaintext(client):
    """`V-09-03`：手机号形如 `xxx****xxxx`，响应**不含明文地址**。"""
    test_db = db.get_db()
    await _seed_profile_graph()
    await test_db[COLL_USERS].update_one(
        {"_id": DEMO_USER}, {"$set": {"phone": "13812346621"}})
    await test_db["user_addresses"].update_one(
        {"_id": DEMO_ADDRESS},
        {"$set": {"detail": "湖南省长沙市岳麓区文一西路 969 号 3 栋 1802"}})

    text = (await client.get(PROFILE_URL, headers=READER)).text
    assert "138****6621" in text
    assert "13812346621" not in text
    for leak in ("文一西路", "969", "1802", "3 栋", "detail_hash"):
        assert leak not in text, f"响应泄露了地址明文/内部字段：{leak}"
    assert "湖南省长沙市****" in text


async def test_profile_unknown_user_returns_404(client):
    """§5：用户不存在 → `GRP-4004`（404），中栏显示「未找到该用户画像」。"""
    r = await client.get("/api/v1/profiles/U-NOT-EXIST", headers=READER)
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False and body["code"] == "GRP-4004"
    assert body["message"] and body["trace_id"]


async def test_profile_service_failure_returns_503(client, monkeypatch):
    """`V-09-14`：画像聚合查询失败 → `GRP-5001`（503）。

    503 与 404 必须严格分开：把数据库抖动报成"未找到该用户"会让审核员
    得出"查无此人"的结论。右栏判定摘要不受影响是**前端的分区渲染**，
    后端在这里只负责如实报错。
    """
    service = profile_service.get_profile_service()
    service.configure(repo=ExplodingRepo())      # type: ignore[arg-type]
    try:
        r = await client.get(PROFILE_URL, headers=READER)
    finally:
        service.reset_dependencies()
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["code"] == "GRP-5001"
    assert "画像服务暂时不可用" in body["message"]


async def test_profile_requires_reviewer_permission(client):
    """`case:read` 的矩阵里只有 REVIEWER：策略师必须拿到 403，未登录 401。"""
    await _seed_profile_graph()
    forbidden = await client.get(PROFILE_URL, headers=WRITER)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "AUTH-4020"
    anonymous = await client.get(PROFILE_URL)
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "AUTH-4002"


# ============================================================ §3.2 关联网络
async def test_graph_endpoint_returns_full_contract(client):
    """`V-09-05` 的后端面：`center/nodes/edges/truncated/total_*/elapsed_ms` 齐全，
    且边的键名是 `from`（不是 `from_`）。"""
    await _seed_profile_graph()
    r = await client.get(GRAPH_URL, params={"max_hop": 2}, headers=READER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data) >= {"center", "nodes", "edges", "truncated", "total_nodes",
                         "total_edges", "elapsed_ms"}
    assert data["center"] == {"type": "user", "id": DEMO_USER, "label": DEMO_USER}
    assert isinstance(data["elapsed_ms"], int) and data["elapsed_ms"] >= 0
    assert data["truncated"] is False
    assert data["total_edges"] == 3 and data["total_nodes"] == 4
    for edge in data["edges"]:
        assert set(edge) >= {"from", "to", "relation", "weight", "risk_flag",
                             "last_seen_at"}
        assert "from_" not in edge, "对外契约的键名必须是 `from`（§3.2）"
    risk = [edge for edge in data["edges"] if edge["risk_flag"]]
    assert len(risk) == 1, "红色边（risk_flag=true）必须如实下发"


async def test_graph_default_max_hop_is_two(client):
    """`max_hop` 的默认值是 2（§3.2 的请求参数表）。"""
    await _seed_profile_graph()
    data = (await client.get(GRAPH_URL, headers=READER)).json()["data"]
    assert {node["hop"] for node in data["nodes"]} <= {0, 1, 2}
    assert any(node["is_center"] for node in data["nodes"])


async def test_graph_max_hop_over_limit_returns_400(client):
    """`V-09-06`：`max_hop=3` → `GRP-4001` + HTTP 400。"""
    r = await client.get(GRAPH_URL, params={"max_hop": 3}, headers=READER)
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "GRP-4001"
    assert "2 跳" in body["message"]


async def test_graph_phone_entity_type_returns_422(client):
    """`V-09-06`：`entity_type=phone` → `GRP-4002` + HTTP 422（明确拒绝）。"""
    r = await client.get("/api/v1/graph/phone/139****0001", headers=READER)
    assert r.status_code == 422
    assert r.json()["code"] == "GRP-4002"


async def test_graph_out_of_range_caps_return_422(client):
    """节点/边上限越界 → 参数错误（拒绝而不是静默夹取）。"""
    for params in ({"max_nodes": 9999}, {"max_edges": 0}, {"max_nodes": 0}):
        r = await client.get(GRAPH_URL, params=params, headers=READER)
        assert r.status_code == 422, params
        assert r.json()["code"] == "COM-4001"


async def test_graph_missing_entity_returns_404_and_isolated_returns_200(client):
    """`V-09-09`：孤立账号 **200 + 只有中心节点**；查不到才 404。

    ⚠️ 孤立账号的 `nodes` **不是空数组**——图接口总会返回中心节点
    （1 个节点 + 0 条边）。前端的空态判据因此必须是"无任何边"，
    写成"节点数为 0"会导致空态永不触发（前端已修，这里钉住后端形状）。
    """
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc("U-LONELY"))
    isolated = await client.get("/api/v1/graph/user/U-LONELY", headers=READER)
    assert isolated.status_code == 200
    data = isolated.json()["data"]
    assert data["edges"] == []
    assert data["total_edges"] == 0
    assert len(data["nodes"]) == 1 and data["nodes"][0]["is_center"] is True

    missing = await client.get("/api/v1/graph/user/U-NOT-EXIST", headers=READER)
    assert missing.status_code == 404
    assert missing.json()["code"] == "GRP-4004"


async def test_graph_truncation_returns_true_totals(client):
    """`V-09-08`：`?max_nodes=1` → `truncated=true` 且给出截断前的真实总数。"""
    test_db = db.get_db()
    await insert_profile(test_db)
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "device", DEMO_DEVICE, "used_device", 3),
        edge_doc("user", DEMO_USER, "ip", DEMO_IP, "shared_ip", 2),
        edge_doc("user", DEMO_USER, "address", DEMO_ADDRESS, "shared_address", 1),
    ])
    data = (await client.get(GRAPH_URL, params={"max_nodes": 1},
                             headers=READER)).json()["data"]
    assert data["truncated"] is True
    assert data["total_nodes"] == 4
    assert data["total_edges"] == 3
    assert len(data["nodes"]) <= 1
    assert data["nodes"][0]["is_center"] is True, "中心节点永远保留"


async def test_graph_risk_only_filters_edges(client):
    """`risk_only=true` 只返回红边及其端点。"""
    await _seed_profile_graph()
    data = (await client.get(GRAPH_URL, params={"risk_only": "true"},
                             headers=READER)).json()["data"]
    assert len(data["edges"]) == 1 and data["edges"][0]["risk_flag"] is True
    assert {node["id"] for node in data["nodes"]} == {DEMO_USER, DEMO_DEVICE}


async def test_graph_requires_reviewer_permission(client):
    """图谱与画像同属中栏区块，权限同为 `case:read`。"""
    await _seed_profile_graph()
    assert (await client.get(GRAPH_URL, headers=WRITER)).status_code == 403
    assert (await client.get(GRAPH_URL)).status_code == 401


async def test_graph_and_profile_reject_unknown_subpaths(client):
    """非法实体类型的路径照样走本模块的错误码（不是裸 404）。"""
    r = await client.get("/api/v1/graph/imei/123456", headers=READER)
    assert r.status_code == 422 and r.json()["code"] == "GRP-4002"


async def test_graph_entity_not_found_for_ip_and_address(client):
    """设备/IP/地址三种中心实体的 404 文案一致（§5 的「未找到该用户/实体」）。"""
    for url in ("/api/v1/graph/device/D-NONE", "/api/v1/graph/ip/1.1.1.1",
                "/api/v1/graph/address/A-NONE"):
        r = await client.get(url, headers=READER)
        assert r.status_code == 404, url
        assert r.json()["code"] == "GRP-4004"


async def test_profile_and_graph_are_read_only_over_http(client):
    """BR-09-19：两个查询接口都是只读的（集合计数不变）。"""
    await _seed_profile_graph()
    test_db = db.get_db()
    before = [await test_db[name].count_documents({}) for name in
              (COLL_USERS, COLL_DEVICES, COLL_IP_POOL, "user_addresses",
               "entity_edges")]
    await client.get(PROFILE_URL, headers=READER)
    await client.get(GRAPH_URL, headers=READER)
    after = [await test_db[name].count_documents({}) for name in
             (COLL_USERS, COLL_DEVICES, COLL_IP_POOL, "user_addresses",
              "entity_edges")]
    assert before == after


async def test_api_doc_mentions_both_endpoints(client):
    """两个端点必须出现在 OpenAPI 里（`scripts/self_audit.py` 也会核对）。"""
    from app.main import app

    paths = app.openapi()["paths"]
    assert set(paths["/api/v1/profiles/{user_id}"]) == {"get"}
    assert set(paths["/api/v1/graph/{entity_type}/{entity_id}"]) == {"get"}
    # 两个端点都标注了权限依赖（依赖名会出现在 OpenAPI 里，便于逐个核对）
    schema = paths["/api/v1/profiles/{user_id}"]["get"]
    assert schema["summary"] == "用户全貌画像"


async def test_seed_shaped_isolated_user_is_served_with_empty_graph(client):
    """种子里的孤立账号形状（`U000131`：有画像、无任何边）必须能被正确服务。

    这条用例连的是**测试库**，因此它验证的是"这种数据形状的响应正确"：
    画像 200 且四类明细为 `null`、图 200 且只有中心节点。种子是否真的灌了它，
    由 `scripts/seed.py` 的幂等运行结果保证（前端已按 `U000131` 核对空态）。
    """
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(
        user_doc("U000131", risk_tags=[], phone="188****6621",
                 stat={"order_cnt": 0, "aftersale_cnt": 0, "block_cnt": 0,
                       "total_amount": 0},
                 latest_decision=None))
    profile = (await client.get("/api/v1/profiles/U000131", headers=READER)).json()["data"]
    assert profile["user"]["user_id"] == "U000131"
    assert profile["user"]["risk_tags"] == []
    assert profile["device"] is None and profile["ip"] is None
    assert profile["address"] is None
    assert profile["latest_decision"] is None

    graph = (await client.get("/api/v1/graph/user/U000131", headers=READER)).json()["data"]
    assert graph["edges"] == [] and graph["total_edges"] == 0
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["is_center"] is True
    assert graph["truncated"] is False


async def test_address_doc_helper_matches_contract():
    """`address_doc` 夹具本身的形状必须与 E13 一致（否则断言失去意义）。"""
    doc = address_doc()
    assert doc["_id"] == DEMO_ADDRESS and doc["user_id"] == DEMO_USER
    assert "detail_hash" in doc and "detail" not in doc
    assert device_doc()["_id"] == DEMO_DEVICE
