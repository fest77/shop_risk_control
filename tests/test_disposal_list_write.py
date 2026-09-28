# -*- coding: utf-8 -*-
"""名单联动的映射、复用与跳过（BR-08-20 ~ 08-24，Spec 08 §6 的 `test_disposal_list_write.py`）。

全部走**真实**的 `ListService.add_auto()` 与真实 Mongo（测试库），
只有"绝不写名单"的那部分用纯函数断言。理由：BR-08-22（唯一冲突复用）与
BR-08-24（缓存主动失效）都是**存储层**的行为，用假仓储验证等于自证。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import COLL_AUDIT_LOGS, COLL_LIST_ENTRIES, COLL_RISK_EVENTS
from app.engine import list_filter
from app.repos.case_action_repo import CaseActionRepo
from app.repos.list_repo import ListRepo
from app.schemas.case_schema import DisposeIn, DisposePreviewIn, default_list_writes
from app.services.disposal_service import get_disposal_service
from app.services.list_service import ListService

from tests.case_testlib import REVIEWER, insert_case

pytestmark = pytest.mark.anyio


# ============================================================ 纯函数：默认映射
async def test_default_mapping_is_exactly_br_08_20():
    """默认映射表逐条（BR-08-20）。"""
    writes, skipped = default_list_writes(
        "violation", ["blacklist_user", "ban_device"], user_id="U1", device_id="D1"
    )
    assert {(w["list_type"], w["entity_type"], w["entity_value"]) for w in writes} == {
        ("black", "user", "U1"), ("black", "device", "D1"),
    }
    assert skipped == []
    assert all(w["expire_at"] is None for w in writes), "黑名单默认永久（expire_at=null）"

    # block_order / reject_refund 不写名单
    writes, _ = default_list_writes(
        "violation", ["block_order", "reject_refund"], user_id="U1", device_id="D1"
    )
    assert writes == []

    # suspicious + pass → 灰名单（观察），且只写灰名单
    writes, _ = default_list_writes("suspicious", ["pass"], user_id="U1", device_id="D1")
    assert [(w["list_type"], w["entity_type"]) for w in writes] == [("gray", "user")]

    # normal + pass → 不写名单
    assert default_list_writes("normal", ["pass"], user_id="U1", device_id="D1")[0] == []


async def test_missing_device_id_skips_the_device_write():
    """BR-08-23：事件没有 `device_id` → 跳过该条映射并**给出提示**，不写空值。"""
    writes, skipped = default_list_writes(
        "violation", ["blacklist_user", "ban_device"], user_id="U1", device_id=None
    )
    assert [(w["entity_type"], w["entity_value"]) for w in writes] == [("user", "U1")]
    assert len(skipped) == 1 and "device_id" in skipped[0]


# ============================================================ 真实写入（BR-08-21）
async def test_add_auto_writes_source_auto_and_case_no(client):
    """BR-08-21：`source=auto` / `related_case_no` / `operator=system` / 永久。"""
    # `client` 夹具只是为了让 `prep_db` 已经建好索引与清空集合（autouse）
    assert client is not None
    service = ListService(ListRepo(db.get_db()))
    result = await service.add_auto(
        list_type="black", entity_type="user", entity_value="U000132",
        reason="案件 CASE0001 处置：拉黑用户", related_case_no="CASE0001",
    )
    doc = await db.get_db()[COLL_LIST_ENTRIES].find_one({"_id": result["entry_id"]})
    assert doc["source"] == "auto"
    assert doc["related_case_no"] == "CASE0001"
    assert doc["operator"] == "system"
    assert doc["status"] == "active"
    assert doc["expire_at"] is None
    assert result["reused"] is False


async def test_add_auto_is_idempotent_by_reuse_not_by_insert(client):
    """BR-08-22 / V-08-09：同实体二次拉黑 → **仍 1 条 active**，且 `reason` 被更新。"""
    assert client is not None
    service = ListService(ListRepo(db.get_db()))
    first = await service.add_auto(
        list_type="black", entity_type="user", entity_value="U000133",
        reason="第一次处置", related_case_no="CASE0002",
    )
    second = await service.add_auto(
        list_type="black", entity_type="user", entity_value="U000133",
        reason="第二次处置（复用）", related_case_no="CASE0003",
    )
    assert second["entry_id"] == first["entry_id"], "必须复用既有条目，不能新增"
    assert second["reused"] is True
    rows = await db.get_db()[COLL_LIST_ENTRIES].find(
        {"entity_value": "U000133", "status": "active"}
    ).to_list(length=10)
    assert len(rows) == 1
    assert rows[0]["reason"] == "第二次处置（复用）"
    assert rows[0]["related_case_no"] == "CASE0003"
    assert rows[0]["list_type"] == "black"


async def test_add_auto_refuses_white_list_and_empty_value(client):
    """处置**不得**授予白名单（免风控授权），也不得写空实体值（BR-08-23）。"""
    assert client is not None
    service = ListService(ListRepo(db.get_db()))
    with pytest.raises(Exception) as white:
        await service.add_auto(list_type="white", entity_type="user",
                               entity_value="U000134", reason="x",
                               related_case_no="CASE0004")
    assert getattr(white.value, "code", None) == "CFG-4009"
    with pytest.raises(Exception) as blank:
        await service.add_auto(list_type="black", entity_type="device",
                               entity_value="   ", reason="x",
                               related_case_no="CASE0004")
    assert getattr(blank.value, "code", None) == "COM-4001"


async def test_add_auto_masks_phone_and_invalidates_cache(client):
    """BR-06-21（phone 脱敏）+ BR-08-24（缓存主动失效）。

    缓存失效的判定走**真实读路径**：先塞一条缓存并确认它命中，写入之后再查
    必须变成未命中——直接看内部字典的大小会把"整表被清空"与"时间戳约定生效"
    两种实现细节混在一起，而 BR-08-24 要求的只是"立刻不再命中旧条目"。
    """
    assert client is not None
    key = ("black", "phone", "139****0135")
    # 第一次 `get` 只用于吸收"时间戳已变 → 整表失效"那一次同步（那是 BR-06-24 的
    # 兜底路径）；随后再 put，缓存的命中与否才只反映**这一次写入**的影响
    list_filter.LIST_CACHE.get(key)
    list_filter.LIST_CACHE.put(key, {"stale": True})
    assert list_filter.LIST_CACHE.get(key)[0] is True, "前置条件：缓存里确实有一条"
    service = ListService(ListRepo(db.get_db()))
    await service.add_auto(
        list_type="black", entity_type="phone", entity_value="13900000135",
        reason="处置联动", related_case_no="CASE0005",
    )
    doc = await db.get_db()[COLL_LIST_ENTRIES].find_one({"entity_type": "phone"})
    assert doc["entity_value"] == "139****0135", "手机号必须脱敏后再入库"
    assert list_filter.LIST_CACHE.get(key)[0] is False, (
        "写入后必须立刻失效名单缓存，而不是等最多 10s 的 TTL（AD-02 / BR-08-24）"
    )


async def test_rollback_auto_sets_removed(client):
    """BR-08-30 的回滚动作：按 `entry_id` 置 `status=removed`（软删，不物理删）。"""
    assert client is not None
    service = ListService(ListRepo(db.get_db()))
    result = await service.add_auto(
        list_type="black", entity_type="device", entity_value="DTEST01",
        reason="待回滚", related_case_no="CASE0006",
    )
    assert await service.rollback_auto(result["entry_id"], "system") is True
    doc = await db.get_db()[COLL_LIST_ENTRIES].find_one({"_id": result["entry_id"]})
    assert doc is not None and doc["status"] == "removed"
    # 幂等：再回滚一次匹配 0 条（状态已不是 active），返回 False 而不是抛错
    assert await service.rollback_auto(result["entry_id"], "system") is False


# ============================================================ 端到端：走真实处置
async def test_dispose_writes_two_black_entries_and_marks_case(client):
    """V-08-08：处置 `blacklist_user + ban_device` → 名单出现 2 条 `source=auto`。"""
    assert client is not None
    db_ = db.get_db()
    case_no = "CASE20260101900001"
    await insert_case(db_, case_no=case_no, status="reviewing", assignee=REVIEWER,
                      user_id="U000132", device_id="DMULE0001")
    service = get_disposal_service()
    preview = await service.preview(
        case_no, _preview_in("violation", ["blacklist_user", "ban_device"]), REVIEWER
    )
    body = await service.dispose(
        case_no, _dispose_in("violation", ["blacklist_user", "ban_device"],
                             "确认批量套券，拉黑并封禁设备", preview["confirm_token"]),
        REVIEWER, actor_role="reviewer",
    )
    assert body["status"] == "disposed"
    assert body["degraded"] is False
    writes = {(w["list_type"], w["entity_type"], w["entity_value"]) for w in body["list_writes"]}
    assert writes == {("black", "user", "U000132"), ("black", "device", "DMULE0001")}
    rows = await db_[COLL_LIST_ENTRIES].find({"source": "auto"},
                                            {"related_case_no": 1}).to_list(length=10)
    assert len(rows) == 2
    assert all(r["related_case_no"] == case_no for r in rows)
    # 名单写入的留痕在本条审计的 after 里（一次处置恰好一条审计）
    logs = await db_[COLL_AUDIT_LOGS].find({"action": "case.dispose"}).to_list(length=5)
    assert len(logs) == 1
    assert len(logs[0]["after"]["list_writes"]) == 2
    assert all(w["entry_id"] for w in logs[0]["after"]["list_writes"])


async def test_pending_case_without_device_skips_device_entry(client):
    """BR-08-23 的端到端分支：事件没有 `device_id` 时不写设备黑名单。"""
    assert client is not None
    db_ = db.get_db()
    case_no = "CASE20260101900002"
    await insert_case(db_, case_no=case_no, status="reviewing", assignee=REVIEWER,
                      user_id="U000136", device_id=None)
    # 事件里也没有 device_id（插入时显式置空）
    await db.get_db()[COLL_RISK_EVENTS].update_one(
        {"_id": "EVT20260101900000000001"}, {"$unset": {"device_id": ""}}
    )
    service = get_disposal_service()
    preview = await service.preview(
        case_no, _preview_in("violation", ["blacklist_user", "ban_device"]), REVIEWER
    )
    assert any(s["code"] == "skip" for s in preview["side_effects"]), (
        "BR-08-23 要求预览里提示『该事件无 device_id，本次不写设备黑名单』"
    )
    body = await service.dispose(
        case_no, _dispose_in("violation", ["blacklist_user", "ban_device"],
                             "设备缺失时的处置", preview["confirm_token"]),
        REVIEWER, actor_role="reviewer",
    )
    assert [w["entity_type"] for w in body["list_writes"]] == ["user"]
    assert body["list_writes_skipped"], "跳过的原因必须回传，前端要显示它"
    # 流水里也只有 user 那一条（归属正确）
    actions = await CaseActionRepo(db_).list_by_case(case_no)
    by_type = {a["action_type"]: a for a in actions}
    assert len(by_type["blacklist_user"]["list_writes"]) == 1
    assert by_type["ban_device"]["list_writes"] == []


# ============================================================ 小工具
def _preview_in(conclusion: str, actions: list[str]) -> DisposePreviewIn:
    return DisposePreviewIn(conclusion=conclusion, action_types=actions)


def _dispose_in(conclusion: str, actions: list[str], remark: str, token: str,
                idempotency_key: str | None = None) -> DisposeIn:
    return DisposeIn(conclusion=conclusion, action_types=actions, remark=remark,
                     confirm_token=token, idempotency_key=idempotency_key)
