# -*- coding: utf-8 -*-
"""模块 06-B「规则配置」验收测试：CRUD / 版本递增 / 乐观锁 / 软删 / 幂等 / 审计。

覆盖 Spec：`06_规则与名单配置管理.md` §3.1 规则接口、§3.4 审计动作、§3.5 列表契约、
BR-06-01~13 / 34 / 36、§5.1 错误码，以及 V-06-01 / 02 / 06 / 07 / 08 / 16 / 18 / 19 / 20。

**所有断言都落在真实行为上**（查库文档、真实 `audit_logs`、真实响应码），
不使用"调用过某个函数"这类空壳断言——空壳断言在实现被删掉后依然是绿的。
唯一例外是 `invalidate_rule_cache` 的调用计数：它的**契约本身**就是"写路径
必须调用失效点"（BR-06-13），所以必须用可替换的桩来钉住"每条写路径各调一次"。

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_rule_crud.py -q
"""
from __future__ import annotations

import pytest

from app import constants, db
from app.errors import AuditWriteFailedError
from app.services import rule_service
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

RULES_URL = "/api/v1/rules"
RULES_COLL = constants.COLL_RULES
AUDIT_COLL = constants.COLL_AUDIT_LOGS
SCENES_COLL = constants.COLL_RULE_SCENES
DAY_MS = 24 * 3600 * 1000

#: 测试用的场景字典（与 `scripts/seed.py` 的 `SEED_SCENES` 同形）。
#: 为什么测试要自己灌：`conftest.prep_db` 每个用例都会清空 `rule_scenes`
#: （它是决策的输入，残留会让别的用例的断言随机漂移），因此本模块的用例
#: 必须自己准备字典——顺便也就证明了 BR-06-07 的校验真的是"查字典"。
SCENES: list[dict] = [
    {"_id": "login", "name": "登录", "event_types": ["login"], "sort": 10},
    {"_id": "coupon", "name": "领券", "event_types": ["coupon_receive"], "sort": 20},
    {"_id": "order", "name": "下单", "event_types": ["order_create"], "sort": 30},
    {"_id": "common", "name": "通用", "event_types": ["login"], "sort": 90},
]

#: 一棵合法且真实可求值的条件树（字段取自 E02 的 18 项特征键）
TREE = {"logic": "and", "children": [
    {"field": "device_user_cnt", "op": "gte", "value": 5},
    {"logic": "or", "children": [
        {"field": "ip_is_proxy", "op": "eq", "value": True},
        {"field": "user_age_days", "op": "lt", "value": 3},
    ]},
]}


# ================================================================ 夹具
@pytest.fixture(autouse=True)
async def seed_scenes():
    """每个用例灌场景字典 + 复位幂等缓存与失效计数。"""
    col = db.get_db()[SCENES_COLL]
    for row in SCENES:
        await col.replace_one({"_id": row["_id"]}, dict(row), upsert=True)
    rule_service.reset_create_idempotency()
    rule_service.RULE_CACHE_FLUSH.reset()
    yield


def body_of(resp) -> dict:
    """统一响应包结构断言，返回整个包（与既有 06-A 用例同一口径）。"""
    b = resp.json()
    assert set(b.keys()) == {"ok", "code", "message", "trace_id", "data"}, b
    assert b["ok"] is (b["code"] == "OK"), "ok 必须由 code 派生，不允许两处状态打架"
    assert isinstance(b["trace_id"], str) and b["trace_id"]
    return b


def data_of(resp) -> dict:
    return body_of(resp)["data"]


async def post_rule(client, headers=WRITER, **over):
    payload = {
        "name": "同设备聚集登录",
        "scene_code": "login",
        "description": "同一设备指纹下关联 5 个以上账号",
        "condition": TREE,
        "score": 45,
        "priority": 10,
    }
    payload.update(over)
    return await client.post(RULES_URL, json=payload, headers=headers)


async def create_rule(client, headers=WRITER, **over) -> dict:
    r = await post_rule(client, headers=headers, **over)
    assert r.status_code == 201, r.text
    return data_of(r)


async def read_rule(rule_code: str) -> dict:
    doc = await db.get_db()[RULES_COLL].find_one({"_id": rule_code})
    assert doc is not None, f"规则 {rule_code} 不存在"
    return doc


async def audit_rows(action: str, target_id: str | None = None) -> list[dict]:
    flt: dict = {"action": action}
    if target_id is not None:
        flt["target_id"] = target_id
    cursor = db.get_db()[AUDIT_COLL].find(flt)
    return await cursor.to_list(length=1000)


async def insert_raw_rule(rule_code: str, scene: str = "login", **over) -> dict:
    """直接落一条规则文档（用于造"种子内置规则"与"已占用编码"这类前置状态）。"""
    ts = now_ms()
    doc = {
        "_id": rule_code, "name": f"内置规则 {rule_code}", "scene_code": scene,
        "description": "由种子灌入的内置规则", "condition": TREE, "score": 45,
        "priority": 10, "status": "enabled", "version": 1, "is_system": True,
        "created_by": "system", "updated_by": "system", "created_at": ts,
        "updated_at": ts,
    }
    doc.update(over)
    await db.get_db()[RULES_COLL].insert_one(doc)
    return doc


# ================================================================ 1 新建
async def test_create_generates_code_and_defaults(client):
    """BR-06-01 / 04：编码服务端生成、version=1、默认停用、操作人取自令牌身份。"""
    got = await create_rule(client)
    assert got["_id"] == "RLOGIN001", "编码格式必须是 R{场景码大写}{3位序号}"
    assert got["version"] == 1
    assert got["status"] == "disabled", "BR-06-04：新建默认停用（上线前先仿真）"
    assert got["is_system"] is False, "BR-06-37：内置规则由种子初始化，页面不能造"
    assert got["scene_name"] == "登录", "scene_name 必须来自 E06 字典（D24 数据驱动）"
    assert got["created_by"] == "strategy01"
    assert got["created_at"] and got["updated_at"]
    assert "condition_summary" in got and got["condition_summary"]

    doc = await read_rule("RLOGIN001")
    assert doc["condition"] == TREE, "落库的必须是 05 归一化后的树"
    assert doc["deleted"] is False


async def test_condition_summary_is_human_readable(client):
    """§2.2.2「条件摘要」列：AND/OR 嵌套要渲染成人读文本。"""
    got = await create_rule(client)
    summary = got["condition_summary"]
    assert "device_user_cnt" in summary and "≥" in summary and "5" in summary
    assert "AND" in summary and "OR" in summary, summary


async def test_code_sequence_increments_and_never_reuses_deleted(client):
    """BR-06-01：同场景序号递增，且**不复用**已删除的编码。"""
    first = await create_rule(client, name="规则一")
    second = await create_rule(client, name="规则二")
    assert (first["_id"], second["_id"]) == ("RLOGIN001", "RLOGIN002")

    r = await client.delete(f"{RULES_URL}/{first['_id']}", headers=WRITER)
    assert r.status_code == 200, r.text

    third = await create_rule(client, name="规则三")
    assert third["_id"] == "RLOGIN003", "已删除的编码（RLOGIN001）绝不能被复用"


async def test_code_conflict_regenerates_sequence(client, monkeypatch):
    """BR-06-01 / CFG-4002：序号被抢占时**重算序号**，重试耗尽才报冲突。

    把"取最大序号"打桩成常数 0，模拟"服务端算出的序号被别的请求抢先占用"
    （并发新建时的真实形态）。这不是在测 mock：被打桩的只是**读序号**这一步，
    被测的是重试循环是否真的会往前走——否则第三条规则永远建不出来。
    """
    from app.repos.rule_repo import RuleAdminRepo

    await insert_raw_rule("RLOGIN001")
    await insert_raw_rule("RLOGIN002")
    monkeypatch.setattr(RuleAdminRepo, "max_seq", _zero_max_seq)

    got = await create_rule(client, name="抢号重试")
    assert got["_id"] == "RLOGIN003", "撞主键后必须重算序号，而不是直接失败"


async def test_code_conflict_reports_after_exhausting_retries(client, monkeypatch):
    """重试次数耗尽 → `409 CFG-4002`（BR-06-01 的"提示重试保存"）。"""
    from app.repos.rule_repo import RuleAdminRepo

    for seq in range(1, 6):
        await insert_raw_rule(f"RLOGIN{seq:03d}")
    monkeypatch.setattr(RuleAdminRepo, "max_seq", _zero_max_seq)

    r = await post_rule(client)
    assert r.status_code == 409, r.text
    b = body_of(r)
    assert b["code"] == "CFG-4002"
    assert b["data"]["tried"] == 5
    assert await db.get_db()[RULES_COLL].count_documents({}) == 5, "失败的写入不得留下残留"


async def _zero_max_seq(self, scene_code: str) -> int:  # noqa: ARG001 - 打桩用
    return 0


async def test_score_out_of_range_rejected(client):
    """V-06-06 / BR-06-06：score=101 与 -1 均 400 CFG-4004，且**不落库**。"""
    for bad in (101, -1, 1000):
        r = await post_rule(client, score=bad)
        assert r.status_code == 400, f"score={bad} 应被拒绝，实际 {r.status_code}"
        assert body_of(r)["code"] == "CFG-4004"
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0


async def test_score_bool_is_rejected(client):
    """`score: true` 不能因为 `isinstance(True, int)` 被当成 1 分写进库。

    它走的是**模型层**拒绝（`COM-4001` 422）：布尔值不是"整数值越界"
    （`CFG-4004` 的语义是"整数不在 0~100"），而是字段类型不对。
    """
    r = await post_rule(client, score=True)
    assert r.status_code == 422 and body_of(r)["code"] == "COM-4001"
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0


async def test_score_boundary_accepted(client):
    for score in (0, 100):
        got = await create_rule(client, name=f"边界 {score}", score=score)
        assert got["score"] == score


async def test_unknown_scene_rejected(client):
    """BR-06-07：`scene_code` 必须在 `rule_scenes` 里存在（数据驱动，不硬编码）。"""
    r = await post_rule(client, scene_code="not_a_scene")
    assert r.status_code == 400
    b = body_of(r)
    assert b["code"] == "CFG-4013"
    assert "login" in b["data"]["known_scenes"], "报错要带上可选场景，用户才知道怎么改"
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0


async def test_common_scene_is_just_a_data_row(client):
    """D10/D24：`common` 不特殊对待——它在字典里就合法。"""
    got = await create_rule(client, scene_code="common", name="通用规则")
    assert got["_id"] == "RCOMMON001"
    assert got["scene_name"] == "通用"


async def test_immutable_fields_rejected_on_create(client):
    """CFG-4007：客户端提交 `is_system` / `_id` / `version` 一律拒绝。"""
    for field, value in (("is_system", True), ("_id", "RHACK001"), ("version", 99)):
        r = await post_rule(client, **{field: value})
        assert r.status_code == 400, f"{field} 应被拒绝，实际 {r.status_code}"
        b = body_of(r)
        assert b["code"] == "CFG-4007"
        assert field in b["data"]["immutable_fields"]
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0


async def test_name_and_description_validation(client):
    """字段级校验失败走模块 00 的 COM-4001（422），与 06-A 同一口径。"""
    r = await post_rule(client, name="X" * 51)
    assert r.status_code == 422 and body_of(r)["code"] == "COM-4001"
    r2 = await post_rule(client, name="   ")
    assert r2.status_code == 422
    r3 = await post_rule(client, description="Y" * 201)
    assert r3.status_code == 422


# ================================================================ 2 修改与版本
async def test_edit_score_and_condition_bump_version(client):
    """**硬性契约（D60 + BR-05-20）**：改分值 / 改条件树 / 改名都必须升 version。

    这是决策重放的地基：`decisions.rule_versions` 记的是"当时哪一版"，
    只要"内容一变、version 就变"成立，重放才算得对。三条修改各断言一次，
    且每次都核对**库里的文档**而不只是响应体。
    """
    got = await create_rule(client)
    code = got["_id"]

    # ① 改分值
    r1 = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 20,
    })
    assert r1.status_code == 200, r1.text
    assert data_of(r1)["version"] == 2
    assert (await read_rule(code))["score"] == 20
    assert (await read_rule(code))["version"] == 2

    # ② 改条件树
    new_tree = {"logic": "and", "children": [
        {"field": "login_cnt_1h", "op": "gte", "value": 10},
    ]}
    r2 = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 2, "condition": new_tree,
    })
    assert r2.status_code == 200 and data_of(r2)["version"] == 3
    assert (await read_rule(code))["condition"] == new_tree

    # ③ 改名
    r3 = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 3, "name": "改名后的规则",
    })
    assert r3.status_code == 200 and data_of(r3)["version"] == 4
    doc = await read_rule(code)
    assert doc["name"] == "改名后的规则"
    assert doc["version"] == 4
    assert doc["updated_by"] == "strategy01"


async def test_three_edits_produce_three_update_audits(client):
    """V-06-02：连续改 3 次 → `rules.version` == 4，且审计恰好 3 条 `rule.update`。"""
    code = (await create_rule(client))["_id"]
    for expected, score in ((1, 10), (2, 11), (3, 12)):
        r = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
            "expected_version": expected, "score": score,
        })
        assert r.status_code == 200, r.text
    assert (await read_rule(code))["version"] == 4

    rows = await audit_rows("rule.update", code)
    assert len(rows) == 3, f"应恰好 3 条 rule.update，实际 {len(rows)}"
    for row in rows:
        assert row["before"] and row["after"], "rule.update 必须含 before/after"
        assert row["before"]["version"] + 1 == row["after"]["version"]
    assert [r["after"]["score"] for r in rows] == [10, 11, 12]


async def test_optimistic_lock_blocks_second_writer(client):
    """V-06-08 / BR-06-05：两个会话拿同一个 `expected_version`，第二个必须 409。"""
    code = (await create_rule(client))["_id"]
    first = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 30,
    })
    assert first.status_code == 200

    second = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 99,
    })
    assert second.status_code == 409, second.text
    b = body_of(second)
    assert b["code"] == "CFG-4006"
    assert b["data"]["current_version"] == 2, "必须回传服务端最新版本供提示"
    assert b["data"]["expected_version"] == 1
    # **不做后写覆盖**：先写者的分值必须还在
    assert (await read_rule(code))["score"] == 30


async def test_update_keeps_absent_fields(client):
    """PUT 留空的字段保持原值：前端只改分值不该把条件树抹掉。"""
    code = (await create_rule(client, description="原始说明"))["_id"]
    r = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 50,
    })
    assert r.status_code == 200
    doc = await read_rule(code)
    assert doc["condition"] == TREE and doc["name"] == "同设备聚集登录"
    assert doc["description"] == "原始说明"


async def test_immutable_fields_rejected_on_update(client):
    code = (await create_rule(client))["_id"]
    r = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 30, "_id": "RNEW001",
    })
    assert r.status_code == 400 and body_of(r)["code"] == "CFG-4007"
    assert (await read_rule(code))["score"] == 45, "被拒绝的修改不得部分生效"


async def test_update_missing_rule_404(client):
    r = await client.put(f"{RULES_URL}/RNOPE999", headers=WRITER, json={
        "expected_version": 1, "score": 30,
    })
    assert r.status_code == 404 and body_of(r)["code"] == "CFG-4001"


async def test_update_missing_expected_version_is_422(client):
    """BR-06-05：`expected_version` 是必填项，缺失时直接拒绝而不是"默认覆盖"。"""
    code = (await create_rule(client))["_id"]
    r = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={"score": 30})
    assert r.status_code == 422 and body_of(r)["code"] == "COM-4001"


# ================================================================ 3 启停用
async def test_toggle_enables_and_audits(client):
    code = (await create_rule(client))["_id"]
    r = await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                          json={"status": "enabled", "expected_version": 1})
    assert r.status_code == 200, r.text
    got = data_of(r)
    assert got["status"] == "enabled" and got["version"] == 2 and got["changed"] is True
    doc = await read_rule(code)
    assert doc["status"] == "enabled" and doc["version"] == 2

    rows = await audit_rows("rule.toggle", code)
    assert len(rows) == 1
    assert rows[0]["before"] == {"status": "disabled", "version": 1}
    assert rows[0]["after"]["status"] == "enabled"
    assert rows[0]["after"]["version"] == 2
    assert rows[0]["after"]["sim_verified"] is False, "BR-06-09：未标注仿真即 false"


async def test_toggle_records_sim_verified_flag(client):
    """BR-06-09：从仿真页带过来的启用要能在审计里区分出来。"""
    code = (await create_rule(client))["_id"]
    r = await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER, json={
        "status": "enabled", "expected_version": 1, "sim_verified": True,
    })
    assert r.status_code == 200
    rows = await audit_rows("rule.toggle", code)
    assert rows[0]["after"]["sim_verified"] is True


async def test_toggle_same_status_is_idempotent(client):
    """V-06-20：对已 enabled 的规则再 toggle 成 enabled → 版本不动、无审计、changed=false。"""
    code = (await create_rule(client))["_id"]
    first = await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                              json={"status": "enabled", "expected_version": 1})
    assert first.status_code == 200 and data_of(first)["changed"] is True

    again = await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                              json={"status": "enabled", "expected_version": 2})
    assert again.status_code == 200, again.text
    got = data_of(again)
    assert got["changed"] is False, "目标状态与当前一致时必须回显 changed=false"
    assert got["version"] == 2, "幂等路径不得递增 version"

    doc = await read_rule(code)
    assert doc["version"] == 2
    assert len(await audit_rows("rule.toggle", code)) == 1, "幂等路径不得写第二条审计"


async def test_toggle_conflict_and_404(client):
    code = (await create_rule(client))["_id"]
    bad = await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                            json={"status": "enabled", "expected_version": 99})
    assert bad.status_code == 409 and body_of(bad)["code"] == "CFG-4006"

    missing = await client.post(f"{RULES_URL}/RNOPE999/toggle", headers=WRITER,
                                json={"status": "enabled", "expected_version": 1})
    assert missing.status_code == 404 and body_of(missing)["code"] == "CFG-4001"


# ================================================================ 4 删除
async def test_delete_is_soft_and_keeps_document(client):
    """BR-06-10 / V-06-07：软删——`status=disabled` + 删除标记，文档保留。"""
    code = (await create_rule(client))["_id"]
    r = await client.delete(f"{RULES_URL}/{code}", headers=WRITER)
    assert r.status_code == 200, r.text
    got = data_of(r)
    assert got["deleted"] is True and got["status"] == "disabled"
    assert got["version"] == 2

    doc = await read_rule(code)
    assert doc["deleted"] is True
    assert doc["status"] == "disabled"
    assert doc["deleted_by"] == "strategy01" and doc["deleted_at"]

    # 列表与单条都不再可见，但文档仍在（历史 decision_hits 可回溯）
    listing = await client.get(RULES_URL, headers=WRITER)
    assert data_of(listing)["total"] == 0
    one = await client.get(f"{RULES_URL}/{code}", headers=WRITER)
    assert one.status_code == 404 and body_of(one)["code"] == "CFG-4001"


async def test_system_rule_cannot_be_deleted(client):
    """V-06-07 / BR-06-08：内置规则 400 CFG-4005，状态纹丝不动。"""
    await insert_raw_rule("RLOGIN001")
    r = await client.delete(f"{RULES_URL}/RLOGIN001", headers=WRITER)
    assert r.status_code == 400
    b = body_of(r)
    assert b["code"] == "CFG-4005"
    doc = await read_rule("RLOGIN001")
    assert doc["status"] == "enabled" and not doc.get("deleted")
    assert await audit_rows("rule.delete", "RLOGIN001") == []


async def test_system_rule_can_be_disabled(client):
    """BR-06-08 的另一半：内置规则**只能停用**——这条路必须通。"""
    await insert_raw_rule("RLOGIN001")
    r = await client.post(f"{RULES_URL}/RLOGIN001/toggle", headers=WRITER,
                          json={"status": "disabled", "expected_version": 1})
    assert r.status_code == 200, r.text
    assert (await read_rule("RLOGIN001"))["status"] == "disabled"


async def test_delete_version_conflict_and_404(client):
    code = (await create_rule(client))["_id"]
    bad = await client.delete(f"{RULES_URL}/{code}?expected_version=99", headers=WRITER)
    assert bad.status_code == 409 and body_of(bad)["code"] == "CFG-4006"
    missing = await client.delete(f"{RULES_URL}/RNOPE999", headers=WRITER)
    assert missing.status_code == 404 and body_of(missing)["code"] == "CFG-4001"


async def test_delete_twice_reports_not_found(client):
    code = (await create_rule(client))["_id"]
    assert (await client.delete(f"{RULES_URL}/{code}", headers=WRITER)).status_code == 200
    again = await client.delete(f"{RULES_URL}/{code}", headers=WRITER)
    assert again.status_code == 404 and body_of(again)["code"] == "CFG-4001"


# ================================================================ 5 审计"恰好一条"
async def test_each_write_writes_exactly_one_audit(client):
    """BR-06-36（D41 范式）：每次成功的写操作**恰好一条**审计。

    四条写路径各查一次，最后再按目标聚合一次总数——只查总数会漏掉
    "某条路径写了两条、另一条没写"这种互相抵消的错误。
    """
    code = (await create_rule(client))["_id"]
    assert len(await audit_rows("rule.create", code)) == 1

    assert (await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 30})).status_code == 200
    assert len(await audit_rows("rule.update", code)) == 1

    assert (await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER, json={
        "status": "enabled", "expected_version": 2})).status_code == 200
    assert len(await audit_rows("rule.toggle", code)) == 1

    assert (await client.delete(f"{RULES_URL}/{code}", headers=WRITER)).status_code == 200
    assert len(await audit_rows("rule.delete", code)) == 1

    cursor = db.get_db()[AUDIT_COLL].find({"target_type": "rule", "target_id": code})
    rows = await cursor.to_list(length=100)
    assert len(rows) == 4, f"该规则的审计总数应为 4，实际 {len(rows)}"
    assert sorted(r["action"] for r in rows) == [
        "rule.create", "rule.delete", "rule.toggle", "rule.update",
    ]
    for row in rows:
        assert row["actor"] == "strategy01" and row["actor_role"] == "strategist"


async def test_failed_write_writes_no_audit(client):
    """被拒绝的写（分值越界 / 编码不存在）绝不能留下审计。"""
    await post_rule(client, score=101)
    await client.put(f"{RULES_URL}/RNOPE999", headers=WRITER,
                     json={"expected_version": 1, "score": 10})
    assert await audit_rows("rule.create") == []
    assert await audit_rows("rule.update") == []


# ================================================================ 6 审计失败回滚
async def _failing_audit(*_args, **_kwargs):
    raise AuditWriteFailedError("rule", "桩：审计不可用")


async def test_audit_failure_rolls_back_create(client, monkeypatch):
    """V-06-16 / CFG-5003：审计写失败 → 新增**未落库**、返回 503。"""
    monkeypatch.setattr(rule_service, "audit", _failing_audit)
    r = await post_rule(client)
    assert r.status_code == 503, r.text
    b = body_of(r)
    assert b["code"] == "CFG-5003"
    assert b["data"]["action"] == "rule.create"
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0, (
        "审计留不下痕迹时，这次写入必须整体撤销（宁可不做，不可无痕地做）"
    )


async def test_audit_failure_rolls_back_update(client, monkeypatch):
    code = (await create_rule(client))["_id"]
    monkeypatch.setattr(rule_service, "audit", _failing_audit)
    r = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "score": 99, "name": "不该生效的名字",
    })
    assert r.status_code == 503
    assert body_of(r)["code"] == "CFG-5003"
    doc = await read_rule(code)
    assert doc["score"] == 45 and doc["name"] == "同设备聚集登录"
    assert doc["version"] == 1, "回滚必须把 version 一起还原，否则乐观锁会错位"


async def test_audit_failure_rolls_back_toggle(client, monkeypatch):
    code = (await create_rule(client))["_id"]
    monkeypatch.setattr(rule_service, "audit", _failing_audit)
    r = await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                          json={"status": "enabled", "expected_version": 1})
    assert r.status_code == 503 and body_of(r)["code"] == "CFG-5003"
    doc = await read_rule(code)
    assert doc["status"] == "disabled" and doc["version"] == 1


async def test_audit_failure_rolls_back_delete(client, monkeypatch):
    code = (await create_rule(client))["_id"]
    monkeypatch.setattr(rule_service, "audit", _failing_audit)
    r = await client.delete(f"{RULES_URL}/{code}", headers=WRITER)
    assert r.status_code == 503 and body_of(r)["code"] == "CFG-5003"
    doc = await read_rule(code)
    assert not doc.get("deleted"), "审计失败时条目必须保持可用状态"
    assert doc["status"] == "disabled"
    assert "deleted_at" not in doc, "本次新增的删除痕迹必须一起清掉"


# ================================================================ 7 缓存失效调用点
async def test_every_write_invalidates_rule_cache(client, monkeypatch):
    """BR-06-13：四条写路径**各调用一次**规则缓存失效点（不等 AD-02 的 TTL）。

    用桩替换失效函数来计数，是因为这条契约的内容就是"必须调用它"：
    05 当前每次决策直查 Mongo（无规则缓存），所以"没有可清的对象"，
    只有"调用过没有"这件事可断言。桩换成计数器而不是空函数，
    以免用例自身变成空壳断言。
    """
    calls: list[int] = []
    real = rule_service.invalidate_rule_cache

    def counting() -> None:
        calls.append(len(calls))
        real()

    monkeypatch.setattr(rule_service, "invalidate_rule_cache", counting)
    code = (await create_rule(client))["_id"]
    await client.put(f"{RULES_URL}/{code}", headers=WRITER,
                     json={"expected_version": 1, "score": 30})
    await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                      json={"status": "enabled", "expected_version": 2})
    await client.delete(f"{RULES_URL}/{code}", headers=WRITER)
    assert len(calls) == 4, f"四条写路径应各失效一次，实际 {len(calls)}"
    assert rule_service.RULE_CACHE_FLUSH.flush_count == 4


async def test_idempotent_toggle_does_not_invalidate_cache(client, monkeypatch):
    """幂等路径（状态没变）不该声称"缓存已失效"——它什么都没改。"""
    code = (await create_rule(client))["_id"]
    await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                      json={"status": "enabled", "expected_version": 1})
    calls: list[int] = []

    def counting() -> None:
        calls.append(1)

    monkeypatch.setattr(rule_service, "invalidate_rule_cache", counting)
    await client.post(f"{RULES_URL}/{code}/toggle", headers=WRITER,
                      json={"status": "enabled", "expected_version": 2})
    assert calls == []


# ================================================================ 8 不动历史快照（BR-05-21）
async def test_rename_and_rescore_never_rewrite_history(client):
    """BR-05-21 / 任务硬性契约：改名改分**不得**回写 `decision_hits` 的冗余快照。

    这是**故意的失真防护**：历史决策必须继续按当时的名称与分值解释。
    若这里被"顺手修正"，复核页上的旧案件会显示成新名字与新的分值，
    等于把当时的判定依据改掉了。
    """
    code = (await create_rule(client, name="旧名字", score=45))["_id"]
    hit = {
        "_id": "DEC1-001", "decision_id": "DEC1", "event_id": "EVT1",
        "rule_code": code, "rule_name": "旧名字", "rule_version": 1,
        "score": 45, "reason": "device_user_cnt ≥ 5", "matched_facts": {},
        "hit_at": now_ms(),
    }
    await db.get_db()[constants.COLL_DECISION_HITS].insert_one(dict(hit))
    await db.get_db()[constants.COLL_DECISIONS].insert_one({
        "_id": "DEC1", "event_id": "EVT1", "rule_versions": {code: 1},
    })

    r = await client.put(f"{RULES_URL}/{code}", headers=WRITER, json={
        "expected_version": 1, "name": "新名字", "score": 90,
    })
    assert r.status_code == 200

    stored = await db.get_db()[constants.COLL_DECISION_HITS].find_one({"_id": "DEC1-001"})
    assert stored["rule_name"] == "旧名字", "历史命中明细是快照，绝不能被回写"
    assert stored["score"] == 45
    assert stored["rule_version"] == 1

    decisions = await db.get_db()[constants.COLL_DECISIONS].find_one({"_id": "DEC1"})
    assert decisions["rule_versions"] == {code: 1}, "历史 rule_versions 同样不得回写"


async def test_impact_reports_hits_and_decision_refs(client):
    """§5.2：删除确认弹窗必须能拿到"近 30 天命中次数"（后端不做二次确认，但要给数）。"""
    code = (await create_rule(client))["_id"]
    now = now_ms()
    await db.get_db()[constants.COLL_DECISION_HITS].insert_many([
        {"_id": "DEC9-001", "decision_id": "DEC9", "event_id": "E1", "rule_code": code,
         "rule_name": "x", "rule_version": 1, "score": 45, "reason": "r",
         "matched_facts": {}, "hit_at": now},
        {"_id": "DEC9-002", "decision_id": "DEC9", "event_id": "E1", "rule_code": code,
         "rule_name": "x", "rule_version": 1, "score": 45, "reason": "r",
         "matched_facts": {}, "hit_at": now - 40 * DAY_MS},   # 超出 30 天窗口
    ])
    await db.get_db()[constants.COLL_DECISIONS].insert_one({
        "_id": "DEC9", "event_id": "E1", "rule_versions": {code: 1},
    })

    r = await client.get(f"{RULES_URL}/{code}/impact", headers=WRITER)
    assert r.status_code == 200, r.text
    got = data_of(r)
    assert got["hit_count_30d"] == 1, "只统计窗口内的命中"
    assert got["decision_refs"] == 1
    assert got["is_system"] is False
    assert "不受影响" in got["hint"]

    system_impact = await client.get(f"{RULES_URL}/RNOPE999/impact", headers=WRITER)
    assert system_impact.status_code == 404


# ================================================================ 9 列表契约
async def test_list_pagination_and_filters(client):
    """V-06-19 / §3.5：分页生效、越界不报错、筛选正确、`as_of` 存在。"""
    for index in range(25):
        await create_rule(client, name=f"规则{index:02d}",
                          status="enabled" if index % 2 == 0 else "disabled")

    page1 = data_of(await client.get(RULES_URL, headers=WRITER))
    assert page1["total"] == 25 and len(page1["items"]) == 20
    assert page1["page"] == 1 and page1["page_size"] == 20
    assert page1["pages"] == 2
    assert isinstance(page1["as_of"], int)

    page2 = data_of(await client.get(f"{RULES_URL}?page=2", headers=WRITER))
    assert len(page2["items"]) == 5

    # page 超过总页数不算错：返回空 items + 正确 total
    over = await client.get(f"{RULES_URL}?page=9", headers=WRITER)
    assert over.status_code == 200
    assert data_of(over)["items"] == [] and data_of(over)["total"] == 25

    assert data_of(await client.get(f"{RULES_URL}?status=enabled", headers=WRITER))["total"] == 13
    assert data_of(await client.get(f"{RULES_URL}?status=disabled", headers=WRITER))["total"] == 12
    assert data_of(await client.get(f"{RULES_URL}?scene_code=login", headers=WRITER))["total"] == 25
    assert data_of(await client.get(f"{RULES_URL}?scene_code=coupon", headers=WRITER))["total"] == 0

    kw = data_of(await client.get(f"{RULES_URL}?keyword=规则1", headers=WRITER))
    assert kw["total"] == 10, "keyword 应模糊匹配 name（规则10~规则19）"
    by_code = data_of(await client.get(f"{RULES_URL}?keyword=RLOGIN001", headers=WRITER))
    assert by_code["total"] == 1, "keyword 也应匹配 _id"
    lower = data_of(await client.get(f"{RULES_URL}?keyword=rlogin002", headers=WRITER))
    assert lower["total"] == 1, "规则编码搜索应大小写不敏感"


async def test_list_default_sort_is_priority_then_code(client):
    """BR-06-11：默认 `priority asc, _id asc`（与 05 的稳定排序语义一致）。"""
    await create_rule(client, name="低优先级", priority=30)
    await create_rule(client, name="高优先级", priority=5)
    await create_rule(client, name="同优先级", priority=30)
    got = data_of(await client.get(RULES_URL, headers=WRITER))
    assert [i["priority"] for i in got["items"]] == [5, 30, 30]
    assert [i["_id"] for i in got["items"]] == ["RLOGIN002", "RLOGIN001", "RLOGIN003"]


async def test_list_sort_whitelist_and_param_bounds(client):
    """§3.5 / CFG-4008：非法排序字段、page/page_size 越界都要报 400 CFG-4008。"""
    cases = [
        "?page=0", "?page=201", "?page_size=0", "?page_size=101",
        "?sort=score:up", "?sort=description:desc", "?sort=nonexistent:asc",
    ]
    for query in cases:
        r = await client.get(f"{RULES_URL}{query}", headers=WRITER)
        assert r.status_code == 400, f"{query} 应报 400，实际 {r.status_code}"
        assert body_of(r)["code"] == "CFG-4008", query
    ok = await client.get(f"{RULES_URL}?sort=score:desc&page=1&page_size=100", headers=WRITER)
    assert ok.status_code == 200
    bad_status = await client.get(f"{RULES_URL}?status=all", headers=WRITER)
    assert bad_status.status_code == 400 and body_of(bad_status)["code"] == "CFG-4008"


async def test_list_score_hint_for_scene_over_100(client):
    """BR-06-12：场景内启用规则分值合计 > 100 时给出非阻断提示数据。"""
    await create_rule(client, name="A", score=60, status="enabled")
    await create_rule(client, name="B", score=60, status="enabled")
    await create_rule(client, name="C", score=90, status="disabled")
    got = data_of(await client.get(RULES_URL, headers=WRITER))
    hint = {h["scene_code"]: h for h in got["score_hints"]}["login"]
    assert hint["enabled_score_sum"] == 120, "只累加 enabled 的规则"
    assert hint["enabled_rule_count"] == 2
    assert hint["over_limit"] is True
    assert got["over_limit_scenes"] == ["login"]


async def test_keyword_regex_metacharacters_are_safe(client):
    await create_rule(client)
    for kw in [".*", "(", "[a-z]+", "\\", "^R", "RLOGIN.*"]:
        r = await client.get(f"{RULES_URL}?keyword={kw}", headers=WRITER)
        assert r.status_code == 200, f"keyword={kw!r} 必须安全处理"


# ================================================================ 10 权限
async def test_reviewer_cannot_read_or_write_rules(client):
    """V-06-18：`reviewer` 无 `rule:write`。

    矩阵里**没有** `rule:read`（`permissions.py` 是权限唯一真源，不得新造），
    `MENU_PERMISSIONS["#/rules"] = rule:write` 也把"能进这个页面"定义为 strategist，
    因此读与写共用同一权限。越权一律 `AUTH-4020`（原 `CFG-4031` 已作废，D28）。
    """
    code = (await create_rule(client))["_id"]
    for method, url, kw in (
        ("get", RULES_URL, {}),
        ("get", f"{RULES_URL}/{code}", {}),
        ("get", f"{RULES_URL}/{code}/impact", {}),
        ("post", RULES_URL, {"json": {"name": "x", "scene_code": "login",
                                      "condition": TREE, "score": 10}}),
        ("post", f"{RULES_URL}/validate-tree", {"json": {"condition": TREE}}),
        ("get", f"{RULES_URL}/import-template", {}),
        ("post", f"{RULES_URL}/import",
         {"files": {"file": ("r.csv", b"x", "text/csv")}}),
        ("put", f"{RULES_URL}/{code}", {"json": {"expected_version": 1, "score": 10}}),
        ("post", f"{RULES_URL}/{code}/toggle",
         {"json": {"status": "enabled", "expected_version": 1}}),
        ("delete", f"{RULES_URL}/{code}", {}),
    ):
        resp = await getattr(client, method)(url, headers=READER, **kw)
        assert resp.status_code == 403, f"{method.upper()} {url} 应为 403，实际 {resp.status_code}"
        assert body_of(resp)["code"] == "AUTH-4020"


async def test_admin_cannot_write_rules(client):
    """D26：`admin` **不**具备 `rule:write`（矩阵只给 strategist）。"""
    r = await post_rule(client, headers=ADMIN)
    assert r.status_code == 403 and body_of(r)["code"] == "AUTH-4020"


# ================================================================ 11 幂等（§3.1）
async def test_idempotency_key_replays_first_result(client):
    """`Idempotency-Key`：同一次保存重试复用键 → 返回首次结果，不重复建规则。"""
    payload = {"name": "幂等规则", "scene_code": "login", "condition": TREE, "score": 45}
    headers = {**WRITER, "Idempotency-Key": "6f1a3c2e-1111-2222-3333-abcdefabcdef"}
    first = await client.post(RULES_URL, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(RULES_URL, json=payload, headers=headers)
    assert second.status_code == 201
    assert data_of(second)["_id"] == data_of(first)["_id"]
    assert "首次结果" in body_of(second)["message"]
    assert await db.get_db()[RULES_COLL].count_documents({}) == 1

    # 换一个键就是一次新的保存
    other = await client.post(RULES_URL, json=payload,
                              headers={**WRITER, "Idempotency-Key": "other-key"})
    assert other.status_code == 201
    assert await db.get_db()[RULES_COLL].count_documents({}) == 2


async def test_idempotency_key_released_after_failure(client, monkeypatch):
    """失败不能把幂等键永久占住（否则同键重试会一直等一个不会到来的结果）。"""
    monkeypatch.setattr(rule_service, "audit", _failing_audit)
    headers = {**WRITER, "Idempotency-Key": "retry-me"}
    failed = await client.post(RULES_URL, json={
        "name": "x", "scene_code": "login", "condition": TREE, "score": 10,
    }, headers=headers)
    assert failed.status_code == 503

    monkeypatch.undo()
    retried = await client.post(RULES_URL, json={
        "name": "x", "scene_code": "login", "condition": TREE, "score": 10,
    }, headers=headers)
    assert retried.status_code == 201, retried.text


# ================================================================ 12 批量导入
IMPORT_URL = "/api/v1/rules/import"
TEMPLATE_URL = "/api/v1/rules/import-template"
IMPORT_JSON = '{"logic":"and","children":[{"field":"login_cnt_1h","op":"gte","value":10}]}'


def csv_bytes(rows: list[list[str]], header: list[str] | None = None) -> bytes:
    """构造导入用的 CSV 字节流（默认用契约表头）。"""
    import csv as _csv
    import io as _io

    from app.schemas.rule_schema import RULE_IMPORT_HEADER

    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header if header is not None else list(RULE_IMPORT_HEADER))
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def row(condition: str = IMPORT_JSON, **over) -> list[str]:
    cells = {
        "rule_code": "", "name": "导入规则", "scene_code": "login",
        "description": "由导入创建", "condition": condition,
        "score": "30", "priority": "10", "status": "disabled",
    }
    cells.update(over)
    from app.schemas.rule_schema import RULE_IMPORT_HEADER

    return [cells[c] for c in RULE_IMPORT_HEADER]


async def upload(client, content: bytes, headers=WRITER, **form):
    files = {"file": ("rules.csv", content, "text/csv")}
    return await client.post(IMPORT_URL, files=files, data=form, headers=headers)


async def test_import_partial_mode_reports_failed_rows(client):
    """BR-06-28 的规则侧：`partial` —— 成功行入库、失败行带行号与原因。"""
    content = csv_bytes([
        row(name="好行1"),
        row(name="坏场景", scene_code="not_a_scene"),
        row(name="坏分值", score="101"),
        row(name="坏JSON", condition="{not json"),
        row(name="好行2", condition='{"logic":"and","children":'
                                     '[{"field":"ip_is_proxy","op":"eq","value":true}]}'),
    ])
    r = await upload(client, content)
    assert r.status_code == 200, r.text
    got = data_of(r)
    assert got["total"] == 5 and got["success"] == 2 and got["failed"] == 3
    reasons = {f["row"]: f["reason"] for f in got["rows"]}
    assert set(reasons) == {3, 4, 5}, reasons
    assert "场景" in reasons[3]
    assert "0~100" in reasons[4] or "分值" in reasons[4]
    assert "JSON" in reasons[5]

    # 成功行真的落库了，且**每行恰好一条** rule.create 审计
    assert await db.get_db()[RULES_COLL].count_documents({}) == 2
    assert len(await audit_rows("rule.create")) == 2
    assert len(got["imported_ids"]) == 2


async def test_import_atomic_mode_writes_nothing_on_any_error(client):
    """BR-06-28：`atomic` —— 任一行校验不过则整批不写，并返回**全部**错误行。"""
    content = csv_bytes([
        row(name="好行"),
        row(name="坏行1", score="999"),
        row(name="坏行2", condition="[]"),
    ])
    r = await upload(client, content, mode="atomic")
    assert r.status_code == 200, r.text
    got = data_of(r)
    assert got["mode"] == "atomic"
    assert got["success"] == 0 and got["failed"] == 3
    assert len(got["rows"]) == 2, "atomic 要一次给出全部错误行，让用户一次改完"
    assert await db.get_db()[RULES_COLL].count_documents({}) == 0
    assert await audit_rows("rule.create") == []


async def test_import_accepts_explicit_rule_code_and_reports_collision(client):
    """§8 新增-7：编码可留空（服务端生成）或显式指定；撞已有编码按行报冲突。"""
    content = csv_bytes([
        row(rule_code="RCOUPON777", name="指定编码", scene_code="coupon"),
        row(rule_code="RCOUPON777", name="重复编码", scene_code="coupon"),
        row(name="自动编码"),
    ])
    r = await upload(client, content)
    got = data_of(r)
    assert got["success"] == 2 and got["failed"] == 1
    assert got["imported_ids"] == ["RCOUPON777", "RLOGIN001"]
    assert len(got["rows"]) == 1
    assert got["rows"][0]["row"] == 3, "报错要指向文件里的真实行号"
    assert got["rows"][0]["rule_code"] == "RCOUPON777"
    assert "占用" in got["rows"][0]["reason"] or "已存在" in got["rows"][0]["reason"]


async def test_import_rejects_bad_code_format_and_header(client):
    bad_code = await upload(client, csv_bytes([row(rule_code="NOT-A-CODE")]))
    got = data_of(bad_code)
    assert got["success"] == 0 and "格式非法" in got["rows"][0]["reason"]

    bad_header = await upload(client, csv_bytes([["a", "b"]], header=["x", "y"]))
    assert bad_header.status_code == 400
    assert body_of(bad_header)["code"] == "CFG-4012"

    empty = await upload(client, b"")
    assert empty.status_code == 400 and body_of(empty)["code"] == "CFG-4012"


async def test_import_row_limit_and_mode_validation(client):
    too_many = csv_bytes([row(name=f"r{i}") for i in range(501)])
    r = await upload(client, too_many)
    assert r.status_code == 400 and "500" in body_of(r)["message"]

    bad_mode = await upload(client, csv_bytes([row()]), mode="whatever")
    assert bad_mode.status_code == 422 and body_of(bad_mode)["code"] == "COM-4001"


async def test_import_template_matches_parser_and_is_not_shadowed(client):
    """模板表头与解析共用一份常量；且本路由**不能被** `/rules/{rule_code}` 吃掉。"""
    from app.schemas.rule_schema import RULE_IMPORT_HEADER

    r = await client.get(TEMPLATE_URL, headers=WRITER)
    assert r.status_code == 200, (
        "若这里 404，说明 `/rules/{rule_code}` 声明在模板路由之前把它吃掉了"
    )
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    first_line = r.text.splitlines()[0]
    assert first_line == ",".join(RULE_IMPORT_HEADER)

    # 模板里自带示例行的表头与解析器一致 —— 用模板原样跑一次导入，示例行必须合法
    ok = await upload(client, r.content)
    assert ok.status_code == 200, ok.text
    assert data_of(ok)["success"] == 1, data_of(ok)["rows"]


async def test_import_template_requires_permission(client):
    r = await client.get(TEMPLATE_URL, headers=READER)
    assert r.status_code == 403 and body_of(r)["code"] == "AUTH-4020"


# ================================================================ 13 模块装配
async def test_routes_registered_exactly_once(client):
    """路由注册：06-A 与 06-B 共用模块编号 `06`，只登记一次（重复登记会抛错）。"""
    from fastapi import APIRouter

    from app.api import MODULE_ROUTERS, register_router
    from app.main import app

    assert "06" in MODULE_ROUTERS
    # 06-B 走的是 `merge=True`：对同一编号再登记一次必须**大声报错**，
    # 而不是静默覆盖（否则后登记者的接口会凭空消失）。
    with pytest.raises(ValueError):
        register_router("06", APIRouter())

    paths = app.openapi()["paths"]
    for path in ("/api/v1/lists", "/api/v1/rules", "/api/v1/rules/validate-tree",
                 "/api/v1/rules/{rule_code}/toggle"):
        assert path in paths, f"{path} 未挂到 06 模块的前缀下"
