# -*- coding: utf-8 -*-
"""模块 12 审计日志验收测试（V-12-01 ~ V-12-16）。

V-12-13（变更摘要展开对比）属浏览器行为，由工作区的 `check_frontend_e2e.js` 验证；
其余全部在此程序化验证。

**本文件会故意篡改/删除库里的审计记录**——这是唯一能验证"篡改可被发现"的手段，
也正是 BR-12-11 只禁止**应用代码**（`app/`）写改、而测试可为之的原因。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from pymongo.errors import PyMongoError

from app import config, db
from app.constants import COLL_AUDIT_LOGS
from app.engine.audit_hash import (
    CANONICAL_FIELDS,
    GENESIS_HASH,
    canonical_payload,
    compute_hash,
)
from app.errors import AuditWriteFailedError
from app.services import audit_service
from app.services.audit_service import get_audit_service
from tests.conftest import READER

pytestmark = pytest.mark.anyio

LOGS_URL = "/api/v1/audit/logs"
VERIFY_URL = "/api/v1/audit/verify"
EXPORT_URL = "/api/v1/audit/export"
ACTORS_URL = "/api/v1/audit/actors"


def _record(i: int, **over) -> dict:
    """构造一条审计内容（默认是"规则改分值"这种带 before/after 的变更类动作）。"""
    payload = {
        "actor": "strategy01",
        "actor_role": "strategist",
        "action": "rule.update",
        "target_type": "rule",
        "target_id": f"RCOUPON{i:03d}",
        "before": {"score": 30 + i, "version": 1},
        "after": {"score": 40 + i, "version": 2},
        "strict": True,
    }
    payload.update(over)
    return payload


async def _write(n: int = 1, **over) -> list[str]:
    """连续写入 n 条（strict 模式，保证返回时已落库）。"""
    ids = []
    for i in range(n):
        log_id = await audit_service.audit(**_record(i, **over))
        ids.append(log_id)
    return ids


def _break_audit_db(monkeypatch):
    """让审计写库持续失败（模拟 Mongo 不可用），用于 strict / 非 strict 语义验证。"""
    from app.repos.audit_repo import AuditRepo

    async def boom(self, doc):
        raise PyMongoError("simulated audit db down")

    monkeypatch.setattr(AuditRepo, "insert", boom)


# ============================================================ V-12-01 变更操作留痕
async def test_change_action_records_before_and_after():
    """V-12-01 / BR-12-18：变更类动作必须携带 before 与 after。"""
    log_id = await audit_service.audit(**_record(1))
    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"_id": log_id})
    assert doc is not None
    assert doc["before"] == {"score": 31, "version": 1}
    assert doc["after"] == {"score": 41, "version": 2}
    assert doc["action"] == "rule.update"
    assert len(doc["hash"]) == 64 and doc["prev_hash"] == GENESIS_HASH


# ============================================================ V-12-02 链连续
async def test_chain_is_continuous_over_100_records():
    """V-12-02：连续写 100 条，每条 prev 等于上一条 hash，首条为创世哈希。"""
    await _write(100)
    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=200)
    assert len(rows) == 100
    assert rows[0]["prev_hash"] == GENESIS_HASH
    for prev, cur in zip(rows, rows[1:]):
        assert cur["prev_hash"] == prev["hash"], f"链在第 {cur['_id']} 处断开"
    # 哈希互不相同（内容不同 -> 哈希不同），排除"算了但都是同一个值"的假实现
    assert len({r["hash"] for r in rows}) == 100


# ============================================================ V-12-03 可复现
async def test_hash_is_reproducible_and_matches_stored():
    """V-12-03 / BR-12-03/04：同一内容算两次必须一致，且与库中存储值相同。"""
    log_id = await audit_service.audit(**_record(7))
    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"_id": log_id})

    first = compute_hash(doc["prev_hash"], doc)
    second = compute_hash(doc["prev_hash"], dict(reversed(list(doc.items()))))
    assert first == second == doc["hash"], "哈希必须与字段插入顺序无关且可复现"

    # 规范化载荷的字段顺序必须与 BR-12-03 完全一致
    payload = canonical_payload(doc)
    assert payload.split("|")[:6] == [
        str(doc["ts"]), doc["actor"], doc["actor_role"], doc["action"],
        doc["target_type"], doc["target_id"],
    ]
    assert CANONICAL_FIELDS == ("ts", "actor", "actor_role", "action", "target_type",
                               "target_id", "before", "after")


async def test_hash_changes_when_content_changes():
    """哈希必须真的覆盖内容：改 before 里的分值就必须换一个哈希。"""
    doc = {"ts": 1, "actor": "a", "actor_role": "admin", "action": "rule.update",
           "target_type": "rule", "target_id": "R1", "before": {"score": 10}, "after": None}
    h1 = compute_hash(GENESIS_HASH, doc)
    doc2 = dict(doc, before={"score": 11})
    assert compute_hash(GENESIS_HASH, doc2) != h1


# ============================================================ V-12-04 篡改可发现
async def test_tampering_is_detected_at_the_exact_record():
    """V-12-04：直接改库中某条的 after，verify 必须报 ok=false 且指向**该条**。"""
    await _write(5)
    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=10)
    victim = rows[2]

    await db.get_db()[COLL_AUDIT_LOGS].update_one(
        {"_id": victim["_id"]}, {"$set": {"after": {"score": 999, "version": 2}}}
    )

    data = await get_audit_service().verify_chain()
    assert data["ok"] is False
    assert data["broken_at"]["log_id"] == victim["_id"], "必须精确定位到被改的那条"
    assert data["broken_at"]["seq"] == 3
    assert data["broken_at"]["actual_hash"] != data["broken_at"]["expected_hash"]
    assert data["notice_code"] == "AUD-5003"

    # BR-12-13：发现篡改必须留痕（且这条本身也进链）
    await audit_service.flush()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "audit.tamper_detected"}) == 1


async def test_verify_is_not_just_counting_rows():
    """V-12-05：删掉中间一条后必须报不一致——只比对总数（或只查长度）的实现会漏判。"""
    await _write(4)
    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=10)
    await db.get_db()[COLL_AUDIT_LOGS].delete_one({"_id": rows[1]["_id"]})

    data = await get_audit_service().verify_chain()
    assert data["ok"] is False, "删除中间记录后链必然断裂"
    assert data["broken_at"]["log_id"] == rows[2]["_id"], "断点应指向缺失记录的后一条"
    assert "prev_hash" in data["broken_at"]["reason"] or data["broken_at"]["expected_prev_hash"]


async def test_ip_and_ua_are_not_covered_by_hash():
    """**钉住一个已知局限**：BR-12-03 的字段清单不含 ip/ua，故单独改它们检测不出来。

    这不是"实现缺陷"，而是 Spec 的取舍；写成用例是为了让它**不能悄悄变化**——
    若将来决定把 ip/ua 纳入哈希，这条用例会失败，从而提醒必须同步评估
    "既有记录将全部校验失败"。
    """
    log_id = await audit_service.audit(**_record(2, ip="1.1.1.1", ua="probe"))
    await db.get_db()[COLL_AUDIT_LOGS].update_one({"_id": log_id}, {"$set": {"ip": "9.9.9.9"}})
    data = await get_audit_service().verify_chain()
    assert data["ok"] is True, "按当前 Spec，ip 不在哈希覆盖范围内"


# ============================================================ V-12-06 并发不分叉
async def test_concurrent_writes_do_not_fork_the_chain():
    """V-12-06 / AD-04 / G-10：50 个并发写必须得到一条**无分叉**的链。"""
    await asyncio.gather(*[
        audit_service.audit(**_record(i, target_id=f"RACE{i:03d}")) for i in range(50)
    ])
    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=100)
    assert len(rows) == 50

    prevs = [r["prev_hash"] for r in rows]
    assert prevs[0] == GENESIS_HASH
    assert len(set(prevs)) == 50, "prev_hash 出现重复即说明有两条接到了同一个链头（分叉）"
    for prev, cur in zip(rows, rows[1:]):
        assert cur["prev_hash"] == prev["hash"]

    data = await get_audit_service().verify_chain()
    assert data["ok"] is True and data["chain_length"] == 50


# ============================================================ V-12-07/08 strict 语义
async def test_strict_write_failure_blocks_business(monkeypatch):
    """V-12-07 / AUD-5001：紧操作的审计写失败必须**抛错**，让调用方中止并回滚。"""
    _break_audit_db(monkeypatch)
    with pytest.raises(AuditWriteFailedError) as ei:
        await audit_service.audit(**_record(1))
    assert ei.value.code == "AUD-5001"
    assert ei.value.http_status == 503
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents({}) == 0


async def test_non_strict_write_failure_does_not_block(monkeypatch):
    """V-12-08 / AUD-5002：普通操作的审计写失败只告警，**不阻断**业务。"""
    _break_audit_db(monkeypatch)
    before = get_audit_service().stats["failed"]
    result = await audit_service.audit(**_record(1, action="event.receive", strict=False))
    assert result is None, "非 strict 写入不返回 log_id，也不抛错"
    assert await audit_service.flush()
    assert get_audit_service().stats["failed"] > before, "失败必须计入统计并告警"


async def test_retry_then_success(monkeypatch):
    """BR-12-08：瞬时故障应被重试吸收（前两次失败、第三次成功）。"""
    from app.repos.audit_repo import AuditRepo

    original = AuditRepo.insert
    calls = {"n": 0}

    async def flaky(self, doc):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PyMongoError("transient")
        return await original(self, doc)

    monkeypatch.setattr(AuditRepo, "insert", flaky)
    log_id = await audit_service.audit(**_record(1))
    assert log_id and calls["n"] == 3, "应在第 3 次尝试成功"


# ============================================================ V-12-09 只追加
async def test_audit_collection_is_append_only_in_app_code():
    """V-12-09 / BR-12-11：`app/` 代码里不得出现对 `audit_logs` 的改/删调用。

    这是"不可篡改"的静态守卫：接口层面的只读可以靠不写路由实现，但代码里一句
    `update_one` 就能把链改掉。测试代码（本文件）需要篡改来验证检测能力，故只扫 `app/`；
    `scripts/seed.py --reset` 是演示复现工具，不属应用路径（见交付说明）。
    """
    forbidden = re.compile(
        r"\b(update_one|update_many|delete_one|delete_many|replace_one|"
        r"find_one_and_update|find_one_and_replace|find_one_and_delete|drop)\s*\("
    )
    offenders: list[str] = []
    for path in Path(config.ROOT, "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # 只审"会碰到审计集合"的文件：引用集合名或仓储类
        if "COLL_AUDIT_LOGS" not in text and "audit_logs" not in text:
            continue
        for m in forbidden.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(config.ROOT)}:{line_no} {m.group(1)}")
    assert not offenders, f"审计集合出现写改调用（违反 BR-12-11）：{offenders}"


# ============================================================ V-12-10 越权留痕
async def test_denied_attempt_records_target_information(client):
    """V-12-10 / BR-12-19：越权记录必须带目标信息，否则看不出"他想干什么"。"""
    await client.post("/api/v1/lists", json={
        "list_type": "black", "entity_type": "device",
        "entity_value": "AUD12-PROBE", "reason": "越权",
    }, headers=READER)
    assert await audit_service.flush()

    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"action": "auth.denied"})
    assert doc is not None
    assert doc["target_type"] == "permission" and doc["target_id"] == "list:write"
    assert doc["after"]["method"] == "POST" and doc["after"]["path"] == "/api/v1/lists"


# ============================================================ V-12-11 导出自身被审计
async def test_export_is_itself_audited(client, bearer):
    """V-12-11 / BR-12-15：导出一次必须新增 `audit.export` 记录。"""
    await _write(3)
    headers = await bearer("admin")
    # 取令牌本身会写一条 auth.login，先排空再读总数，否则导出条数无法精确断言
    await audit_service.flush()
    expected = await db.get_db()[COLL_AUDIT_LOGS].count_documents({})

    r = await client.get(EXPORT_URL, params={"format": "csv"}, headers=headers)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("log_id,ts,actor"), "导出的是数据文件，不是响应包"

    assert await audit_service.flush()
    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"action": "audit.export"})
    assert doc is not None and doc["actor"] == "admin01"
    assert doc["after"]["count"] == expected, "导出的条数必须等于查询时刻的记录数"


# ============================================================ V-12-12 四维筛选
async def test_four_dimension_filters(client, bearer):
    """V-12-12：按操作人 / 动作（前缀）/ 目标类型 / 时间分别筛选。"""
    await audit_service.audit(**_record(1, actor="strategy01", action="rule.update",
                                        target_type="rule"))
    await audit_service.audit(**_record(2, actor="admin01", actor_role="admin",
                                        action="rule.toggle", target_type="rule"))
    await audit_service.audit(**_record(3, actor="reviewer01", actor_role="reviewer",
                                        action="rule.delete", target_type="rule"))
    # 故意多写一条不同目标类型，用来证明 target_type 过滤**确实排除了**它
    await audit_service.audit(**_record(4, actor="admin01", actor_role="admin",
                                        action="config.update", target_type="config"))
    await audit_service.flush()
    headers = await bearer("admin")

    r = await client.get(LOGS_URL, params={"actor": "admin01"}, headers=headers)
    items = r.json()["data"]["items"]
    assert len(items) == 2 and all(i["actor"] == "admin01" for i in items)

    r2 = await client.get(LOGS_URL, params={"action": "rule."}, headers=headers)
    items2 = r2.json()["data"]["items"]
    assert len(items2) == 3 and all(i["action"].startswith("rule.") for i in items2)

    r3 = await client.get(LOGS_URL, params={"target_type": "config"}, headers=headers)
    assert [i["action"] for i in r3.json()["data"]["items"]] == ["config.update"]

    import time

    now = int(time.time() * 1000)
    r4 = await client.get(LOGS_URL, params={"from": now + 60_000, "to": now + 120_000},
                          headers=headers)
    assert r4.json()["data"]["total"] == 0, "未来时间窗内不应有记录"

    # 分页契约（沿用模块 00 §3.2）。用 target_type 限定，避免把
    # "本用例登录时写的 auth.login" 也算进来
    data = (await client.get(LOGS_URL, params={"target_type": "rule", "page_size": 2},
                             headers=headers)).json()["data"]
    assert set(data) >= {"items", "total", "page", "page_size", "pages"}
    assert len(data["items"]) == 2 and data["total"] == 3 and data["pages"] == 2


async def test_time_span_limit(client, bearer):
    """AUD-4003：时间跨度超过 30 天必须拒绝（防全表扫描）。"""
    import time

    now = int(time.time() * 1000)
    r = await client.get(LOGS_URL, params={"from": now - 40 * 86_400_000, "to": now},
                         headers=await bearer("admin"))
    assert r.status_code == 422 and r.json()["code"] == "AUD-4003"


# ============================================================ V-12-14 权限
async def test_only_admin_can_read_audit(client, bearer):
    """V-12-14 / BR-12-24：仅 admin 可访问；其余角色 403 且**留痕**。"""
    for role in ("reviewer", "strategist"):
        r = await client.get(LOGS_URL, headers=await bearer(role))
        assert r.status_code == 403, role
        assert r.json()["code"] == "AUTH-4020", role
    assert (await client.get(LOGS_URL, headers=await bearer("admin"))).status_code == 200

    await audit_service.flush()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents({"action": "auth.denied"}) == 2


async def test_verify_requires_admin(client, bearer):
    assert (await client.get(VERIFY_URL, headers=await bearer("strategist"))).status_code == 403
    assert (await client.get(ACTORS_URL, headers=await bearer("admin"))).status_code == 200


# ============================================================ V-12-15 分页与上限
async def test_page_size_limit_and_export_cap(client, bearer):
    """BR-12-21：单页最大 100；导出超上限返回 AUD-4002。"""
    headers = await bearer("admin")
    assert (await client.get(LOGS_URL, params={"page_size": 100}, headers=headers)).status_code == 200
    r = await client.get(LOGS_URL, params={"page_size": 101}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "AUD-4001"

    await _write(3)
    # 直接调大上限阈值不可取，这里把上限改小以验证"超限即拒绝"这条逻辑本身
    import app.services.audit_service as svc

    original = svc.EXPORT_LIMIT
    try:
        svc.EXPORT_LIMIT = 2
        r2 = await client.get(EXPORT_URL, params={"format": "csv"}, headers=headers)
        assert r2.status_code == 422 and r2.json()["code"] == "AUD-4002"
    finally:
        svc.EXPORT_LIMIT = original


async def test_verify_pagination_params(client, bearer):
    """AUD-4001 + 分段校验：`from_seq` 越界由参数校验拦下；分段结果可衔接。"""
    await _write(5)
    headers = await bearer("admin")
    r = await client.get(VERIFY_URL, params={"from_seq": -1}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "COM-4001"

    part = (await client.get(VERIFY_URL, params={"from_seq": 0, "limit": 3},
                             headers=headers)).json()["data"]
    assert part["ok"] is True and part["chain_length"] == 3
    rest = (await client.get(VERIFY_URL, params={"from_seq": 3}, headers=headers)).json()["data"]
    assert rest["ok"] is True and rest["chain_length"] == 2


async def test_export_formats(client, bearer):
    """§3.3：CSV 与 Markdown 两种导出格式都要可用。"""
    await _write(2)
    headers = await bearer("admin")
    csv = await client.get(EXPORT_URL, params={"format": "csv"}, headers=headers)
    assert csv.status_code == 200 and csv.text.startswith("log_id,ts,actor")
    md = await client.get(EXPORT_URL, params={"format": "markdown"}, headers=headers)
    assert md.status_code == 200 and md.text.startswith("# 审计流水导出")
    assert "| `rule.update` |" in md.text


async def test_actors_endpoint(client, bearer):
    """§2.2：操作人下拉的数据来源，同时下发动作与目标类型选项（避免前端硬编码）。"""
    await _write(2)
    await audit_service.audit(**_record(3, actor="admin01", actor_role="admin"))
    await audit_service.flush()
    data = (await client.get(ACTORS_URL, headers=await bearer("admin"))).json()["data"]
    assert "strategy01" in data["actors"] and "admin01" in data["actors"]
    assert "rule.update" in data["actions"] and "rule" in data["target_types"]


# ============================================================ V-12-16 重启后继续
async def test_chain_continues_after_restart():
    """V-12-16 / BR-12-09：模拟重启（新建服务实例），链头必须从库中恢复。"""
    await _write(3)
    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=10)
    old_head = rows[-1]["hash"]

    # 停掉消费者 + 换一个全新的服务实例，等价于"进程重启后重新装配"
    await audit_service.stop_consumer()
    from app.services.audit_service import AuditService

    fresh = AuditService()
    log_id = await fresh.audit(**_record(9))
    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"_id": log_id})
    assert doc["prev_hash"] == old_head, "重启后必须接在原有链头之后，而不是从创世重开"

    data = await fresh.verify_chain()
    assert data["ok"] is True and data["chain_length"] == 4
    await fresh.stop()


async def test_empty_db_starts_from_genesis():
    """BR-12-09：库为空时以创世块重新开始。"""
    log_id = await audit_service.audit(**_record(1))
    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"_id": log_id})
    assert doc["prev_hash"] == GENESIS_HASH


async def test_verify_reports_head_and_length(client, bearer):
    """§3.2 响应字段齐全（前端校验卡片直接绑定这些字段）。"""
    await _write(4)
    data = (await client.get(VERIFY_URL, headers=await bearer("admin"))).json()["data"]
    assert set(data) >= {"ok", "chain_length", "genesis_hash", "head_hash",
                         "broken_at", "elapsed_ms"}
    assert data["genesis_hash"] == GENESIS_HASH
    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=10)
    assert data["head_hash"] == rows[-1]["hash"]
    assert isinstance(data["elapsed_ms"], int)
