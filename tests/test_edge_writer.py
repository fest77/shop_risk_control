# -*- coding: utf-8 -*-
"""异步建边器：D46 的 IP 自动落画像、仅新边计数、标签/统计、重试队列。

对应验收：`V-09-10`（建边异步、不阻塞决策）、`V-09-11`（重复上报只累加 weight）、
`V-09-12`（聚集度单一真源）、`V-09-13`（标签去重与不自动复活）、
`V-09-16`（D46：事件 IP 必须能解析出确定的 `ip_is_proxy`）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import db
from app.constants import COLL_DEVICES, COLL_ENTITY_EDGES, COLL_IP_POOL, COLL_USERS
from app.core import edge_writer as edge_writer_mod
from app.core.degraded import DEGRADED
from app.core.edge_writer import SAME_PHONE_MAX, EdgeWriter
from app.repos.profile_repo import ProfileRepo
from tests.profile_testlib import (
    DEMO_ADDRESS,
    DEMO_DEVICE,
    DEMO_IP,
    DEMO_USER,
    ExplodingRepo,
    FailingGraphRepo,
    device_doc,
    ip_doc,
    user_doc,
)

pytestmark = pytest.mark.anyio


def _event(**over: object) -> dict:
    """一条"已通过 03 校验"的事件（形状与 `validate_event` 的产物一致）。"""
    event = {
        "event_type": "order_create",
        "user_id": DEMO_USER,
        "device_id": DEMO_DEVICE,
        "ip": DEMO_IP,
        "address_id": DEMO_ADDRESS,
        "phone": None,
        "amount": 9900,
        "ts": 1_800_000_000_000,
        "scene_extra": {},
    }
    event.update(over)
    return event


def _decision(decision: str = "review", score: float = 61.0) -> dict:
    return {"decision": decision, "risk_level": "high", "final_score": score,
            "rule_score": score, "hits": []}


# ============================================================ D46 IP 自动落画像
async def test_event_ip_creates_ip_row_with_explicit_booleans():
    """`V-09-16` / 决策 D46：事件带的 IP 必须自动落 E12，且**布尔字段显式在场**。

    04 的 `_pick_proxy` 只有看到 bool 才会给出 `False`；字段缺失会返回 `None`，
    于是 `ip_is_proxy` 进 `missing_features`，03 在调 05 之前短路降级——
    系统会**永远**停在 `feature` 阶段，05/07/08/10 全部无法演示。
    """
    writer = EdgeWriter()
    await writer.write_for_event(_event(), _decision())

    doc = await db.get_db()[COLL_IP_POOL].find_one({"_id": DEMO_IP})
    assert doc is not None, "D46：事件带 IP 时必须有 E12 画像行"
    assert doc["is_proxy"] is False and doc["is_idc"] is False, (
        "必须是**显式布尔值**（缺失会让 04 判'缺失'并短路降级）"
    )
    assert doc["first_seen_at"] == doc["first_seen_at"] and doc["first_seen_at"]
    assert doc["linked_user_cnt"] == 1


async def test_repeated_event_does_not_overwrite_first_seen_or_real_proxy_flag():
    """重复事件不得改写 `first_seen_at`，也**绝不能**把真实的 `is_proxy=true` 抹掉。

    后者是最危险的一条：代理 IP 是羊毛党的标准配置，把它覆盖成 `false`
    等于亲手删掉本次计算里最关键的信号。
    """
    writer = EdgeWriter()
    await writer.write_for_event(_event(), _decision())
    first = await db.get_db()[COLL_IP_POOL].find_one({"_id": DEMO_IP})
    # 模拟"外部 IP 库后来把这个 IP 判定为代理"（本项目无 IP 库，因此由测试手工标注）
    await db.get_db()[COLL_IP_POOL].update_one(
        {"_id": DEMO_IP}, {"$set": {"is_proxy": True, "is_idc": True}})
    await asyncio.sleep(0.01)
    await writer.write_for_event(_event(event_type="login"), _decision())

    doc = await db.get_db()[COLL_IP_POOL].find_one({"_id": DEMO_IP})
    assert doc["first_seen_at"] == first["first_seen_at"], "first_seen_at 只在首次写入"
    assert doc["is_proxy"] is True and doc["is_idc"] is True, (
        "已存在的真实代理标记绝不能被'补默认值'覆盖掉"
    )


async def test_ip_row_without_boolean_fields_is_repaired():
    """已存在但**缺**布尔字段的 E12 行（历史数据）必须被补上，否则 D46 有空洞。"""
    await db.get_db()[COLL_IP_POOL].insert_one({"_id": "8.8.8.8", "linked_user_cnt": 0})
    writer = EdgeWriter()
    await writer.write_for_event(_event(ip="8.8.8.8", device_id=None,
                                       address_id=None), _decision())
    doc = await db.get_db()[COLL_IP_POOL].find_one({"_id": "8.8.8.8"})
    assert doc["is_proxy"] is False and doc["is_idc"] is False


# ============================================================ V-09-11 / V-09-12 计数
async def test_linked_user_cnt_increments_only_on_first_edge():
    """`V-09-11` + BR-09-12：同一 user-device 上报 3 次 → 边 1 条、计数 1。

    这条是"重复上报把计数刷爆"的直接防线：`linked_user_cnt` 一旦被刷成 100，
    页面上就是"该设备关联 100 个账号"——一个纯属虚构的团伙规模，
    而并案判断正是看这个数字。
    """
    await db.get_db()[COLL_DEVICES].insert_one(device_doc(DEMO_DEVICE, linked_user_cnt=0))
    writer = EdgeWriter()
    for _ in range(3):
        await writer.write_for_event(_event(address_id=None, ip=None), _decision())

    edges = await db.get_db()[COLL_ENTITY_EDGES].count_documents({})
    assert edges == 1, "重复上报不得新增边"
    device = await db.get_db()[COLL_DEVICES].find_one({"_id": DEMO_DEVICE})
    assert device["linked_user_cnt"] == 1, "只有首次插入才递增冗余计数"
    doc = await db.get_db()[COLL_ENTITY_EDGES].find_one({})
    assert doc["weight"] == 3, "重复上报累加 weight"


async def test_linked_user_cnt_matches_distinct_accounts_in_edges():
    """`V-09-12` 的数据面：冗余计数 == 边集合里的**去重账号数**（单一真源）。"""
    test_db = db.get_db()
    await test_db[COLL_DEVICES].insert_one(device_doc(DEMO_DEVICE, linked_user_cnt=0))
    writer = EdgeWriter()
    for index in range(5):
        await writer.write_for_event(
            _event(user_id=f"U{900000 + index}", ip=None, address_id=None), _decision())
    for index in range(3):        # 其中 3 个账号重复上报两次
        await writer.write_for_event(
            _event(user_id=f"U{900000 + index}", ip=None, address_id=None), _decision())

    doc = await test_db[COLL_DEVICES].find_one({"_id": DEMO_DEVICE})
    rows = await test_db[COLL_ENTITY_EDGES].find({"to_id": DEMO_DEVICE}).to_list(length=50)
    assert doc["linked_user_cnt"] == len({row["from_id"] for row in rows}) == 5


# ============================================================ BR-09-09 建边
async def test_all_three_entities_get_edges():
    """每次事件的关联：user→device（`used_device`）、user→ip、user→address。"""
    test_db = db.get_db()
    writer = EdgeWriter()
    await writer.write_for_event(_event(), _decision())
    rows = await test_db[COLL_ENTITY_EDGES].find({}).to_list(length=10)
    pairs = {(row["to_type"], row["to_id"], row["relation"]) for row in rows}
    assert pairs == {
        ("device", DEMO_DEVICE, "used_device"),
        ("ip", DEMO_IP, "shared_ip"),
        ("address", DEMO_ADDRESS, "shared_address"),
    }
    assert all(row["from_type"] == "user" and row["from_id"] == DEMO_USER for row in rows)
    # 设备/地址画像行也被建立（含计数初值）
    assert (await test_db[COLL_DEVICES].find_one({"_id": DEMO_DEVICE}))["linked_user_cnt"] == 1
    assert (await test_db[COLL_USERS].count_documents({"_id": DEMO_USER})) == 1


async def test_same_phone_edge_is_user_to_user_and_single_direction():
    """`same_phone`：手机号关联的多个账号之间建**单向、去重**的 user↔user 边。

    E14 的编码约束不接受 `phone` 作为图节点（手机号是用户属性），因此这条关系
    落在两个账号之间。方向固定按编号字典序：唯一索引是
    `from_id + to_id + relation`，`A→B` 与 `B→A` 会是**两条**记录，
    图上就会画出两条重叠的边、边数翻倍。
    """
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_many([
        user_doc("U-A", phone="139****0001"),
        user_doc("U-B", phone="139****0001"),
        user_doc("U-Z", phone="139****0001"),
        user_doc("U-C", phone="138****0002"),
    ])
    writer = EdgeWriter()
    await writer.write_for_event(
        _event(user_id="U-Z", phone="139****0001", device_id=None, ip=None,
               address_id=None), _decision())

    rows = await test_db[COLL_ENTITY_EDGES].find({"relation": "same_phone"}).to_list(length=10)
    assert len(rows) == 2, "与两个同号账号各建一条"
    others: set[str] = set()
    for row in rows:
        assert row["from_type"] == "user" and row["to_type"] == "user"
        assert row["from_id"] < row["to_id"], "方向必须归一（字典序），否则会出现重复边"
        # 边的另一端就是那个同号账号（U-Z 在哪一端由字典序决定，不硬编码）
        others.add(row["from_id"] if row["to_id"] == "U-Z" else row["to_id"])
    assert others == {"U-A", "U-B"}


async def test_same_phone_lookup_is_bounded():
    """同号账号可能很多（脱敏号区分度低）：建边数量必须有上限。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_many(
        [user_doc(f"U-P{i:03d}", phone="139****0001") for i in range(SAME_PHONE_MAX + 10)]
    )
    writer = EdgeWriter()
    await writer.write_for_event(
        _event(user_id="U-P000", phone="139****0001", device_id=None, ip=None,
               address_id=None), _decision())
    rows = await test_db[COLL_ENTITY_EDGES].count_documents({"relation": "same_phone"})
    assert rows == SAME_PHONE_MAX, f"一次登录不该写出成百上千条边，实际 {rows}"


# ============================================================ 统计 / 标签 / 决策
async def test_stat_and_latest_decision_are_updated_from_the_event():
    """`bump_stat` 与 `latest_decision` 由事件侧的异步链路维护。"""
    test_db = db.get_db()
    writer = EdgeWriter()
    await writer.write_for_event(_event(event_type="order_pay", amount=12300,
                                       device_id=None, ip=None, address_id=None),
                                _decision("pass", 12.0))
    doc = await test_db[COLL_USERS].find_one({"_id": DEMO_USER})
    assert doc["stat"]["total_amount"] == 12300
    assert doc["latest_decision"]["risk_score"] == 12.0
    assert doc["latest_decision"]["decision"] == "pass"

    await writer.write_for_event(_event(event_type="login", amount=None,
                                       device_id=None, ip=None, address_id=None),
                                _decision("reject", 88.0))
    doc = await test_db[COLL_USERS].find_one({"_id": DEMO_USER})
    assert doc["stat"]["block_cnt"] == 1, "拦截数只在 reject 时累加"
    assert doc["latest_decision"]["decision"] == "reject", "只保留最近一次"


async def test_cluster_tag_applied_at_threshold_on_new_edges():
    """聚集度达到 §2.1 的 ≥5 阈值时，自动打 `device_cluster`。"""
    test_db = db.get_db()
    writer = EdgeWriter()
    for index in range(5):
        await writer.write_for_event(
            _event(user_id=DEMO_USER if index == 4 else f"U{800000 + index}",
                   ip=None, address_id=None), _decision())
    doc = await test_db[COLL_USERS].find_one({"_id": DEMO_USER})
    assert doc["risk_tags"] == ["device_cluster"]


async def test_auto_tag_does_not_resurrect_manually_removed_tag():
    """BR-09-04：人工移除的标签**不会**在后续同一条旧证据上自动复活。

    自动打标只在"新证据"（新边/新 IP 行）上评估。若每次事件都补打一遍，
    移除操作就等于白做，审计里那条记录也失去了意义。
    """
    test_db = db.get_db()
    await test_db[COLL_DEVICES].insert_one(device_doc(DEMO_DEVICE, linked_user_cnt=0))
    writer = EdgeWriter()
    for index in range(5):
        await writer.write_for_event(
            _event(user_id=f"U{700000 + index}", ip=None, address_id=None), _decision())
    await test_db[COLL_USERS].update_one(
        {"_id": f"U{700000 + 4}"}, {"$pull": {"risk_tags": "device_cluster"}})

    # 再来一条**同样的**事件（边已存在 → 不是新证据）
    await writer.write_for_event(
        _event(user_id=f"U{700000 + 4}", ip=None, address_id=None), _decision())
    doc = await test_db[COLL_USERS].find_one({"_id": f"U{700000 + 4}"})
    assert doc["risk_tags"] in ([], None) or "device_cluster" not in (
        doc.get("risk_tags") or []), "旧证据上的自动打标不得复活人工移除的标签"

    # 但"新证据"（新账号加入同一设备）仍会重新评估并打标
    await writer.write_for_event(
        _event(user_id="U-NEW-JOIN", ip=None, address_id=None), _decision())
    new_doc = await test_db[COLL_USERS].find_one({"_id": "U-NEW-JOIN"})
    assert new_doc["risk_tags"] == ["device_cluster"]


async def test_proxy_ip_tag_is_applied_for_new_evidence():
    """`is_proxy=true` 的 IP → 打 `proxy_ip` 标签（§2.1 的橙色档）。"""
    test_db = db.get_db()
    await test_db[COLL_IP_POOL].insert_one(
        ip_doc("1.2.3.4", is_proxy=True, is_idc=True, linked_user_cnt=0))
    writer = EdgeWriter()
    await writer.write_for_event(
        _event(ip="1.2.3.4", device_id=None, address_id=None), _decision())
    doc = await test_db[COLL_USERS].find_one({"_id": DEMO_USER})
    assert "proxy_ip" in doc["risk_tags"]


# ============================================================ V-09-10 异步
async def test_enqueue_is_synchronous_and_does_not_block():
    """`V-09-10`：入队是**同步**动作（决策链路上不看库），写库在后台任务里。

    断言三件事：① `enqueue_after_decision` 不是协程（不能 await，也就无法
    在决策链路上被"顺手 await"）；② 调用返回后立刻返回，不等写库完成；
    ③ `flush()` 之后数据确实落库了（后台任务真的跑了，不是"什么都没做"）。
    """
    writer = EdgeWriter()
    assert not asyncio.iscoroutinefunction(writer.enqueue_after_decision)
    writer.enqueue_after_decision(_event(), _decision())
    assert writer.stats["enqueued"] == 1
    assert await db.get_db()[COLL_IP_POOL].count_documents({}) == 0, (
        "入队调用返回时还没写库——写库属于后台任务（BR-09-07）"
    )
    assert await writer.flush() is True
    assert await db.get_db()[COLL_IP_POOL].count_documents({}) == 1
    assert writer.stats["processed"] == 1


async def test_enqueue_performs_no_io_at_all():
    """`V-09-10` 的硬证据：入队**完全不碰数据库**（连一个查询都不发）。

    做法是把依赖换成"一碰就抛"的替身：只要 `enqueue_after_decision` 里出现了
    任何一次读写，这里就会立刻炸。这比"测一下接口耗时"确定得多——
    后者会被机器负载左右，而"决策链路上有没有 IO"是一个**结构性**事实。
    """
    writer = EdgeWriter(repo=ExplodingRepo(), graph=FailingGraphRepo())  # type: ignore[arg-type]
    writer.enqueue_after_decision(_event(), _decision())      # 不得抛任何异常
    assert writer.stats["enqueued"] == 1
    assert writer.stats["processed"] == 0, "此刻后台任务还没跑（也可能刚被调度）"
    await writer.stop()          # 收尾：让那个注定失败的后台任务停下来


async def test_module_level_enqueue_and_flush_work_together():
    """模块级入口（03 调用的那个）与 `flush` 配套可用。"""
    edge_writer_mod.enqueue_after_decision(_event(), _decision())
    assert await edge_writer_mod.flush() is True
    assert await db.get_db()[COLL_ENTITY_EDGES].count_documents({}) == 3


async def test_stop_drains_and_clears_pending():
    """`stop()` 必须把待写队列清空（测试夹具与优雅关闭都靠它）。"""
    writer = EdgeWriter()
    writer.enqueue_after_decision(_event(), _decision())
    assert await writer.stop() is True
    writer.enqueue_after_decision(_event(event_type="login"), _decision())
    await writer.stop()
    assert writer.stats["dropped"] > 0 or writer.stats["processed"] >= 1


async def test_queue_overflow_drops_oldest_and_counts():
    """队列有界：溢出时丢最旧并**计数告警**（BR-09-11 的"计数告警"）。"""
    writer = EdgeWriter()
    original = edge_writer_mod.QUEUE_MAX
    edge_writer_mod.QUEUE_MAX = 2
    try:
        for _ in range(4):
            writer.enqueue_after_decision(_event(), _decision())
        assert writer.stats["dropped"] == 2
        assert len(writer._pending) <= 2          # noqa: SLF001 - 断言队列长度
    finally:
        edge_writer_mod.QUEUE_MAX = original
        await writer.stop()


# ============================================================ BR-09-11 失败与重试
async def test_write_failure_goes_to_retry_queue_and_alarm():
    """BR-09-11：建图失败**不影响决策**——入重试队列 + 计数告警。

    这条路径必须真实验证：它保护的正是"Mongo 抖一下，业务就被中断"这类事故。
    """
    failing = FailingGraphRepo()
    writer = EdgeWriter(repo=ProfileRepo(db.get_db()), graph=failing)
    writer.enqueue_after_decision(_event(), _decision())
    await writer.flush(timeout=5)

    assert failing.attempts >= 2, "失败的任务应当按 RETRY_ATTEMPTS 重试"
    assert writer.stats["failed"] == 1
    assert writer.stats["requeued"] == 1
    assert len(writer._retry_queue) == 1          # noqa: SLF001
    assert DEGRADED.degraded is True, "必须置降级标记（否则故障无人知晓）"
    assert "GRP-5003" in (DEGRADED.last_error or "")


async def test_retry_pending_writes_after_recovery():
    """重试队列里的任务在依赖恢复后应真的写成功（`retry_pending`）。"""
    writer = EdgeWriter(repo=ProfileRepo(db.get_db()), graph=FailingGraphRepo())
    writer.enqueue_after_decision(_event(), _decision())
    await writer.flush(timeout=5)
    assert writer.stats["requeued"] == 1

    writer.reset_dependencies()          # 依赖恢复（换成真实仓储）
    written = await writer.retry_pending()
    assert written == 1
    assert writer.stats["retried"] == 1
    assert await db.get_db()[COLL_ENTITY_EDGES].count_documents({}) == 3


async def test_retry_queue_is_bounded():
    """重试队列同样有界（丢最旧）：Mongo 长时间不可用时不能吃光内存。"""
    writer = EdgeWriter()
    for _ in range(edge_writer_mod.RETRY_QUEUE_MAX + 5):
        writer._enqueue_retry({"event": _event(), "decision": {}})   # noqa: SLF001
    assert len(writer._retry_queue) <= edge_writer_mod.RETRY_QUEUE_MAX   # noqa: SLF001
    assert writer.stats["requeued"] == edge_writer_mod.RETRY_QUEUE_MAX + 5


# ============================================================ §3.3 add_edge
async def test_add_edge_direct_and_count_once():
    """§3.3 的 `add_edge`：直接写一条边，新边同样只递增一次计数。"""
    test_db = db.get_db()
    await test_db[COLL_DEVICES].insert_one(device_doc("D-X", linked_user_cnt=0))
    await edge_writer_mod.add_edge("user", "U1", "device", "D-X", "used_device")
    await edge_writer_mod.add_edge("user", "U1", "device", "D-X", "used_device")
    assert await test_db[COLL_ENTITY_EDGES].count_documents({}) == 1
    assert (await test_db[COLL_DEVICES].find_one({"_id": "D-X"}))["linked_user_cnt"] == 1


async def test_add_edge_user_to_user_does_not_touch_counts():
    """user↔user 的边（转交/同号）不该动任何实体上的 `linked_user_cnt`。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc("U1"))
    await edge_writer_mod.add_edge("user", "U1", "user", "U2", "transferred_to")
    doc = await test_db[COLL_USERS].find_one({"_id": "U1"})
    assert "linked_user_cnt" not in doc


# ============================================================ 循环绑定
async def test_loop_change_drops_pending_and_counts():
    """事件循环更换时必须丢弃旧队列并**计数**（跨用例/热重载的卫生问题）。"""
    writer = EdgeWriter()
    writer._pending = [{"event": _event(), "decision": {}}]     # noqa: SLF001
    writer._loop = None                                          # noqa: SLF001
    writer._ensure_loop()                                        # noqa: SLF001
    assert writer.stats["dropped"] == 1
    assert writer._pending == []                                 # noqa: SLF001
    assert await db.get_db()[COLL_ENTITY_EDGES].count_documents({}) == 0
