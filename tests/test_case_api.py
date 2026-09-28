# -*- coding: utf-8 -*-
"""案件处置的 HTTP 契约与跨模块联动（模块 08 的验收主线）。

覆盖：建案（含 D5 的降级建案）、认领（原子/幂等/并发）、预览与令牌、
处置的七步副作用、审计恰好一条、幂等回放、权限矩阵、归档、流水读取、
超时回收与自动归档、以及"不存在 undo/revert 端点"。

**断言全部落在真实行为上**（HTTP 状态码 + 错误码 + 库里文档），
不 mock 业务服务：本模块的风险恰恰在"接口返回成功、库里没生效"，
用替身写出来的绿是假的。
"""
from __future__ import annotations

import asyncio
import re

import pytest
from pymongo.errors import PyMongoError

from app import db
from app.constants import (
    COLL_AUDIT_LOGS,
    COLL_CASE_ACTIONS,
    COLL_DECISIONS,
    COLL_LIST_ENTRIES,
    COLL_RISK_CASES,
    COLL_RISK_EVENTS,
    COLL_RULES,
    COLL_SYS_USERS,
)
from app.core import case_maintenance_task
from app.engine import decision as decision_engine
from app.engine.case_state import TRANSITIONS as STATE_TRANSITIONS
from app.main import app
from app.utils.timeutil import now_ms

from tests.case_testlib import DEMO_USER, FailingAudit, insert_action, insert_case
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

CASES = "/api/v1/cases"


# ============================================================
# 夹具与小工具
# ============================================================
@pytest.fixture
async def second_reviewer(client, user_hashes):
    """第二个审核员（验证 V-08-03 的"其余 `DSP-4005`"需要**不同**的人来抢）。

    复用 `reviewer01` 的口令哈希：bcrypt 很慢，而这里要验的是"身份不同"，
    不是"口令策略"（后者由模块 01 的用例覆盖）。
    """
    await db.get_db()[COLL_SYS_USERS].insert_one({
        "_id": "reviewer02", "username": "reviewer02",
        "password_hash": user_hashes["reviewer01"], "real_name": "李审核",
        "role": "reviewer", "status": "active",
        "password_changed_at": None, "last_login_at": None,
    })
    r = await client.post("/api/v1/auth/login",
                          json={"username": "reviewer02", "password": "reviewer123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


async def _extra_reviewer_headers(client, user_hashes, index: int) -> dict:
    """按序号造一个审核员并登录（并发认领需要**互不相同**的认领人）。

    为什么必须不同人：BR-08-04 的幂等条款规定"同一 `assignee` 重复认领返回 200"，
    因此同一个人并发 10 次会得到 10 次 200（只有一次真迁移）。V-08-03 要验的是
    "并发下只有一个赢家"，那就必须是**多个不同的人**来抢——同一人的并发属于
    另一条规则（幂等），由 `test_concurrent_claims_by_the_same_reviewer_are_idempotent`
    单独覆盖。
    """
    username = f"reviewer{100 + index}"
    await db.get_db()[COLL_SYS_USERS].insert_one({
        "_id": username, "username": username,
        "password_hash": user_hashes["reviewer01"], "real_name": f"审核{index}",
        "role": "reviewer", "status": "active",
        "password_changed_at": None, "last_login_at": None,
    })
    r = await client.post("/api/v1/auth/login",
                          json={"username": username, "password": "reviewer123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def body_of(response) -> dict:
    payload = response.json()
    assert payload["ok"] is True, payload
    return payload["data"]


def code_of(response) -> str:
    return response.json()["code"]


async def preview(client, case_no: str, conclusion: str, actions: list[str],
                  headers=None) -> dict:
    r = await client.post(
        f"{CASES}/{case_no}/dispose/preview",
        json={"conclusion": conclusion, "action_types": actions},
        headers=headers or READER,
    )
    assert r.status_code == 200, r.text
    return body_of(r)


async def dispose(client, case_no: str, conclusion: str, actions: list[str],
                  token: str, *, remark: str = "确认违规，执行拦截", headers=None,
                  idempotency_key: str | None = None, extra: dict | None = None):
    payload = {"conclusion": conclusion, "action_types": actions, "remark": remark,
               "confirm_token": token}
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    if extra:
        payload.update(extra)
    return await client.post(f"{CASES}/{case_no}/dispose", json=payload,
                             headers=headers or READER)


async def claim(client, case_no: str, headers=None):
    return await client.post(f"{CASES}/{case_no}/claim", headers=headers or READER)


# ============================================================
# 建案（BR-08-01 ~ 03 / D5）
# ============================================================
async def test_decision_hook_creates_case_only_for_review_and_reject():
    """钩子层：`review`/`reject` 建案，`pass` **不建案**（BR-08-01）。"""
    base = {
        "_id": "DEC_TEST_1", "event_id": "EVT_TEST_1", "user_id": DEMO_USER,
        "scene_code": "login", "final_score": 70, "risk_level": "medium",
        "decision": "pass", "degraded": False,
    }
    assert await decision_engine._create_case_if_needed(dict(base)) is None
    assert await db.get_db()[COLL_RISK_CASES].count_documents({}) == 0

    for verdict, decision_id in (("review", "DEC_TEST_2"), ("reject", "DEC_TEST_3")):
        doc = {**base, "_id": decision_id, "event_id": f"EVT_{decision_id}",
               "decision": verdict}
        case_no = await decision_engine._create_case_if_needed(doc)
        assert case_no and case_no.startswith("CASE"), case_no
        case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
        assert case["status"] == "pending"
        assert case["decision"] == verdict
        assert case["decision_id"] == decision_id
        assert case["risk_score"] == 70 and case["risk_level"] == "medium"
        # BR-08-02：同一 decision_id 再触发一次 → 返回既有案件，不新增
        assert await decision_engine._create_case_if_needed(doc) == case_no
    assert await db.get_db()[COLL_RISK_CASES].count_documents({}) == 2


async def test_case_snapshots_event_amount_and_profile_tags():
    """BR-08-03 + E08 的冗余快照：`estimated_loss` 取事件金额、`risk_tags` 取画像标签。"""
    # 用 upsert 而不是 insert：`risk_events` 由 03 的用例自己清理（conftest 不清它），
    # 因此这里必须容忍"上一次运行留下的同编号事件"
    await db.get_db()[COLL_RISK_EVENTS].replace_one(
        {"_id": "EVT_AMOUNT"},
        {"_id": "EVT_AMOUNT", "user_id": "U000150", "amount": 12345,
         "device_id": "D-AMOUNT", "event_type": "order_pay"},
        upsert=True,
    )
    await db.get_db()["users"].replace_one(
        {"_id": "U000150"},
        {"_id": "U000150", "risk_tags": ["device_cluster", "proxy_ip"],
         "register_at": now_ms() - 3 * 86_400_000},
        upsert=True,
    )
    case_no = await decision_engine._create_case_if_needed({
        "_id": "DEC_AMOUNT", "event_id": "EVT_AMOUNT", "user_id": "U000150",
        "scene_code": "pay", "final_score": 90, "risk_level": "high",
        "decision": "reject", "degraded": False,
    })
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["estimated_loss"] == 12345
    assert case["risk_tags"] == ["device_cluster", "proxy_ip"]
    assert case["scene_code"] == "pay"


async def test_degraded_review_still_creates_case():
    """**D5 的核心断言**：降级产生的 `review` 也必须建案，否则请求无人处理。"""
    await db.get_db()[COLL_RULES].insert_one(_rule_doc())
    # 名单依赖不可用 → 05 降级为 review + degraded=true（RUL-5001），并落库
    outcome = await decision_engine.decide(
        {"_id": "EVT_DEGRADED", "event_type": "login", "user_id": "U000151"},
        {"login_cnt_1h": 3},
        list_repo=_FailingListRepo(),
    )
    assert outcome.degraded is True
    assert outcome.block["decision"] == "review"
    assert await decision_engine.flush()
    case = await db.get_db()[COLL_RISK_CASES].find_one({"event_id": "EVT_DEGRADED"})
    assert case is not None, "降级 review 必须建案（D5）"
    assert case["degraded"] is True
    assert case["degrade_code"] == "RUL-5001"
    assert case["status"] == "pending"


async def test_review_decision_from_real_rule_engine_creates_case():
    """V-08-01：真实 05 判出的 `review` → 自动建案并带上决策时的冗余快照。"""
    await db.get_db()[COLL_RULES].insert_one(_rule_doc())
    outcome = await decision_engine.decide(
        {"_id": "EVT_REVIEW", "event_type": "login", "user_id": "U000152",
         "device_id": "D-REVIEW"},
        {"login_cnt_1h": 5},
    )
    assert outcome.block["decision"] == "review", outcome.block
    assert await decision_engine.flush()
    case = await db.get_db()[COLL_RISK_CASES].find_one({"event_id": "EVT_REVIEW"})
    assert case is not None
    assert case["risk_score"] == outcome.block["final_score"]
    assert case["risk_level"] == outcome.block["risk_level"]
    assert case["user_id"] == "U000152"
    # 建案也留了一条痕（D41），且是 strict=False：审计故障不得丢掉案件
    assert await _flush_audit()
    logs = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "case.create"}).to_list(10)
    assert len(logs) == 1 and logs[0]["target_id"] == case["_id"]
    # D5 的兜底扫描：已经建过案的决策不会被重复建案
    assert await case_maintenance_task.run_once() is not None
    assert await db.get_db()[COLL_RISK_CASES].count_documents({}) == 1


async def test_missing_case_is_recovered_by_the_sweeper():
    """补建案扫描：钩子失败（模拟）留下的决策会被下一轮维护补上。"""
    await db.get_db()[COLL_DECISIONS].insert_one({
        "_id": "DEC_ORPHAN", "event_id": "EVT_ORPHAN", "user_id": "U000153",
        "scene_code": "login", "final_score": 80, "risk_level": "high",
        "decision": "reject", "degraded": False, "decided_at": now_ms(),
    })
    assert await db.get_db()[COLL_RISK_CASES].count_documents({}) == 0
    result = await case_maintenance_task.run_once()
    assert result["cases_created"] == 1
    case = await db.get_db()[COLL_RISK_CASES].find_one({"event_id": "EVT_ORPHAN"})
    assert case is not None and case["decision"] == "reject"


# ============================================================
# 认领（BR-08-04 / 05 / 06，V-08-02 / 03）
# ============================================================
async def test_claim_transitions_and_sets_deadline(client):
    await insert_case(db.get_db(), case_no="CASE-API-0001")
    r = await claim(client, "CASE-API-0001")
    assert r.status_code == 200, r.text
    data = body_of(r)
    assert data["status"] == "reviewing"
    assert data["assignee"] == "reviewer01"
    assert data["changed"] is True
    # BR-08-05：deadline = claimed_at + 30 分钟（默认阈值）
    assert data["claim_deadline_at"] - data["claimed_at"] == 30 * 60_000
    assert await _flush_audit()
    logs = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "case.claim"}).to_list(10)
    assert len(logs) == 1, "认领必须恰好一条审计"


async def test_claim_is_idempotent_for_the_same_assignee(client):
    await insert_case(db.get_db(), case_no="CASE-API-0002")
    first = body_of(await claim(client, "CASE-API-0002"))
    second = body_of(await claim(client, "CASE-API-0002"))
    assert second["changed"] is False
    assert second["claimed_at"] == first["claimed_at"], "幂等不得刷新认领时间"
    assert await _flush_audit()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "case.claim"}
    ) == 1, "幂等重放没有产生新的写入，就不该有第二条审计"


async def test_claim_missing_case_is_404(client):
    r = await claim(client, "CASE-NOT-EXIST")
    assert r.status_code == 404 and code_of(r) == "DSP-4040"


@pytest.mark.parametrize("status,code", [
    ("reviewing", "DSP-4005"),   # 已被他人认领 → 说的是"谁拿着"
    ("disposed", "DSP-4003"),    # 状态不允许认领
    ("archived", "DSP-4003"),
])
async def test_claim_on_non_pending_is_rejected(client, status: str, code: str):
    """V-08-02：只有 `pending` 可认领；`reviewing` 让他人认出 `DSP-4005` 而不是 4003。"""
    case_no = f"CASE-API-{status}"
    await insert_case(db.get_db(), case_no=case_no, status=status,
                      assignee="reviewer02" if status == "reviewing" else None)
    r = await claim(client, case_no)
    assert r.status_code == 409, r.text
    assert code_of(r) == code
    if code == "DSP-4005":
        assert r.json()["data"]["assignee"] == "reviewer02"


async def test_concurrent_claims_by_different_reviewers(client, user_hashes):
    """V-08-03：并发认领**恰好一个成功**，其余 `DSP-4005`（BR-08-04 的原子性）。"""
    case_no = "CASE-API-CONC"
    await insert_case(db.get_db(), case_no=case_no)
    headers = [READER] + [
        await _extra_reviewer_headers(client, user_hashes, i) for i in range(1, 6)
    ]
    results = await asyncio.gather(*[claim(client, case_no, h) for h in headers])
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1, f"必须恰好 1 次成功，实际 {codes}"
    assert codes.count(409) == 5
    assert all(code_of(r) == "DSP-4005" for r in results if r.status_code == 409), (
        [(r.status_code, code_of(r), r.json().get("data")) for r in results]
    )
    assert body_of([r for r in results if r.status_code == 200][0])["changed"] is True
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "reviewing" and case["assignee"]
    assert await _flush_audit()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "case.claim"}
    ) == 1, "只有一次真正的状态迁移，因此只有一条审计"


async def test_concurrent_claims_by_the_same_reviewer_are_idempotent(client):
    """同一人并发点两次：都返回 200，但**只有一次真正的迁移**（`changed=true`）。"""
    case_no = "CASE-API-CONC-SAME"
    await insert_case(db.get_db(), case_no=case_no)
    results = await asyncio.gather(*[claim(client, case_no) for _ in range(6)])
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    changed = [body_of(r)["changed"] for r in results]
    assert changed.count(True) == 1, f"只能有一次真正的认领，实际 {changed}"
    await _flush_audit()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "case.claim"}
    ) == 1


async def test_claim_audit_failure_rolls_back_the_claim(client, monkeypatch):
    """D41：认领的审计写不进去 → **回滚这次认领**并返回 `AUD-5001`。"""
    from app.services import case_service as case_service_mod

    case_no = "CASE-API-AUDIT"
    await insert_case(db.get_db(), case_no=case_no)
    monkeypatch.setattr(case_service_mod, "audit", FailingAudit("case.claim"))
    r = await claim(client, case_no)
    assert r.status_code == 503, r.text
    assert code_of(r) == "AUD-5001"
    # 「已回滚」这句提示落在 `data.detail`（`message` 由错误码表统一给出，BR-00-14）
    assert "已回滚" in r.json()["data"]["detail"]
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "pending", "审计失败必须回滚到 pending"
    assert not case.get("assignee") and not case.get("claimed_at")


# ============================================================
# 二次确认令牌（BR-08-26 ~ 28，V-08-07）
# ============================================================
async def test_preview_lists_all_five_side_effects_without_side_effects(client):
    """V-08-06：预览覆盖 ①~⑤ 且**不产生任何副作用**（BR-08-28）。"""
    case_no = "CASE-API-PREVIEW"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01")
    data = await preview(client, case_no, "violation", ["blacklist_user", "ban_device"])
    codes = [s["code"] for s in data["side_effects"]]
    assert codes[:5] == ["case_action", "list_entry", "biz_sync", "audit_log", "case_status"]
    assert data["expires_in"] == 30
    assert data["confirm_token"]
    assert {(w["list_type"], w["entity_type"]) for w in data["list_writes_preview"]} == {
        ("black", "user"), ("black", "device")
    }
    assert data["summary"]["user_id"] == DEMO_USER
    assert data["summary"]["conclusion_label"] == "违规"
    # 动作标签按**归一化后**的顺序（去重 + 字典序，BR-08-15）：`ban_device` < `blacklist_user`
    assert data["summary"]["action_labels"] == ["封禁设备", "拉黑用户"]
    assert data["summary"]["action_types"] == ["ban_device", "blacklist_user"]
    # 无副作用：什么都没写
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0
    assert await db.get_db()[COLL_RISK_CASES].count_documents({"status": "disposed"}) == 0


async def test_preview_requires_reviewing_and_matching_assignee(client, second_reviewer):
    pending = "CASE-API-PV1"
    await insert_case(db.get_db(), case_no=pending)
    r = await client.post(f"{CASES}/{pending}/dispose/preview",
                          json={"conclusion": "normal", "action_types": ["pass"]},
                          headers=READER)
    assert r.status_code == 409 and code_of(r) == "DSP-4003"

    reviewing = "CASE-API-PV2"
    await insert_case(db.get_db(), case_no=reviewing, status="reviewing",
                      assignee="reviewer02")
    r = await client.post(f"{CASES}/{reviewing}/dispose/preview",
                          json={"conclusion": "normal", "action_types": ["pass"]},
                          headers=READER)
    assert r.status_code == 409 and code_of(r) == "DSP-4005"
    assert r.json()["data"]["assignee"] == "reviewer02"


async def test_preview_rejects_empty_or_incompatible_input(client):
    case_no = "CASE-API-PV3"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing", assignee="reviewer01")
    r = await client.post(f"{CASES}/{case_no}/dispose/preview",
                          json={"conclusion": "normal", "action_types": []}, headers=READER)
    assert r.status_code == 400 and code_of(r) == "DSP-4001"
    r = await client.post(f"{CASES}/{case_no}/dispose/preview",
                          json={"conclusion": "normal", "action_types": ["block_order"]},
                          headers=READER)
    assert r.status_code == 422 and code_of(r) == "DSP-4002"


@pytest.mark.parametrize("case", ["missing", "old", "changed_params", "reused"])
async def test_token_bypass_attempts_are_all_rejected(client, case: str):
    """V-08-07 / BR-08-27：无令牌 / 换参数 / 旧令牌 / 重复使用 → 全部 `DSP-4006`。

    `reused` 这一格刻意走"第一次处置因**名单写入失败**而中止"的路径：
    令牌在步骤 ① 就被消费，因此第二次用同一个令牌必须被拒，而案件此时**仍在
    `reviewing`**（这正是令牌一次性要防的场景：审核员刷新页面后拿旧令牌重试）。
    """
    case_no = f"CASE-API-TOKEN-{case}"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01")
    # `reused` 这一格要靠"名单写入失败"把令牌消费掉，因此必须选一个**会写名单**的动作
    action = "blacklist_user" if case == "reused" else "block_order"
    token = (await preview(client, case_no, "violation", [action]))["confirm_token"]

    if case == "missing":
        token = ""
    elif case == "old":
        # 用另一个案件的令牌冒充（签名有效、但绑定的是别的 case_no）
        other = "CASE-API-TOKEN-old-other"
        await insert_case(db.get_db(), case_no=other, status="reviewing",
                          assignee="reviewer01")
        token = (await preview(client, other, "violation", [action]))["confirm_token"]
    elif case == "changed_params":
        r = await dispose(client, case_no, "violation", ["ban_device"], token)
        assert r.status_code == 422 and code_of(r) == "DSP-4006"
        assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0
        return
    elif case == "reused":
        from app.services import disposal_service as disposal_mod
        from tests.case_testlib import FailingListService

        service = disposal_mod.get_disposal_service()
        service.configure(list_service=FailingListService(fail_at=0))
        try:
            first = await dispose(client, case_no, "violation", [action], token)
            assert first.status_code == 503 and code_of(first) == "DSP-5002"
        finally:
            service.reset_dependencies()
        second = await dispose(client, case_no, "violation", [action], token)
        assert second.status_code == 422 and code_of(second) == "DSP-4006"
        assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0
        assert await db.get_db()[COLL_LIST_ENTRIES].count_documents({}) == 0
        return

    r = await dispose(client, case_no, "violation", [action], token)
    assert r.status_code == 422, r.text
    assert code_of(r) == "DSP-4006"
    # 令牌不合法时**不执行任何副作用**
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0
    case_doc = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case_doc["status"] == "reviewing"
    assert not case_doc.get("dispose_lock"), "令牌校验失败不得留下内部锁"


# ============================================================
# 处置（核心链路的 HTTP 契约）
# ============================================================
async def test_dispose_transitions_status_and_writes_exactly_one_audit(client):
    """核心链路：状态流转 `reviewing → disposed` + **恰好一条** `case.dispose` 审计。"""
    case_no = "CASE-API-DISPOSE"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01", user_id="U000132")
    token = (await preview(client, case_no, "violation",
                           ["block_order", "blacklist_user", "ban_device"]))["confirm_token"]
    r = await dispose(client, case_no, "violation",
                      ["block_order", "blacklist_user", "ban_device"], token)
    assert r.status_code == 200, r.text
    data = body_of(r)
    assert data["status"] == "disposed"
    assert data["action_id"] and len(data["action_ids"]) == 3
    assert data["degraded"] is False
    assert data["audit"]["log_id"] and data["audit"]["hash"] and data["audit"]["prev_hash"]
    # 案件：状态 + disposed_at + 锁已释放
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "disposed" and case["disposed_at"]
    assert not case.get("dispose_lock")
    # 流水：一动作一条（BR-08-37）
    actions = await db.get_db()[COLL_CASE_ACTIONS].find({"case_no": case_no}).to_list(10)
    assert {a["action_type"] for a in actions} == {
        "block_order", "blacklist_user", "ban_device"
    }
    assert all(a["conclusion"] == "violation" for a in actions)
    assert all(a["remark"] == "确认违规，执行拦截" for a in actions)
    assert all(a["operator"] == "reviewer01" and a["operator_role"] == "reviewer"
               for a in actions)
    assert all(a["trace_id"] for a in actions), "BR-08-35：全程带 trace_id"
    # 审计**恰好一条**（D41）
    assert await _flush_audit()
    logs = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "case.dispose"}).to_list(10)
    assert len(logs) == 1
    assert logs[0]["target_id"] == case_no
    assert logs[0]["after"]["conclusion"] == "violation"
    # 名单写入的留痕在同一条审计里（BR-08-20 的默认映射：user + device 各一条）
    assert len(logs[0]["after"]["list_writes"]) == 2


async def test_dispose_ignores_operator_in_the_request_body(client):
    """V-08-17 / BR-08-07：请求体伪造 `operator` 不生效，落库一律取 JWT 主体。"""
    case_no = "CASE-API-FORGE"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01", user_id="U000133")
    token = (await preview(client, case_no, "violation", ["block_order"]))["confirm_token"]
    r = await dispose(client, case_no, "violation", ["block_order"], token,
                      extra={"operator": "admin01", "operator_role": "admin"})
    assert r.status_code == 200, r.text
    data = body_of(r)
    assert data["operator"] == "reviewer01" and data["operator_role"] == "reviewer"
    action = await db.get_db()[COLL_CASE_ACTIONS].find_one({"case_no": case_no})
    assert action["operator"] == "reviewer01" and action["operator_role"] == "reviewer"
    assert await _flush_audit()
    log = await db.get_db()[COLL_AUDIT_LOGS].find_one({"action": "case.dispose"})
    assert log["actor"] == "reviewer01"


async def test_duplicate_dispose_is_rejected_but_idempotency_key_replays(client):
    """BR-08-19 / V-08-14：再处置 → `DSP-4004`；同 `idempotency_key` → 返回首次结果。

    已处置案件要走到 `DSP-4004`，必须出示一个**有效令牌**——而 `/dispose/preview`
    对已处置案件本身就返回 `DSP-4004`（不给令牌）。因此这一格只能模拟
    "令牌签发时还在 `reviewing`、随后被别人处置掉"这一真实竞态：直接在进程内
    签发一个合法令牌，再对已处置的案件提交。
    """
    case_no = "CASE-API-IDEM"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01", user_id="U000134")
    # 第一次（带幂等键）
    token = (await preview(client, case_no, "violation", ["block_order"]))["confirm_token"]
    first = await dispose(client, case_no, "violation", ["block_order"], token,
                          idempotency_key="KEY-1")
    assert first.status_code == 200, first.text
    first_data = body_of(first)
    # 第二次：换一个幂等键 + **合法令牌**（模拟"令牌签发后被别人处置掉"）→ DSP-4004
    from app.core import confirm_token as confirm_mod

    later_token = confirm_mod.issue(case_no, "violation", ["block_order"],
                                    "reviewer01")["token"]
    again = await dispose(client, case_no, "violation", ["block_order"], later_token,
                          idempotency_key="KEY-2")
    assert again.status_code == 409, again.text
    assert code_of(again) == "DSP-4004"
    # 无令牌/坏令牌重复提交 → DSP-4006（防绕过恒优先）
    bypass = await dispose(client, case_no, "violation", ["block_order"], "whatever",
                           idempotency_key="KEY-3")
    assert bypass.status_code == 422 and code_of(bypass) == "DSP-4006"
    # 第三次：**同一个**幂等键 → 回放首次结果，且不产生新副作用
    replay = await dispose(client, case_no, "violation", ["block_order"], "whatever",
                          idempotency_key="KEY-1")
    assert replay.status_code == 200, replay.text
    replay_data = body_of(replay)
    assert replay_data["idempotent_replay"] is True
    assert replay_data["action_id"] == first_data["action_id"]
    assert replay_data["acted_at"] == first_data["acted_at"]
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({"case_no": case_no}) == 1
    assert await _flush_audit()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "case.dispose"}
    ) == 1, "幂等回放不得再写审计"


async def test_dispose_requires_matching_assignee_even_for_admin(client):
    """BR-08-06：`assignee` 不是当前用户 → `DSP-4005`；**admin 也不得代为处置**。"""
    case_no = "CASE-API-ASSIGNEE"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer02")
    r = await client.post(f"{CASES}/{case_no}/dispose/preview",
                          json={"conclusion": "normal", "action_types": ["pass"]},
                          headers=READER)
    assert r.status_code == 409 and code_of(r) == "DSP-4005"
    # admin 连 case:dispose 权限都没有（矩阵里它只属于 reviewer）→ AUTH-4020
    r = await client.post(f"{CASES}/{case_no}/dispose/preview",
                          json={"conclusion": "normal", "action_types": ["pass"]},
                          headers=ADMIN)
    assert r.status_code == 403 and code_of(r) == "AUTH-4020"


async def test_dispose_validates_remark_and_matrix(client):
    case_no = "CASE-API-VALID"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01")
    token = (await preview(client, case_no, "violation", ["block_order"]))["confirm_token"]
    r = await dispose(client, case_no, "violation", ["block_order"], token, remark="   ")
    assert r.status_code == 400 and code_of(r) == "DSP-4001"
    r = await dispose(client, case_no, "violation", ["block_order"], token, remark="x" * 501)
    assert r.status_code == 400 and code_of(r) == "DSP-4001"
    r = await dispose(client, case_no, "normal", ["block_order"], token)
    assert r.status_code == 422 and code_of(r) == "DSP-4002"
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0


async def test_dispose_404_for_missing_case(client):
    r = await dispose(client, "CASE-NOPE", "normal", ["pass"], "token")
    assert r.status_code == 404 and code_of(r) == "DSP-4040"


# ============================================================
# 权限矩阵（BR-08-36 / D26 / D28）
# ============================================================
@pytest.mark.parametrize("headers,role", [(WRITER, "strategist"), (ADMIN, "admin")])
async def test_dispose_endpoints_are_reviewer_only(client, headers, role: str):
    case_no = f"CASE-API-PERM-{role}"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01")
    for method, path, payload in (
        ("post", f"{CASES}/{case_no}/claim", None),
        ("post", f"{CASES}/{case_no}/dispose/preview",
         {"conclusion": "normal", "action_types": ["pass"]}),
        ("post", f"{CASES}/{case_no}/dispose",
         {"conclusion": "normal", "action_types": ["pass"], "remark": "x",
          "confirm_token": "t"}),
        ("post", f"{CASES}/{case_no}/biz-sync/retry", None),
    ):
        r = await getattr(client, method)(path, json=payload, headers=headers)
        assert r.status_code == 403, f"{role} 调 {path} 应为 403：{r.text}"
        assert code_of(r) == "AUTH-4020"


async def test_actions_read_uses_case_read_permission(client):
    """读用 `case:read`（矩阵里同样只有 reviewer）。"""
    case_no = "CASE-API-READ"
    await insert_case(db.get_db(), case_no=case_no, status="disposed")
    # 预置一条流水：读路径必须真的读 E09（返回 0 条与"读到了但恰好为空"无法区分）
    await insert_action(db.get_db(), case_no, action_type="blacklist_user",
                        conclusion="violation")
    r = await client.get(f"{CASES}/{case_no}/actions", headers=READER)
    assert r.status_code == 200, r.text
    data = body_of(r)
    assert data["total"] == 1
    assert data["items"][0]["action_type"] == "blacklist_user"
    assert data["items"][0]["conclusion"] == "violation"
    for headers in (WRITER, ADMIN):
        r = await client.get(f"{CASES}/{case_no}/actions", headers=headers)
        assert r.status_code == 403 and code_of(r) == "AUTH-4020"


async def test_actions_returns_the_disposal_flow_in_ascending_time_order(client):
    case_no = "CASE-API-ACTIONS"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01", user_id="U000135")
    token = (await preview(client, case_no, "violation",
                           ["block_order", "blacklist_user"]))["confirm_token"]
    await dispose(client, case_no, "violation", ["block_order", "blacklist_user"], token)
    r = await client.get(f"{CASES}/{case_no}/actions", headers=READER)
    data = body_of(r)
    assert data["total"] == 2
    assert [i["acted_at"] for i in data["items"]] == sorted(
        i["acted_at"] for i in data["items"]
    )
    assert all(i["action_label"] for i in data["items"])
    assert {i["action_type"] for i in data["items"]} == {"block_order", "blacklist_user"}


# ============================================================
# 归档（BR-08-08 / 36 / 38）
# ============================================================
async def test_archive_is_admin_only_and_requires_disposed(client):
    """`archive` 仅 admin（借用 `sys:config`，见 `case_api` 的裁定说明）。"""
    disposed = "CASE-API-ARCH1"
    await insert_case(db.get_db(), case_no=disposed, status="disposed")
    r = await client.post(f"{CASES}/{disposed}/archive", json={}, headers=READER)
    assert r.status_code == 403 and code_of(r) == "AUTH-4020"
    r = await client.post(f"{CASES}/{disposed}/archive", json={"remark": "月度归档"},
                          headers=ADMIN)
    assert r.status_code == 200, r.text
    data = body_of(r)
    assert data["status"] == "archived" and data["archived_at"]
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": disposed})
    assert case["status"] == "archived" and case["archived_at"]
    assert await _flush_audit()
    logs = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "case.archive"}).to_list(5)
    assert len(logs) == 1 and logs[0]["actor"] == "admin01"

    # 只允许 disposed → archived
    pending = "CASE-API-ARCH2"
    await insert_case(db.get_db(), case_no=pending)
    r = await client.post(f"{CASES}/{pending}/archive", json={}, headers=ADMIN)
    assert r.status_code == 409 and code_of(r) == "DSP-4003"


async def test_archive_audit_failure_rolls_back(client, monkeypatch):
    """D41：归档的审计写不进去 → 回滚归档并返回 `AUD-5001`。"""
    from app.services import case_service as case_service_mod

    case_no = "CASE-API-ARCH3"
    await insert_case(db.get_db(), case_no=case_no, status="disposed")
    monkeypatch.setattr(case_service_mod, "audit", FailingAudit("case.archive"))
    r = await client.post(f"{CASES}/{case_no}/archive", json={}, headers=ADMIN)
    assert r.status_code == 503 and code_of(r) == "AUD-5001"
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "disposed" and not case.get("archived_at")


# ============================================================
# 后台维护（BR-08-10 / 11 / 38）
# ============================================================
async def test_timeout_recycle_returns_case_to_pending_with_audit(client):
    """V-08-15 / BR-08-10：超时未处置 → 回 `pending`、清空认领、写 `case.recycle`。"""
    case_no = "CASE-API-RECYCLE"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee="reviewer01")
    await db.get_db()[COLL_RISK_CASES].update_one(
        {"_id": case_no}, {"$set": {"claim_deadline_at": now_ms() - 1000}}
    )
    result = await case_maintenance_task.run_once()
    assert result["recycled"] == 1
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "pending"
    assert not case.get("assignee") and not case.get("claimed_at")
    # BR-08-11：只写审计，**不写 case_actions**（E09 的 action_type 里没有"回收"）
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0
    assert await _flush_audit()
    logs = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "case.recycle"}).to_list(5)
    assert len(logs) == 1 and logs[0]["before"]["assignee"] == "reviewer01"
    # 回收后可以再次认领（案件回到待审队列的意义就在这里）
    r = await claim(client, case_no)
    assert r.status_code == 200 and body_of(r)["status"] == "reviewing"


async def test_claim_timeout_zero_disables_recycling(client, monkeypatch):
    """BR-08-12：阈值为 0（关闭超时回收）时 `claim_deadline_at` 为空且**永不回收**。"""
    from app import config

    monkeypatch.setattr(config, "CASE_CLAIM_TIMEOUT_MIN", 0)
    case_no = "CASE-API-NOTIMEOUT"
    await insert_case(db.get_db(), case_no=case_no)
    data = body_of(await claim(client, case_no))
    assert data["claim_deadline_at"] is None
    result = await case_maintenance_task.run_once()
    assert result["recycled"] == 0
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "reviewing" and case["assignee"] == "reviewer01"


async def test_auto_archive_after_seven_days(client):
    """BR-08-38：`disposed` 超过 7 天 → 自动归档并写审计（actor=system）。"""
    old = "CASE-API-AUTO1"
    fresh = "CASE-API-AUTO2"
    await insert_case(db.get_db(), case_no=old, status="disposed")
    await insert_case(db.get_db(), case_no=fresh, status="disposed")
    await db.get_db()[COLL_RISK_CASES].update_one(
        {"_id": old}, {"$set": {"disposed_at": now_ms() - 8 * 86_400_000}}
    )
    result = await case_maintenance_task.run_once()
    assert result["archived"] == 1
    assert (await db.get_db()[COLL_RISK_CASES].find_one({"_id": old}))["status"] == "archived"
    assert (await db.get_db()[COLL_RISK_CASES].find_one(
        {"_id": fresh}))["status"] == "disposed"
    assert await _flush_audit()
    logs = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "case.archive"}).to_list(5)
    assert len(logs) == 1 and logs[0]["actor"] == "system"


# ============================================================
# 不可撤销（BR-08-18 / V-08-14）
# ============================================================
async def test_no_undo_or_revert_route_exists(client):
    """V-08-14：路由表里**不存在** undo/revert 端点（处置不可撤销）。"""
    paths = list(app.openapi()["paths"].keys())
    bad = [p for p in paths if re.search(r"(undo|revert|rollback)", p, re.IGNORECASE)]
    assert bad == [], f"不得提供撤销类端点：{bad}"
    assert set(STATE_TRANSITIONS) == {"claim", "dispose", "archive", "recycle"}


# ============================================================
# 小工具
# ============================================================
def _rule_doc() -> dict:
    """一条 70 分的登录场景规则（60~79 → review）。"""
    moment = now_ms()
    return {
        "_id": "RLOGIN001", "name": "同用户高频登录", "scene_code": "login",
        "description": "测试用", "condition": {
            "logic": "and",
            "children": [{"field": "login_cnt_1h", "op": "gte", "value": 1}],
        },
        "score": 70, "priority": 10, "status": "enabled", "version": 1,
        "is_system": False, "deleted": False,
        "created_by": "test", "updated_by": "test",
        "created_at": moment, "updated_at": moment,
    }


class _FailingListRepo:
    """必失败的名单仓储（触发 `RUL-5001` 的降级路径）。"""

    async def find_active(self, *args, **kwargs):
        raise PyMongoError("名单库不可用")

    async def find_active_other_lists(self, *args, **kwargs):
        raise PyMongoError("名单库不可用")


async def _flush_audit() -> bool:
    from app.services import audit_service

    return await audit_service.flush()
