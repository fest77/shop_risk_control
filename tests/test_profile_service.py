# -*- coding: utf-8 -*-
"""画像聚合服务：组装、`null` 语义、脱敏、增量维护（模块 09 §3.1 / §4.1）。

对应验收：`V-09-01`（画像四类明细齐全）、`V-09-03`（手机号脱敏 + **不含明文地址**）、
`V-09-04`（聚集度 ≥5 的标红依据）、`V-09-13`（标签不重复 + 移除写审计）、
`V-09-14`（画像查询失败 → `GRP-5001`）。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import (
    COLL_AUDIT_LOGS,
    COLL_DEVICES,
    COLL_ENTITY_EDGES,
    COLL_IP_POOL,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.errors import AppError
from app.repos.profile_repo import ProfileRepo
from app.repos.graph_repo import GraphRepo
from app.services import audit_service
from app.services.profile_service import (
    CLUSTER_ALERT_THRESHOLD,
    STAT_FIELDS,
    MongoLinkedUserCountProvider,
    ProfileService,
    age_days_of,
    cluster_tags,
    is_proxy_of,
    masked_detail_of,
    stat_updates_for,
)
from app.utils.timeutil import now_ms
from tests.profile_testlib import (
    DEMO_ADDRESS,
    DEMO_DEVICE,
    DEMO_IP,
    DEMO_USER,
    ExplodingGraphRepo,
    ExplodingRepo,
    address_doc,
    device_doc,
    edge_doc,
    insert_edges,
    insert_profile,
    ip_doc,
    user_doc,
)

pytestmark = pytest.mark.anyio


def service() -> ProfileService:
    return ProfileService()


# ============================================================ V-09-01 组装
async def test_profile_contains_all_four_sections_and_tags():
    """`V-09-01`：响应含 `user/stat/device/ip/address` 与标签数组，且 `tag_meta` 齐全。"""
    test_db = db.get_db()
    await insert_profile(test_db)
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "device", DEMO_DEVICE, "used_device", 3),
        edge_doc("user", DEMO_USER, "ip", DEMO_IP, "shared_ip", 2),
        edge_doc("user", DEMO_USER, "address", DEMO_ADDRESS, "shared_address", 1),
    ])
    data = await service().get_profile(DEMO_USER)

    assert set(data) == {"user", "stat", "latest_decision", "device", "ip",
                         "address", "tag_meta"}
    assert data["user"]["user_id"] == DEMO_USER
    assert data["user"]["risk_tags"] == ["device_cluster", "blacklist_history"]
    assert data["user"]["level"] == "normal" and data["user"]["status"] == "active"
    assert data["user"]["age_days"] == 42
    assert data["stat"] == {"order_cnt": 3, "aftersale_cnt": 1, "block_cnt": 1,
                            "total_amount": 20000}
    assert data["latest_decision"]["decision"] == "review"
    assert data["latest_decision"]["risk_level"] == "high"
    assert data["device"] == {"device_id": DEMO_DEVICE, "linked_user_cnt": 1,
                              "first_seen_at": data["device"]["first_seen_at"],
                              "os": "Android 13"}
    assert data["ip"]["ip"] == DEMO_IP and data["ip"]["is_proxy"] is False
    assert data["ip"]["region"] == "湖南省长沙市" and data["ip"]["isp"] == "中国移动"
    assert data["address"]["address_id"] == DEMO_ADDRESS
    assert data["address"]["aftersale_cnt"] == 2
    # 8 个标签全部下发（含配色档位），前端不必硬编码（§3.1）
    assert len(data["tag_meta"]) == 8
    assert data["tag_meta"]["device_cluster"] == {
        "label": "设备聚集", "severity": "high", "color": "red"}
    assert data["tag_meta"]["ip_cluster"]["color"] == "orange"
    assert data["tag_meta"]["high_freq"]["severity"] == "low"


async def test_profile_picks_latest_entity_per_relation():
    """画像卡上的设备/IP/地址取**最近共现**的那个（按 `last_seen_at`）。

    为什么不能用 weight 排序：半年前刷出来的强关联不该被显示成"当前设备"。
    """
    test_db = db.get_db()
    await insert_profile(test_db, device=False)
    now = now_ms()
    await test_db[COLL_DEVICES].insert_many([
        device_doc("D-OLD", now=now - 100 * 86_400_000),
        device_doc("D-NEW", now=now),
    ])
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "device", "D-OLD", "used_device", 99,
                 last_seen_at=now - 100 * 86_400_000),
        edge_doc("user", DEMO_USER, "device", "D-NEW", "used_device", 1,
                 last_seen_at=now),
    ])
    data = await service().get_profile(DEMO_USER)
    assert data["device"]["device_id"] == "D-NEW", (
        "画像卡要回答'现在挂在什么设备上'，不能被高 weight 的历史边顶掉"
    )


async def test_profile_falls_back_to_address_lookup_without_edge():
    """没有 `shared_address` 边时按 `user_id` 反查地址（历史数据兜底）。"""
    test_db = db.get_db()
    await insert_profile(test_db, ip=False)
    data = await service().get_profile(DEMO_USER)
    assert data["address"]["address_id"] == DEMO_ADDRESS
    assert data["ip"] is None


# ============================================================ V-09-03 脱敏
async def test_profile_masks_phone_and_never_returns_plaintext_address():
    """`V-09-03`：手机号形如 `138****6621`，响应**不含明文地址**。

    这里故意往库里塞一条带明文 `detail` 的历史脏数据，验证读侧的纵深防御
    （`sanitize_address`）：写入侧不存明文，但历史数据/人工修库可能带进来，
    而"响应里出现明文地址"就是一次真实的数据泄露。
    """
    test_db = db.get_db()
    await insert_profile(test_db)
    await test_db[COLL_USERS].update_one(
        {"_id": DEMO_USER}, {"$set": {"phone": "13812346621"}})
    await test_db[COLL_USER_ADDRESSES].update_one(
        {"_id": DEMO_ADDRESS},
        {"$set": {"detail": "湖南省长沙市岳麓区文一西路 969 号 3 栋 1802"}},
    )
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "address", DEMO_ADDRESS, "shared_address", 1),
    ])

    data = await service().get_profile(DEMO_USER)
    assert data["user"]["phone_masked"] == "138****6621"
    assert "13812346621" not in str(data)
    # 展示值只到区县一级，且绝不含门牌/楼栋
    assert data["address"]["masked_detail"] == "湖南省长沙市****"
    for leak in ("文一西路", "969", "1802", "3 栋"):
        assert leak not in str(data), f"响应里泄露了地址明文片段：{leak}"


async def test_repo_sanitize_drops_plaintext_fields_only():
    """读侧清洗：只丢明文相关字段，其它字段原样保留。"""
    from app.repos.profile_repo import sanitize_address

    doc = {"_id": "A1", "detail": "明文", "address_detail": "明文2",
           "full_address": "明文3", "detail_text": "明文4",
           "province": "浙江省", "detail_hash": "h1"}
    cleaned = sanitize_address(doc)
    assert set(cleaned) == {"_id", "province", "detail_hash"}
    assert doc["detail"] == "明文", "清洗必须返回新 dict，不得就地改库对象"


# ============================================================ null 语义
async def test_missing_fields_are_null_not_zero():
    """`null` = "不知道"，绝不用 0/空串冒充（模块 09 的核心契约）。

    一个只被事件侧 `$inc` 建过行的账号：`register_at`/`level`/`status` 都不知道，
    此时 `age_days` 必须是 `null`——`0` 是一个断言："今天刚注册"。
    """
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(
        {"_id": "U-EVENT-ONLY", "risk_tags": [], "stat": {"order_cnt": 2}})
    data = await service().get_profile("U-EVENT-ONLY")
    assert data["user"]["register_at"] is None
    assert data["user"]["age_days"] is None
    assert data["user"]["level"] is None and data["user"]["status"] is None
    assert data["user"]["phone_masked"] is None
    assert data["stat"] == {"order_cnt": 2, "aftersale_cnt": 0, "block_cnt": 0,
                            "total_amount": 0}
    assert data["device"] is None and data["ip"] is None and data["address"] is None
    assert data["latest_decision"] is None


async def test_ip_is_proxy_is_null_when_row_missing_or_field_absent():
    """`ip.is_proxy`：E12 缺行或字段缺失 → `null`（**不是 false**，决策 D46）。

    给 `false` 等于向 05 宣称"这个 IP 是干净的住宅 IP"，是本次计算里最危险的
    一次猜测；`null` 才是"我们不知道"。
    """
    test_db = db.get_db()
    await insert_profile(test_db, ip=False)
    await insert_edges(test_db, [
        edge_doc("user", DEMO_USER, "ip", "9.9.9.9", "shared_ip", 1),
    ])
    data = await service().get_profile(DEMO_USER)
    assert data["ip"]["ip"] == "9.9.9.9" and data["ip"]["is_proxy"] is None

    await test_db[COLL_IP_POOL].insert_one({"_id": "9.9.9.9", "linked_user_cnt": 1})
    data = await service().get_profile(DEMO_USER)
    assert data["ip"]["is_proxy"] is None, "字段缺失时不得推算 false"
    assert data["ip"]["region"] is None and data["ip"]["isp"] is None


async def test_plaintext_free_address_falls_back_to_none():
    """只有 `detail_hash` 的地址行：展示值为 `null`（前端显示「—」）。"""
    assert masked_detail_of({"_id": "A", "detail_hash": "h"}) is None
    assert masked_detail_of({"_id": "A", "masked_detail": "  上海市**** "}) == "上海市****"
    assert masked_detail_of(None) is None


# ============================================================ 错误路径
async def test_unknown_user_raises_grp_4004():
    """用户不存在 → `GRP-4004`（404），不是 503。"""
    with pytest.raises(AppError) as exc:
        await service().get_profile("U-NOT-EXIST")
    assert exc.value.code == "GRP-4004" and exc.value.http_status == 404


async def test_dependency_failure_raises_grp_5001_not_404():
    """`V-09-14`（服务层）：画像聚合查询失败 → `GRP-5001`（503）。

    这条钉住"故障 ≠ 查无此人"：把 Mongo 抖动显示成 404 会让审核员得出
    "没有这个人"的结论，而那是一次被伪装成业务结论的技术故障。
    """
    svc = ProfileService(repo=ExplodingRepo())
    with pytest.raises(AppError) as exc:
        await svc.get_profile(DEMO_USER)
    assert exc.value.code == "GRP-5001"
    assert exc.value.http_status == 503
    assert "暂时不可用" in exc.value.message


async def test_graph_read_failure_degrades_to_user_only_profile():
    """边的读取失败不该让整张画像卡 503（用户/stat/标签仍然可看），但**必须记日志**。"""
    svc = ProfileService(graph=ExplodingGraphRepo())  # type: ignore[arg-type]
    await insert_profile(db.get_db())
    data = await svc.get_profile(DEMO_USER)
    assert data["user"]["user_id"] == DEMO_USER
    assert data["device"] is None and data["ip"] is None


async def test_unexpected_graph_error_also_degrades_instead_of_500():
    """连**非预期**异常也不该让画像卡消失（关联实体只是可选增强）。

    这条挡住一种很实际的退化：某个仓储方法改了签名、抛 `AttributeError`，
    结果整张画像卡变成 500——而用户/统计/标签这三块其实完全可读。
    降级的同时必须打 WARNING（不能静默）。
    """
    svc = ProfileService(graph=ExplodingRepo())  # type: ignore[arg-type]
    await insert_profile(db.get_db())
    data = await svc.get_profile(DEMO_USER)
    assert data["user"]["user_id"] == DEMO_USER
    assert data["stat"]["order_cnt"] == 3


async def test_masked_phone_of_is_idempotent_and_masks_plaintext():
    """手机号展示值必须**幂等**：已脱敏的原样返回、明文的当场掩码。

    幂等性不是洁癖：`mask_phone("139****0001")` 会按"非纯数字"的通用规则
    掩成 `13****01`，把已经正确的脱敏值破坏掉——那会让画像卡显示一个错误号码。
    """
    from app.services.profile_service import masked_phone_of

    assert masked_phone_of("139****0001") == "139****0001"
    assert masked_phone_of("13900000001") == "139****0001"
    assert masked_phone_of("13812346621") == "138****6621"
    assert masked_phone_of("") is None and masked_phone_of(None) is None
    assert masked_phone_of("  ") is None


# ============================================================ stat 映射（BR-09-02 / D12）
@pytest.mark.parametrize("event_type,amount,expected", [
    ("order_create", 9900, {"order_cnt": 1}),
    # 决策 D12：事件类型是字面量 `order_pay`（场景码才是 `pay`）
    ("order_pay", 9900, {"total_amount": 9900}),
    ("after_sale_apply", 29900, {"aftersale_cnt": 1}),
    ("login", None, {}),
    ("coupon_receive", 2000, {}),
])
async def test_stat_updates_follow_event_type(event_type, amount, expected):
    """按事件类型累加 `users.stat`：下单计笔数、支付计金额、售后计次数。"""
    event = {"event_type": event_type, "amount": amount}
    assert stat_updates_for(event) == expected


async def test_stat_updates_block_cnt_only_on_reject():
    """`block_cnt` 取决于**决策结果**而不是事件类型：只有 `reject` 才计。

    降级（`review`）**不计**：它表示"没算出来、转人工"，而不是"拦住了"。
    混在一起会让拦截率虚高，而拦截率是大盘与日报的头号指标。
    """
    event = {"event_type": "login"}
    assert stat_updates_for(event, {"decision": "reject"}) == {"block_cnt": 1}
    assert stat_updates_for(event, {"decision": "review"}) == {}
    assert stat_updates_for(event, {"decision": "pass"}) == {}
    assert stat_updates_for(event, {}) == {}


async def test_bump_stat_updates_document_and_rejects_unknown_field():
    """`bump_stat`（§3.3）真的落到 `users.stat`；未知字段名必须报错。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc())
    svc = service()
    await svc.bump_stat(DEMO_USER, "order_cnt", 2)
    await svc.bump_stat(DEMO_USER, "total_amount", 500)
    doc = await test_db[COLL_USERS].find_one({"_id": DEMO_USER})
    assert doc["stat"]["order_cnt"] == 5 and doc["stat"]["total_amount"] == 20500

    with pytest.raises(AppError) as exc:
        await svc.bump_stat(DEMO_USER, "odrer_cnt", 1)     # 拼错的字段名
    assert exc.value.code == "COM-4001" and exc.value.http_status == 422
    assert set(STAT_FIELDS) == {"order_cnt", "aftersale_cnt", "block_cnt", "total_amount"}


async def test_bump_stats_batches_fields_in_one_update():
    """一次 `$inc` 带多个字段，避免"单数加了、金额没加"的中间态。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc())
    await service().bump_stats(DEMO_USER, {"order_cnt": 1, "total_amount": 100})
    doc = await test_db[COLL_USERS].find_one({"_id": DEMO_USER})
    assert doc["stat"]["order_cnt"] == 4 and doc["stat"]["total_amount"] == 20100


async def test_bump_stat_creates_minimal_row_without_inventing_profile():
    """事件侧出现的陌生账号：只建 `stat`，**不编造** `register_at`/`level`。"""
    test_db = db.get_db()
    await service().bump_stat("U-BRAND-NEW", "order_cnt", 1)
    doc = await test_db[COLL_USERS].find_one({"_id": "U-BRAND-NEW"})
    assert doc["stat"]["order_cnt"] == 1
    assert "register_at" not in doc and "level" not in doc
    data = await service().get_profile("U-BRAND-NEW")
    assert data["user"]["age_days"] is None


# ============================================================ V-09-13 标签
async def test_tag_twice_keeps_one_and_reports_new_only_once():
    """`V-09-13` 前半段：同一标签打两次仍只有 1 个（`$addToSet` 集合语义）。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc("U-TAG", risk_tags=[]))
    svc = service()
    assert await svc.tag_user("U-TAG", "device_cluster") is True
    assert await svc.tag_user("U-TAG", "device_cluster") is False, (
        "第二次必须报告'没有新增'（这是唯一能让调用方知道去重生效的信号）"
    )
    doc = await test_db[COLL_USERS].find_one({"_id": "U-TAG"})
    assert doc["risk_tags"] == ["device_cluster"]


async def test_unknown_tag_is_rejected():
    """未知标签拒绝写入：`tag_meta` 只有 8 项，写别的会让前端拿到一个没有颜色的标签。"""
    await db.get_db()[COLL_USERS].insert_one(user_doc("U-TAG2", risk_tags=[]))
    with pytest.raises(AppError) as exc:
        await service().tag_user("U-TAG2", "not_a_tag")
    assert exc.value.code == "COM-4001"


async def test_untag_writes_audit_with_before_and_after():
    """`V-09-13` 后半段：显式移除标签必须**写审计**（BR-09-04）。

    `before`/`after` 都要带完整标签数组：只记"移除了 device_cluster"回答不了
    "移除前它还有哪些标签"，而误打的常见情形恰恰是"顺手多打了几个"。
    """
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(
        user_doc("U-UNTAG", risk_tags=["device_cluster", "blacklist_history"]))
    svc = service()
    removed = await svc.untag_user("U-UNTAG", "device_cluster",
                                   actor="reviewer01", actor_role="reviewer")
    assert removed is True
    doc = await test_db[COLL_USERS].find_one({"_id": "U-UNTAG"})
    assert doc["risk_tags"] == ["blacklist_history"], "只移除指定标签"

    await audit_service.flush()
    logs = await test_db[COLL_AUDIT_LOGS].find({"action": "profile.tag.remove"}).to_list(length=5)
    assert len(logs) == 1, "移除操作必须留下一条审计"
    assert logs[0]["target_type"] == "user" and logs[0]["target_id"] == "U-UNTAG"
    assert logs[0]["before"]["risk_tags"] == ["device_cluster", "blacklist_history"]
    assert logs[0]["after"]["removed_tag"] == "device_cluster"
    assert logs[0]["after"]["risk_tags"] == ["blacklist_history"]
    assert logs[0]["actor"] == "reviewer01"


async def test_untag_absent_tag_returns_false_and_still_audits():
    """移除一个不存在的标签：返回 False（没有变更）但仍留痕（尝试本身要可追溯）。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc("U-UNTAG2", risk_tags=["ip_cluster"]))
    removed = await service().untag_user("U-UNTAG2", "new_account", actor="admin01")
    assert removed is False
    await audit_service.flush()
    logs = await test_db[COLL_AUDIT_LOGS].find(
        {"action": "profile.tag.remove", "target_id": "U-UNTAG2"}).to_list(length=5)
    assert len(logs) == 1 and logs[0]["after"]["removed"] is False


# ============================================================ 聚集度与标签阈值
@pytest.mark.parametrize("count,tagged", [
    (0, False), (4, False), (CLUSTER_ALERT_THRESHOLD - 1, False),
    (CLUSTER_ALERT_THRESHOLD, True), (12, True),
])
async def test_cluster_tag_threshold_matches_spec(count, tagged):
    """§2.1 的"**N ≥ 5** 时标红"：阈值边界 4 → 不标、5 → 标。"""
    tags = cluster_tags(device_cnt=count)
    assert ("device_cluster" in tags) is tagged
    assert CLUSTER_ALERT_THRESHOLD == 5, "阈值是规格里的数字，改动必须同步前端配色说明"


async def test_cluster_tags_cover_only_evidence_backed_tags():
    """只自动打**有数据依据**的 4 个标签；其余 4 个归 04/05 的判定口径。"""
    tags = cluster_tags(device_cnt=12, ip_cnt=5, address_cnt=8, is_proxy=True)
    assert set(tags) == {"device_cluster", "ip_cluster", "address_cluster", "proxy_ip"}
    assert cluster_tags() == []
    assert cluster_tags(device_cnt=None, is_proxy=None) == []


@pytest.mark.parametrize("doc,expected", [
    (None, None), ({}, None), ({"is_proxy": False}, False),
    ({"is_proxy": True}, True), ({"is_idc": True}, True),
    ({"is_proxy": False, "is_idc": False}, False),
    ({"region": "x"}, None),
])
async def test_is_proxy_semantics(doc, expected):
    """`is_proxy` 的口径与 `feature_repo._pick_proxy` **必须一致**（两处不能漂移）。"""
    assert is_proxy_of(doc) is expected


@pytest.mark.parametrize("register_at,expected_days", [
    (None, None), (0, None), ("abc", None), (-5, None),
])
async def test_age_days_invalid_inputs(register_at, expected_days):
    assert age_days_of(register_at) is None if expected_days is None else True


async def test_age_days_is_floor_days_and_never_negative():
    now = now_ms()
    assert age_days_of(now - 42 * 86_400_000, at_ms=now) == 42
    assert age_days_of(now - 86_400_000 + 1000, at_ms=now) == 0
    assert age_days_of(now + 10 * 86_400_000, at_ms=now) == 0, "未来时间不得算出负天数"


# ============================================================ BR-09-06 最近决策
async def test_set_latest_decision_overwrites_and_bounds_history():
    """BR-09-06：`latest_decision` 覆盖式更新；`risk_score_history` 有界追加。"""
    test_db = db.get_db()
    await test_db[COLL_USERS].insert_one(user_doc("U-DEC", risk_score_history=[]))
    svc = service()
    for i in range(25):
        await svc.set_latest_decision("U-DEC", {
            "risk_score": float(i), "risk_level": "high",
            "decision": "review", "decided_at": now_ms(),
        })
    doc = await test_db[COLL_USERS].find_one({"_id": "U-DEC"})
    assert doc["latest_decision"]["risk_score"] == 24.0, "只保留最近一次"
    assert len(doc["risk_score_history"]) == 20, "历史必须有界（$slice -20）"
    assert doc["risk_score_history"][-1]["score"] == 24.0
    assert doc["risk_score_history"][0]["score"] == 5.0


# ============================================================ §3.3 的 04 契约
async def test_linked_user_count_reads_redundant_field():
    """BR-09-13：04 的三项聚集度一律向 09 取，09 读自己维护的冗余计数。"""
    test_db = db.get_db()
    await test_db[COLL_DEVICES].insert_one(device_doc("D-CNT", linked_user_cnt=12))
    await test_db[COLL_IP_POOL].insert_one(ip_doc("1.1.1.1", linked_user_cnt=6))
    await test_db[COLL_USER_ADDRESSES].insert_one(
        address_doc("A-CNT", linked_user_cnt=8))
    provider = MongoLinkedUserCountProvider()
    assert await provider.get_linked_user_count("device", "D-CNT") == 12
    assert await provider.get_linked_user_count("ip", "1.1.1.1") == 6
    assert await provider.get_linked_user_count("address", "A-CNT") == 8
    assert provider.available is True


async def test_linked_user_count_falls_back_to_edges_and_returns_none_when_unknown():
    """三种语义：有画像行 → 读字段；无行 → 如实数边；无法计算 → `None`。

    `None` 与 `0` 的区别是这条用例的核心：`0` 是一个**可证实**的断言
    （"没有任何账号关联"），`None` 是"不知道"（04 会写进 `missing_features`）。
    """
    test_db = db.get_db()
    await insert_edges(test_db, [
        edge_doc("user", "U1", "device", "D-NOROW", "used_device", 1),
        edge_doc("user", "U2", "device", "D-NOROW", "used_device", 1),
        edge_doc("user", "U3", "device", "D-NOROW", "used_device", 1),
    ])
    provider = MongoLinkedUserCountProvider()
    assert await provider.get_linked_user_count("device", "D-NOROW") == 3
    assert await provider.get_linked_user_count("device", "D-NO-ANYTHING") == 0
    assert await provider.get_linked_user_count("phone", "139****0001") is None
    assert await provider.get_linked_user_count("device", "") is None


async def test_linked_user_count_returns_none_on_db_error():
    """查库失败 → `None`（"无法计算"），**绝不返回 0**（BR-04-09 的同一原则）。"""
    class BrokenRepo:
        db = None

        async def get_user(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("simulated db down")

    provider = MongoLinkedUserCountProvider(repo=BrokenRepo())  # type: ignore[arg-type]
    assert await provider.get_linked_user_count("device", "D1") is None


async def test_profile_service_dependencies_can_be_reset():
    """`configure`/`reset_dependencies` 必须能把注入的假依赖清干净（跨用例卫生）。"""
    svc = ProfileService()
    svc.configure(repo=ExplodingRepo())          # type: ignore[arg-type]
    with pytest.raises(AppError):
        await svc.get_profile(DEMO_USER)
    svc.reset_dependencies()
    await insert_profile(db.get_db())
    data = await svc.get_profile(DEMO_USER)
    assert data["user"]["user_id"] == DEMO_USER


async def test_repo_reads_by_id_only():
    """画像读取只走 `_id`（`users`/`devices`/`ip_pool` 因此不需要额外索引）。"""
    test_db = db.get_db()
    await insert_profile(test_db)
    repo = ProfileRepo(test_db)
    assert (await repo.get_user(DEMO_USER))["_id"] == DEMO_USER
    assert (await repo.get_device(DEMO_DEVICE))["_id"] == DEMO_DEVICE
    assert (await repo.get_ip(DEMO_IP))["_id"] == DEMO_IP
    assert (await repo.get_address(DEMO_ADDRESS))["_id"] == DEMO_ADDRESS
    assert await repo.get_user("nope") is None
    assert await repo.find_address_of_user(DEMO_USER) is not None
    assert await repo.find_address_of_user("nope") is None
    assert await repo.get_address("nope") is None
    assert repo.collection_for("phone") is None
    assert repo.collection_for("user") is not None
    assert GraphRepo(test_db).col.name == COLL_ENTITY_EDGES
