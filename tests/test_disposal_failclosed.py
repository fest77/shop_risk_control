# -*- coding: utf-8 -*-
"""紧操作 fail-closed：名单写入失败必须回滚且**不置已处置**
（BR-08-30 / `DSP-5002` / `DSP-5001` / `DSP-5005`，Spec 08 §6 的 `test_disposal_failclosed.py`）。

这是本模块最重要的一组断言。它的反面（"接口返回成功、黑名单其实没生效"）
是风控系统里最危险的一类缺陷：页面显示已拦截，而订单照常放行。
因此这里不只断言错误码，还逐条钉住**库里的状态**：
案件仍是 `reviewing`、没有成功态流水、写入的名单条目被置回 `removed`。
"""
from __future__ import annotations

import pytest
from pymongo.errors import PyMongoError

from app import db
from app.constants import COLL_CASE_ACTIONS, COLL_LIST_ENTRIES, COLL_RISK_CASES
from app.core import confirm_token
from app.repos.list_repo import ListRepo
from app.repos.case_repo import CaseRepo
from app.schemas.case_schema import DisposeIn, DisposePreviewIn
from app.services.disposal_service import get_disposal_service
from app.services.list_service import ListService

from tests.case_testlib import REVIEWER, FailingListService, insert_case

pytestmark = pytest.mark.anyio

ACTIONS = ["blacklist_user", "ban_device"]


async def _prepare(case_no: str, *, user_id: str = "U000132") -> None:
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee=REVIEWER, user_id=user_id)


def _dispose_in(actions: list[str], token: str, remark: str = "fail-closed 用例"):
    return DisposeIn(conclusion="violation", action_types=actions, remark=remark,
                     confirm_token=token)


async def test_first_list_write_fails_nothing_is_written(client):
    """第 1 条名单就失败：没有条目被写入，也没有条目需要回滚。"""
    assert client is not None
    case_no = "CASE20260101901001"
    await _prepare(case_no)
    fake = FailingListService(fail_at=0)
    service = get_disposal_service()
    service.configure(list_service=fake)
    try:
        preview = await service.preview(
            case_no, DisposePreviewIn(conclusion="violation", action_types=ACTIONS),
            REVIEWER,
        )
        with pytest.raises(Exception) as ei:
            await service.dispose(case_no, _dispose_in(ACTIONS, preview["confirm_token"]),
                                  REVIEWER, actor_role="reviewer")
        exc = ei.value
        assert exc.code == "DSP-5002" and exc.http_status == 503
        assert exc.data["rolled_back"] is True
        assert fake.rollbacks == [], "没有任何条目写入，就不该有回滚动作"
        await _assert_still_reviewing(case_no)
    finally:
        service.reset_dependencies()


async def test_second_list_write_fails_rolls_back_the_first(client):
    """V-08-10：第 2 条名单失败 → **第 1 条被回滚**，案件保持 reviewing。"""
    assert client is not None
    case_no = "CASE20260101901002"
    await _prepare(case_no)
    fake = FailingListService(fail_at=1)
    service = get_disposal_service()
    service.configure(list_service=fake)
    try:
        preview = await service.preview(
            case_no, DisposePreviewIn(conclusion="violation", action_types=ACTIONS),
            REVIEWER,
        )
        with pytest.raises(Exception) as ei:
            await service.dispose(case_no, _dispose_in(ACTIONS, preview["confirm_token"]),
                                  REVIEWER, actor_role="reviewer")
        assert ei.value.code == "DSP-5002"
        assert len(fake.calls) == 2, "第二条被尝试过"
        assert fake.rollbacks == ["LTEST001"], "已写入的第一条必须被回滚"
        await _assert_still_reviewing(case_no)
        # 内部锁必须被释放：否则审核员永远拿回 DSP-4003「正在被处置」
        case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
        assert not case.get("dispose_lock"), "失败的处置必须释放内部锁"
    finally:
        service.reset_dependencies()


async def test_real_list_write_failure_rolls_back_the_inserted_entry(client):
    """用**真实**的名单服务验证回滚：第二条写入撞 Mongo 故障 → 第一条被软删。

    与上面两个用例的分工：上面验"编排顺序与回滚调用"（假服务可计数），
    这里验"回滚真的把库里那条置成了 `removed`"（真实服务 + 真实 Mongo）。
    两者缺一：只有假服务时，`rollback_auto` 可以是个空实现也照样"通过"。
    """
    assert client is not None
    case_no = "CASE20260101901003"
    await _prepare(case_no, user_id="U000137")

    class SecondWriteFails(ListService):
        def __init__(self, repo):
            super().__init__(repo)
            self.written: list[str] = []

        async def add_auto(self, **kwargs):
            if self.written:
                raise PyMongoError("模拟第二条写入时数据库故障")
            result = await super().add_auto(**kwargs)
            self.written.append(result["entry_id"])
            return result

    service = get_disposal_service()
    service.configure(list_service=SecondWriteFails(ListRepo(db.get_db())))
    try:
        preview = await service.preview(
            case_no, DisposePreviewIn(conclusion="violation", action_types=ACTIONS),
            REVIEWER,
        )
        with pytest.raises(Exception) as ei:
            await service.dispose(case_no, _dispose_in(ACTIONS, preview["confirm_token"]),
                                  REVIEWER, actor_role="reviewer")
        assert ei.value.code == "DSP-5002"
        rows = await db.get_db()[COLL_LIST_ENTRIES].find(
            {"related_case_no": case_no}
        ).to_list(length=10)
        assert len(rows) == 1
        assert rows[0]["status"] == "removed", "BR-08-30 要求按 entry_id 置 removed"
        await _assert_still_reviewing(case_no)
    finally:
        service.reset_dependencies()


async def test_case_status_write_failure_rolls_back_lists_and_actions(client, monkeypatch):
    """`DSP-5001`：案件状态写不进去 → 名单回滚、流水删除、案件保持 reviewing。"""
    assert client is not None
    case_no = "CASE20260101901004"
    await _prepare(case_no)
    service = get_disposal_service()

    async def boom(*args, **kwargs):
        raise PyMongoError("案件集合不可用")

    monkeypatch.setattr(CaseRepo, "mark_disposed", boom)
    preview = await service.preview(
        case_no, DisposePreviewIn(conclusion="violation", action_types=ACTIONS), REVIEWER
    )
    with pytest.raises(Exception) as ei:
        await service.dispose(case_no, _dispose_in(ACTIONS, preview["confirm_token"]),
                              REVIEWER, actor_role="reviewer")
    assert ei.value.code == "DSP-5001" and ei.value.http_status == 500
    await _assert_still_reviewing(case_no)
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0, (
        "本次未生效，不得留下处置流水"
    )
    rows = await db.get_db()[COLL_LIST_ENTRIES].find({"related_case_no": case_no}).to_list(10)
    assert rows and all(r["status"] == "removed" for r in rows)


async def test_case_action_write_failure_rolls_back_lists(client, monkeypatch):
    """`DSP-5005`：处置流水落库失败 → 名单回滚、案件保持 reviewing。"""
    assert client is not None
    case_no = "CASE20260101901005"
    await _prepare(case_no)
    service = get_disposal_service()

    from app.repos.case_action_repo import CaseActionRepo

    async def boom(self, docs):
        raise PyMongoError("流水集合不可用")

    monkeypatch.setattr(CaseActionRepo, "insert_many", boom)
    preview = await service.preview(
        case_no, DisposePreviewIn(conclusion="violation", action_types=ACTIONS), REVIEWER
    )
    with pytest.raises(Exception) as ei:
        await service.dispose(case_no, _dispose_in(ACTIONS, preview["confirm_token"]),
                              REVIEWER, actor_role="reviewer")
    assert ei.value.code == "DSP-5005" and ei.value.http_status == 500
    await _assert_still_reviewing(case_no)
    assert await db.get_db()[COLL_CASE_ACTIONS].count_documents({}) == 0
    rows = await db.get_db()[COLL_LIST_ENTRIES].find({"related_case_no": case_no}).to_list(10)
    assert rows and all(r["status"] == "removed" for r in rows)


async def test_skip_mapping_leaves_exactly_one_write_to_fail(client):
    """BR-08-23 与 fail-closed 的交叉格：设备缺失时**只有 1 次**名单写入。

    这条把两个要求钉在一起：跳过的映射不能"退化成写空值"（那会是第 2 次写入，
    本用例的 `fail_at=1` 就会命中），也不能因为"少写一条"而少回滚（这里
    第 1 条失败、无需回滚，`rollbacks` 必须为空——空回滚列表不是缺陷，
    而是"确实没有已生效的写入"的证据）。
    """
    assert client is not None
    case_no = "CASE20260101901006"
    await insert_case(db.get_db(), case_no=case_no, status="reviewing",
                      assignee=REVIEWER, user_id="U000134", device_id=None)
    await db.get_db()["risk_events"].update_one(
        {"_id": "EVT20260101900000000001"}, {"$unset": {"device_id": ""}}
    )
    fake = FailingListService(fail_at=1)
    service = get_disposal_service()
    service.configure(list_service=fake)
    try:
        preview = await service.preview(
            case_no, DisposePreviewIn(conclusion="violation", action_types=ACTIONS),
            REVIEWER,
        )
        body = await service.dispose(
            case_no, _dispose_in(ACTIONS, preview["confirm_token"]),
            REVIEWER, actor_role="reviewer",
        )
        assert len(fake.calls) == 1, "device_id 缺失时只应写入 user 那一条"
        assert fake.calls[0]["entity_type"] == "user"
        assert [w["entity_type"] for w in body["list_writes"]] == ["user"]
        assert body["list_writes_skipped"], "跳过的原因必须回传"
    finally:
        service.reset_dependencies()


# ============================================================ 小工具
async def _assert_still_reviewing(case_no: str) -> None:
    """失败后的三条硬约束：状态 / 处置时间 / 认领信息。"""
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": case_no})
    assert case["status"] == "reviewing", "紧操作失败后案件必须保持『审核中』"
    assert not case.get("disposed_at"), "未生效的处置不得写 disposed_at"
    assert case.get("assignee") == REVIEWER, "认领信息必须保留（审核员可以重试）"
    confirm_token.reset_store()
