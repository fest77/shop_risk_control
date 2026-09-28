# -*- coding: utf-8 -*-
"""模块 08 测试的公共助手：案件文档构造、必失败的依赖、可断言的假适配器。

## 为什么造案件用"直接插文档"而不是走一遍决策链路

案件是 05 落库后**异步**建出来的（AD-01 + D5）。要验证"认领/处置"的行为，
把每个用例都变成"先灌画像 → 灌规则 → 造事件 → 等决策 → 等建案"会让
一个 30 行的用例膨胀到 200 行，且它们会因为与案件无关的原因（规则分值、
特征窗口）而随机失败。

**真实链路的建案**由 `test_case_api.py::test_review_decision_creates_case`
单独钉住（它驱动的就是真实的 05 → 08 路径）。本文件只负责"造出合法的
前置状态"——这与 `profile_testlib` 造 E10~E14 是同一取舍。

## `state_after_*` 的取值必须与 `case_state` 一致

`make_case(status=...)` 会顺手把该状态应有的字段（`assignee`/`claimed_at`/
`disposed_at`）补齐。**故意不提供"造一个不可能的状态组合"的开关**：
那种数据在真实的 08 写入路径下不可能出现（每个迁移都是条件更新），
用它写出的用例只会验证一堆永远不会发生的情形。
"""
from __future__ import annotations

from typing import Any, Optional

from pymongo.errors import PyMongoError

from app.constants import COLL_CASE_ACTIONS, COLL_RISK_CASES, COLL_RISK_EVENTS
from app.enums import CaseStatus, Conclusion
from app.protocols import BizSyncResult
from app.utils.timeutil import now_ms

#: 测试用的固定编号（与 `scripts/seed.py` 的演示用户对齐，便于人工对照）。
#: 注意：`make_case` 的 `event_id` / `decision_id` **默认由案件编号派生**，
#: 不共用这两个常量（E08 的 `event_id` 上有唯一索引，一个用例里造两个案件
#: 共用编号会撞键——那不是被测代码的缺陷，而是夹具在造不可能的状态）。
DEMO_USER = "U000132"
DEMO_DEVICE = "DMULE0001"
DEMO_EVENT = "EVT20260101900000000001"
DEMO_DECISION = "DEC20260101900000000001"
REVIEWER = "reviewer01"
OTHER_REVIEWER = "reviewer02"


def make_case(
    *,
    case_no: str,
    status: str = CaseStatus.PENDING.value,
    user_id: str = DEMO_USER,
    assignee: Optional[str] = None,
    event_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    risk_score: int = 70,
    risk_level: str = "medium",
    decision: str = "review",
    degraded: bool = False,
) -> dict:
    """拼一个合法的案件文档（纯函数，便于单测与复核）。

    `event_id` / `decision_id` **默认由案件编号派生**（`EVT-{case_no}`），
    而不是共用一个常量：E08 的 `event_id` 上有唯一索引（BR-08-02 的幂等落点），
    一个用例里插两个案件时共用一个编号会撞键——那不是被测代码的缺陷，
    而是夹具在制造一个真实系统里不可能出现的状态。
    """
    moment = now_ms()
    event_id = event_id or f"EVT-{case_no}"
    decision_id = decision_id or f"DEC-{case_no}"
    if status == CaseStatus.REVIEWING.value and assignee is None:
        assignee = REVIEWER
    claim_deadline_at = moment + 30 * 60_000 if status == CaseStatus.REVIEWING.value else None
    doc = {
        "_id": case_no,
        "event_id": event_id,
        "decision_id": decision_id,
        "user_id": user_id,
        "scene_code": "login",
        "risk_score": risk_score,
        "risk_level": risk_level,
        "decision": decision,
        "degraded": degraded,
        "degrade_code": None,
        "risk_tags": ["device_cluster"],
        "status": status,
        "assignee": assignee if status == CaseStatus.REVIEWING.value else None,
        "claimed_at": moment if status == CaseStatus.REVIEWING.value else None,
        "claim_deadline_at": claim_deadline_at,
        "disposed_at": moment if status in (CaseStatus.DISPOSED.value,
                                            CaseStatus.ARCHIVED.value) else None,
        "archived_at": moment if status == CaseStatus.ARCHIVED.value else None,
        "audit_pending": False,
        "biz_sync_pending": False,
        "created_at": moment,
    }
    return doc


async def insert_case(db: Any, *, device_id: Optional[str] = DEMO_DEVICE,
                      amount: int = 19800, **case_fields: Any) -> dict:
    """插入案件并**保证对应的 E01 事件存在**（处置编排要读它的 `device_id`）。

    `device_id` / `amount` 是"事件侧"的输入（BR-08-23 的跳过分支、BR-08-03 的
    `estimated_loss`），因此它们**不**进 E08 文档的构造：`make_case` 的参数
    与 E08 的字段一一对应，两类输入分开写，用例里就不会出现
    "以为在造案件、其实在造事件"的误会。
    """
    doc = make_case(**case_fields)
    event = {
        "_id": doc["event_id"],
        "event_type": "login",
        "user_id": doc["user_id"],
        "device_id": device_id,
        "amount": int(amount),
        "ts": doc["created_at"],
        "received_at": doc["created_at"],
        "source": "manual_sim",
    }
    await db[COLL_RISK_EVENTS].replace_one({"_id": event["_id"]}, event, upsert=True)
    await db[COLL_RISK_CASES].insert_one(doc)
    return doc


async def insert_action(db: Any, case_no: str, *, action_type: str = "block_order",
                        conclusion: str = Conclusion.VIOLATION.value,
                        operator: str = REVIEWER, acted_at: Optional[int] = None,
                        biz_status: str = "ok") -> dict:
    """插入一条处置流水（供"已处置"案件的读路径用例使用）。"""
    moment = acted_at if acted_at is not None else now_ms()
    doc = {
        "_id": f"ACTTEST{case_no[-6:]}{moment % 100000}",
        "case_no": case_no,
        "action_type": action_type,
        "action_types": [action_type],
        "conclusion": conclusion,
        "remark": "测试预置的处置流水",
        "evidence_refs": [],
        "list_writes": [],
        "operator": operator,
        "operator_role": "reviewer",
        "acted_at": moment,
        "biz_sync_result": {"target": "mock", "status": biz_status,
                            "message": "no real biz system", "retryable": False,
                            "attempt_no": 0},
    }
    await db[COLL_CASE_ACTIONS].insert_one(doc)
    return doc


# ============================================================
# 可注入的假依赖
# ============================================================
class FailingListService:
    """名单写入必失败的名单服务（验证 BR-08-30 的 fail-closed）。

    只实现 08 会用到的两个方法：`add_auto`（必抛）与 `rollback_auto`（记账）。
    `rollback_auto` 记账是为了断言"回滚被调用过几次"——只断言"案件仍是
    reviewing"是不够的：那可能是因为**根本还没写名单**就失败了。
    """

    def __init__(self, *, fail_at: int = 0, error: Optional[Exception] = None):
        self.fail_at = int(fail_at)
        self.calls: list[dict] = []
        self.rollbacks: list[str] = []
        self.error = error or PyMongoError("list write blew up")

    async def add_auto(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if len(self.calls) - 1 >= self.fail_at:
            raise self.error
        return {
            "entry_id": f"LTEST{len(self.calls):03d}",
            "list_type": kwargs.get("list_type"),
            "entity_type": kwargs.get("entity_type"),
            "entity_value": kwargs.get("entity_value"),
            "reused": False,
            "effective_at": now_ms(),
        }

    async def rollback_auto(self, entry_id: str, operator: str = "system") -> bool:
        del operator
        self.rollbacks.append(entry_id)
        return True


class FailingAudit:
    """让审计写入必失败的替身（验证 BR-08-32 与 D41 的两条不同路径）。

    它替换的是 `app.services.audit_service.audit` —— 也就是说，业务侧**仍然**
    走真实的调用点与真实的错误码（`AUD-5001`），只是落库那一步必然失败。
    比 monkeypatch 掉 `AuditRepo.insert` 更贴近真实故障（真实故障发生在
    驱动层），也不会因为仓储内部实现变化而失去意义。
    """

    def __init__(self, action: str = "case.dispose"):
        self.action = action
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        from app.errors import AuditWriteFailedError

        raise AuditWriteFailedError(
            str(kwargs.get("action") or self.action), "测试注入的审计故障"
        )


class RecordingAdapter:
    """记录调用并可按需失败的 `BizAdapter`（验证 BR-08-31 / DSP-5003）。"""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def sync(self, action: str, payload: dict) -> BizSyncResult:
        self.calls.append((action, dict(payload)))
        if self.fail:
            raise RuntimeError("mock 业务系统不可用")
        return BizSyncResult(target="mock", status="ok",
                             message="no real biz system", retryable=False)


__all__ = [
    "DEMO_DECISION",
    "DEMO_DEVICE",
    "DEMO_EVENT",
    "DEMO_USER",
    "OTHER_REVIEWER",
    "REVIEWER",
    "FailingAudit",
    "FailingListService",
    "RecordingAdapter",
    "insert_action",
    "insert_case",
    "make_case",
]
