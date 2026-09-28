# -*- coding: utf-8 -*-
"""模块 09 与 03/04 的跨模块集成：单一真源（`V-09-12`）、异步建图（`V-09-10`）、
D46 的 IP 可解析性。

这些用例**故意跨模块**：单模块内的用例证明"09 自己算得对"，而这里的用例证明
"04 拿到的数字确实是 09 给的那一个"——BR-09-13/14 的全部意义就在这里
（同一指标只能有一个真源，否则特征快照与图谱页会给出两个不同的账号数）。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import COLL_DEVICES, COLL_ENTITY_EDGES, COLL_IP_POOL, COLL_USERS
from app.core import edge_writer
from app.services import feature_service
from tests.conftest import WRITER
from tests.event_testlib import coupon_payload, unique_event_id
from tests.feature_testlib import make_event
from tests.profile_testlib import device_doc, edge_doc, insert_edges, ip_doc, user_doc

pytestmark = pytest.mark.anyio

EVENTS_URL = "/api/v1/events"
DEVICE = "D-DEMO09"
IP = "203.0.113.210"


# ============================================================ V-09-12 单一真源
async def test_feature_engine_reads_linked_counts_from_module_09():
    """`V-09-12`：04 的 `device_user_cnt` 必须等于 09 的 `linked_user_cnt`（以及边里的去重账号数）。

    这条真接上下：04 走 `get_components().linked_user_count_provider`
    （默认已是 09 的真实实现），09 侧的数字来自它维护的冗余计数，
    而冗余计数必须等于 `entity_edges` 里该设备的**去重账号数**。
    三者一致才算"单一真源"成立——任何一处另算一份都会在这条用例上暴露。
    """
    test_db = db.get_db()
    users = ["U-INT-1", "U-INT-2", "U-INT-3"]
    await test_db[COLL_USERS].insert_many([user_doc(uid) for uid in users])
    await test_db[COLL_DEVICES].insert_one(device_doc(DEVICE, linked_user_cnt=len(users)))
    await test_db[COLL_IP_POOL].insert_one(ip_doc(IP, linked_user_cnt=1))
    await insert_edges(test_db, [
        edge_doc("user", uid, "device", DEVICE, "used_device", 1) for uid in users
    ] + [edge_doc("user", "U-INT-1", "ip", IP, "shared_ip", 1)])

    service = feature_service.get_feature_service()
    snapshot = await service.compute(make_event(
        unique_event_id(), 1_800_000_000_000, "order_create",
        user_id="U-INT-1", device_id=DEVICE, ip=IP,
    ))
    await feature_service.flush()

    features = snapshot["features"]
    assert features["device_user_cnt"] == 3, features.get("device_user_cnt")
    assert "device_user_cnt" not in snapshot["missing_features"], (
        "09 已落地：聚集度不该再进 missing_features（那会让 04 报'数据不足'）"
    )
    assert features["ip_user_cnt"] == 1
    assert features["ip_is_proxy"] is False, "E12 有该 IP 的显式布尔 → 必须给出确定的 false"

    # 与 09 的冗余计数、以及边里的去重账号数**三方一致**
    device = await test_db[COLL_DEVICES].find_one({"_id": DEVICE})
    rows = await test_db[COLL_ENTITY_EDGES].find({"to_id": DEVICE}).to_list(length=50)
    assert device["linked_user_cnt"] == features["device_user_cnt"]
    assert len({row["from_id"] for row in rows}) == features["device_user_cnt"]


async def test_linked_counts_are_missing_when_provider_is_unavailable():
    """反向对照：把 09 的 provider 换回占位实现时，三项必须如实进 `missing_features`。

    没有这条对照，"04 拿到了 3"这个断言证明不了它**来自** 09
    （例如 04 自己按事件数猜了一个 3 也能过）。占位实现恒返回 `None`，
    此时 04 必须写进缺失，而**不能**回落到 0（BR-04-09）。
    """
    from app.protocols import UnavailableLinkedUserCountProvider

    service = feature_service.get_feature_service()
    # 直接改私有格子而不是 `configure()`：`configure` 的语义是"非 None 才替换"，
    # 因此它无法把依赖**恢复成 None**（那会让本用例污染后续用例的默认装配）
    old = service._linked_provider                            # noqa: SLF001
    service._linked_provider = UnavailableLinkedUserCountProvider()   # noqa: SLF001
    try:
        snapshot = await service.compute(make_event(
            unique_event_id(), 1_800_000_000_000, "login", user_id="U-INT-9",
            device_id=DEVICE, ip=IP,
        ))
        await feature_service.flush()
    finally:
        service._linked_provider = old                        # noqa: SLF001
    for name in ("device_user_cnt", "ip_user_cnt", "address_user_cnt"):
        assert name in snapshot["missing_features"], name
        assert name not in snapshot["features"], (
            f"{name} 必须缺失而不是 0（0 是断言，None 才是'不知道'）"
        )


# ============================================================ V-09-10 异步建图
async def test_event_returns_decision_before_graph_is_written(client):
    """`V-09-10`：决策同步返回，建边在返回之后异步发生（BR-09-07）。

    断言顺序刻意如此：响应到手时 `risk_events` 还没落库（AD-01 的异步落库语义）、
    `entity_edges` 也还没建；`flush` 之后两者都在。这才是"建边不阻塞决策"的
    可观测证据，而不是"接口很快"这种会被机器负载左右的断言。
    """
    payload = coupon_payload(event_id=unique_event_id(), ip=IP, device_id=DEVICE)
    r = await client.post(EVENTS_URL, json=payload, headers=WRITER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["decision"] in ("pass", "review", "reject")
    assert isinstance(data["elapsed_ms"], int) and data["elapsed_ms"] < 200, (
        f"同步链路必须在 200ms 预算内返回（实际 {data['elapsed_ms']}ms）"
    )
    assert data["degrade"] is not None and data["degrade"]["stage"] != "timeout"
    # 同步返回时建边尚未发生（下面的 flush 之后才出现）
    assert await db.get_db()[COLL_ENTITY_EDGES].count_documents({}) == 0
    assert await edge_writer.flush() is True
    assert await db.get_db()[COLL_ENTITY_EDGES].count_documents({}) >= 1
    edge = await db.get_db()[COLL_ENTITY_EDGES].find_one(
        {"from_id": payload["user_id"], "to_id": DEVICE})
    assert edge is not None and edge["relation"] == "used_device"


# ============================================================ D46 的可解析性
async def test_first_event_on_new_ip_degrades_then_resolves(client):
    """决策 D46 的直接证据：**首个**事件降级 `stage=feature`，同 IP 的**第二次**已能取到 `ip_is_proxy`。

    第 1 条：E12 还没有这个 IP ⇒ `ip_is_proxy` 缺失 ⇒ 04 如实报
    `degrade_suggested=true` ⇒ 03 在调 05 之前短路（`stage=feature`）。
    第 1 条决策返回**之后**，模块 09 在后台把 E12 行落了（显式布尔），
    于是第 2 条事件的快照完整、链路走到 05 并给出**真实决策**（05 落地前这里
    停在 `stage=rule`，那句已不再是事实）。

    **第一个事件降级是正确行为**（那一刻确实不知道）；要点是它不会**永远**降级——
    若 09 只给"已经出现过的 IP"建画像，系统会永久停在 feature 阶段，
    05/07/08/10 全部无法演示（这正是 D46 要解决的问题）。
    """
    assert await db.get_db()[COLL_IP_POOL].count_documents({"_id": IP}) == 0
    first = await client.post(EVENTS_URL, json=coupon_payload(
        event_id=unique_event_id(), ip=IP, device_id=DEVICE), headers=WRITER)
    first_data = first.json()["data"]
    assert first_data["degrade"]["stage"] == "feature", first_data["degrade"]
    assert "degrade_suggested" in first_data["degrade"]["reason"]
    assert "特征服务不可用" not in first_data["degrade"]["reason"]

    # 等异步建图收尾（真实链路上它是"决策返回之后"发生的；这里显式等，避免竞态）
    await edge_writer.flush()
    doc = await db.get_db()[COLL_IP_POOL].find_one({"_id": IP})
    assert doc is not None and doc["is_proxy"] is False and doc["is_idc"] is False

    second = await client.post(EVENTS_URL, json=coupon_payload(
        event_id=unique_event_id(), ip=IP, device_id=DEVICE), headers=WRITER)
    second_data = second.json()["data"]
    assert second_data["degrade"] is None, (
        "同 IP 的第二次事件必须已经能解析出 ip_is_proxy（D46），链路不再降级；"
        f"实际 {second_data['degrade']}"
    )
    assert second_data["engine_version"] == "rule-engine-v1", (
        "第二次事件的决策必须由真实的规则引擎给出（占位实现只会抛异常 → stage=rule）"
    )
    assert second_data["decision"] in {"pass", "review", "reject"}
    assert second_data["model_score"] is None


async def test_seeded_ip_resolves_on_the_very_first_event(client):
    """反向证据：种子**预置过**的 IP 上，第一次事件就不再降级。

    这正是 `scripts/seed.py` 要为演示 IP 预置 E12 行的意义：演示一开始
    链路就是完整的，不必先"喂"一条事件（对 E2E 与前端演示都重要）。
    """
    await db.get_db()[COLL_IP_POOL].insert_one(ip_doc(IP, linked_user_cnt=0))
    r = await client.post(EVENTS_URL, json=coupon_payload(
        event_id=unique_event_id(), ip=IP, device_id=DEVICE), headers=WRITER)
    data = r.json()["data"]
    assert data["degrade"] is None, data["degrade"]
    assert data["engine_version"] == "rule-engine-v1", (
        "预置 IP 画像后第一次事件就应拿到真实决策"
    )
