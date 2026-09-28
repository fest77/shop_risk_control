# -*- coding: utf-8 -*-
"""旁路副作用失败时的降级与重试标记（BR-08-31 / 32，Spec 08 §6 的 `test_disposal_partial.py`。

旁路（`BizAdapter` 同步、审计落库）失败**不撤销**已生效的处置——撤销会造成
"先拦后放"的二次伤害（Spec §8「新增 2」）。但它**必须**留下可重试的标记：
`degraded=true`、`retry_hint`、`case_actions.biz_sync_result.status="failed"`、
`risk_cases.audit_pending=true`、以及重试队列里的一条待办。
"不撤销"绝不等于"不管"。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import COLL_AUDIT_LOGS, COLL_CASE_ACTIONS, COLL_RISK_CASES
from app.core import biz_sync_retry
from app.repos.case_action_repo import CaseActionRepo
from app.schemas.case_schema import DisposeIn, DisposePreviewIn
from app.services import audit_service
from app.services.disposal_service import get_disposal_service

from tests.case_testlib import REVIEWER, FailingAudit, RecordingAdapter, insert_case

pytestmark = pytest.mark.anyio

ACTIONS = ["block_order", "blacklist_user"]


async def _prepare(case_no: str, *, user_id: str = "U000132") -> None:
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee=REVIEWER, user_id=user_id)


def _dispose_in(token: str, *, remark: str = "旁路失败用例", actions: list[str] | None = None):
    return DisposeIn(conclusion="violation", action_types=list(actions or ACTIONS),
                     remark=remark, confirm_token=token)


async def _preview(service, case_no: str, actions: list[str] | None = None):
    return await service.preview(
        case_no,
        DisposePreviewIn(conclusion="violation", action_types=list(actions or ACTIONS)),
        REVIEWER,
    )


async def test_biz_adapter_failure_is_degraded_and_retryable(client):
    """V-08-11 / DSP-5003：适配器抛异常 → 处置保留 + `degraded` + 入队可重试。"""
    assert client is not None
    case_no = "CASE20260101902001"
    await _prepare(case_no)
    adapter = RecordingAdapter(fail=True)
    service = get_disposal_service()
    service.configure(adapter=adapter)
    try:
        preview = await _preview(service, case_no)
        body = await service.dispose(case_no, _dispose_in(preview["confirm_token"]),
                                     REVIEWER, actor_role="reviewer")
        # ① 处置**保留**：状态已 disposed、名单已写、流水已落
        assert body["status"] == "disposed"
        assert body["degraded"] is True
        assert body["retry_hint"] == {"biz_sync": True, "audit": False}
        assert body["notice_code"] == "DSP-5003"
        assert body["biz_sync"]["status"] == "failed"
        assert body["biz_sync"]["retryable"] is True
        case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
        assert case["status"] == "disposed" and case["biz_sync_pending"] is True
        # ② 逐条流水都记下了失败与 message
        rows = await CaseActionRepo(db.get_db()).list_by_case(case_no)
        assert len(rows) == 2
        assert all(r["biz_sync_result"]["status"] == "failed" for r in rows)
        assert all("mock 业务系统不可用" in r["biz_sync_result"]["message"] for r in rows)
        # ③ 重试队列里各一条
        assert biz_sync_retry.get_queue().size() == 2
        assert {p["case_no"] for p in biz_sync_retry.get_queue().peek()} == {case_no}
    finally:
        service.reset_dependencies()


async def test_biz_sync_retry_updates_status_and_increments_attempt(client):
    """V-08-11 的后半段：重试可把 `failed` 改成 `ok`，`attempt_no` 递增。"""
    assert client is not None
    case_no = "CASE20260101902002"
    await _prepare(case_no)
    failing = RecordingAdapter(fail=True)
    service = get_disposal_service()
    service.configure(adapter=failing)
    try:
        preview = await _preview(service, case_no)
        await service.dispose(case_no, _dispose_in(preview["confirm_token"]),
                              REVIEWER, actor_role="reviewer")
    finally:
        service.reset_dependencies()

    working = RecordingAdapter(fail=False)
    service = get_disposal_service()
    service.configure(adapter=working)
    try:
        repo = CaseActionRepo(db.get_db())
        rows = await repo.list_by_case(case_no)
        target = rows[0]
        body = await service.retry_biz_sync(case_no, str(target["_id"]), REVIEWER,
                                           actor_role="reviewer")
        assert body["attempt_no"] == 1
        assert body["biz_sync"]["status"] == "ok"
        assert "notice_code" not in body, "全部重试成功时不应再报 DSP-5003"
        fresh = await repo.find_by_id(str(target["_id"]))
        assert fresh["biz_sync_result"]["status"] == "ok"
        assert fresh["biz_sync_result"]["attempt_no"] == 1
        # 每次重试**追加**一条审计，不覆盖原记录
        assert await audit_service.flush()
        logs = await db.get_db()[COLL_AUDIT_LOGS].find(
            {"action": "case.biz_sync.retry"}
        ).to_list(length=10)
        assert len(logs) == 1 and logs[0]["target_id"] == str(target["_id"])
        # 该案件另一条仍失败 → 案件上的待重试标记保持 true
        assert (await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no}))[
            "biz_sync_pending"
        ] is True
    finally:
        service.reset_dependencies()


async def test_biz_sync_retry_on_un_disposed_case_is_rejected(client):
    """`DSP-4003`：案件还没处置时没有可重试的联动记录。"""
    assert client is not None
    case_no = "CASE20260101902003"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee=REVIEWER)
    service = get_disposal_service()
    with pytest.raises(Exception) as ei:
        await service.retry_biz_sync(case_no, None, REVIEWER)
    assert ei.value.code == "DSP-4003" and ei.value.http_status == 409


async def test_audit_failure_keeps_disposal_and_marks_audit_pending(client, monkeypatch):
    """`DSP-5004` / BR-08-32：审计写不进去**不撤销**处置，只标记待重试。"""
    assert client is not None
    case_no = "CASE20260101902004"
    await _prepare(case_no, user_id="U000133")
    fake = FailingAudit()
    monkeypatch.setattr(audit_service, "audit", fake)
    from app.services import disposal_service as disposal_mod

    monkeypatch.setattr(disposal_mod, "audit", fake)
    service = get_disposal_service()
    try:
        preview = await _preview(service, case_no, actions=["block_order"])
        body = await service.dispose(
            case_no, _dispose_in(preview["confirm_token"], actions=["block_order"]),
            REVIEWER, actor_role="reviewer",
        )
        assert body["status"] == "disposed", "审计失败不得撤销已生效的处置（BR-08-32）"
        assert body["degraded"] is True
        assert body["retry_hint"] == {"biz_sync": False, "audit": True}
        assert body["notice_code"] == "DSP-5004"
        assert body["audit"] is None
        case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
        assert case["status"] == "disposed" and case["audit_pending"] is True
        assert await db.get_db()[COLL_CASE_ACTIONS].count_documents(
            {"case_no": case_no}
        ) == 1
        audit_pending = [p for p in biz_sync_retry.get_queue().peek(case_no=case_no)
                         if p["kind"] == biz_sync_retry.KIND_AUDIT]
        assert audit_pending, "审计待重试项必须入队（BR-08-32 ②）"
    finally:
        service.reset_dependencies()


async def test_successful_disposal_has_no_degradation(client):
    """对照组：全部成功时 `degraded=false`、两个 `retry_hint` 都是 false。"""
    assert client is not None
    case_no = "CASE20260101902005"
    await _prepare(case_no, user_id="U000135")
    service = get_disposal_service()
    preview = await _preview(service, case_no, actions=["block_order"])
    body = await service.dispose(
        case_no, _dispose_in(preview["confirm_token"], actions=["block_order"]),
        REVIEWER, actor_role="reviewer",
    )
    assert body["degraded"] is False
    assert body["retry_hint"] == {"biz_sync": False, "audit": False}
    assert body["notice_code"] is None
    assert body["biz_sync"]["target"] == "mock", "默认装配是 MockBizAdapter（AD-08）"
    assert body["biz_sync"]["message"] == "no real biz system", (
        "返回值不具权威性，message 必须明说没有真实业务系统"
    )
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["biz_sync_pending"] is False and case["audit_pending"] is False
    assert biz_sync_retry.get_queue().size() == 0
