# -*- coding: utf-8 -*-
"""`POST /api/v1/engine/evaluate` 的接口面（Spec §3.1 / §3.2 / §5 / §6）。

## 覆盖

- **契约**：§3.1 的字段逐个在位；响应仍是 5 键信封 + `X-Trace-Id`；
- **`dry_run`**：`true` 不写 `decisions`/`decision_hits`，`false` 写；
- **`trace`**：`true` 给逐规则过程、`false` 必须为 `null`（不是空数组）；
- **错误码**：`RUL-4001`(400) / 03 的 `EVT-*` 原样透出 / `RUL-5001`/`RUL-5002`(503)；
- **权限**：`sim:run`（无令牌 401、角色不符 403）；
- **V-05-13**：本接口与 03 调的 `RuleDecisionProvider` 对同一份特征给出**同一个**
  决策块——这是"仿真必须复用真实链路"（Spec §3.1）在 05 侧留下的门禁。

## 为什么 503 的响应体里还要有决策块

Spec §3.1 的状态码栏写的是「503 引擎依赖不可用（**返回 degraded=true 与人工审核
建议，不返回 pass**）」。因此这里断言两件事同时成立：HTTP 是 503，而 `data` 里
带着那个 `decision=review`、`degraded=true` 的决策块（且已按 D5 落库建案）。
只断言状态码会漏掉"结论丢了"这一半。
"""
from __future__ import annotations

import pytest

from app import config, db
from app.constants import COLL_DECISIONS, COLL_DECISION_HITS, COLL_DEVICES, COLL_IP_POOL
from app.engine import list_filter
from app.main import app
from app.repos.rule_repo import RuleRepo

from tests.conftest import ADMIN, READER, WRITER
from tests.engine_testlib import (
    flush_decisions as flush_decision_writes,
    install_lists,
    install_rules,
    leaf,
    list_doc,
    rule_doc,
)

pytestmark = pytest.mark.anyio

URL = f"{config.API_PREFIX}/engine/evaluate"

#: 一条合法的事件体（`login` 的必填字段齐备）
LOGIN_EVENT: dict = {
    "event_type": "login",
    "user_id": "U000001",
    "device_id": "DN00001",
    "ip": "203.0.113.10",
    "scene_extra": {"login_type": "pwd", "ua": "Mozilla/5.0", "success": True},
}

#: 让 04 能算出关键特征 `ip_is_proxy`（否则 03/本接口都会按 fail-closed 降级）
PREPARED_IP = "203.0.113.10"


async def _prepare_ip() -> None:
    """预置 E12 IP 画像，使 04 的快照完整（D46 的演示前提）。"""
    await db.get_db()[COLL_IP_POOL].replace_one(
        {"_id": PREPARED_IP},
        {"_id": PREPARED_IP, "is_proxy": False, "is_idc": False},
        upsert=True,
    )


async def _prepare_login_rules() -> None:
    """两条登录规则：同设备聚集 40 + 代理 IP 20。"""
    await install_rules(
        rule_doc("RLOGIN001", leaf("device_user_cnt", "gte", 5), score=40,
                 scene_code="login", name="同设备聚集登录"),
        rule_doc("RLOGIN002", leaf("ip_is_proxy", "eq", True), score=20,
                 scene_code="login", name="代理IP登录", priority=20),
    )


# ============================================================
# 权限（模块 01 的矩阵，不在本模块自造权限码）
# ============================================================
async def test_requires_authentication(client):
    """无令牌 → 401（`AUTH-4002` 请先登录）。"""
    r = await client.post(URL, json={"event": LOGIN_EVENT})
    assert r.status_code == 401


@pytest.mark.parametrize("case,headers,expected", [
    ("admin-无该权限", ADMIN, 403),
    ("reviewer-有该权限", READER, 200),
])
async def test_requires_sim_run_permission(client, case, headers, expected):
    """权限取 `sim:run`（reviewer + strategist）：admin 无该权限 → 403。

    Spec §3.1 未规定权限，本模块按"仿真/调试接口"的定位取矩阵里**已有**的
    `sim:run`，绝不自造权限码（BR-01-12：矩阵是唯一真源）。
    """
    r = await client.post(URL, json={"event": LOGIN_EVENT}, headers=headers)
    assert r.status_code == expected, f"{case}: {r.text}"
    if expected == 403:
        assert r.json()["code"] == "AUTH-4020"


# ============================================================
# 契约（§3.1）
# ============================================================
async def test_response_matches_the_spec_31_field_table(client):
    """§3.1 的响应字段逐个在位，且决策块的取值域正确。"""
    await _prepare_ip()
    await _prepare_login_rules()
    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                          headers=WRITER)
    assert r.status_code == 200, r.text
    body = r.json()
    # 统一信封（BR-00-21）
    assert set(body) == {"ok", "code", "message", "trace_id", "data"}
    assert body["ok"] is True and body["code"] == "OK"
    assert r.headers.get("X-Trace-Id") == body["trace_id"]

    data = body["data"]
    for field in ("event_id", "snapshot_id", "list_hit", "rule_score", "model_score",
                  "final_score", "risk_level", "decision", "hit_rule_count", "hits",
                  "rule_versions", "engine_version", "elapsed_ms", "degraded", "trace",
                  "warnings"):
        assert field in data, f"§3.1 的字段 {field} 缺失"
    assert data["event_id"].startswith("EVT")
    assert data["snapshot_id"].startswith("SNP"), "由 04 的真实快照给出"
    assert data["model_score"] is None, "AD-09：模型引擎是空实现"
    assert data["final_score"] == data["rule_score"], "G-01：当前恒等"
    assert data["engine_version"] == "rule-engine-v1"
    assert data["decision"] in {"pass", "review", "reject"}
    assert data["risk_level"] in {"low", "medium", "high"}
    assert isinstance(data["rule_score"], int) and 0 <= data["rule_score"] <= 100
    assert data["degraded"] is False
    assert data["list_hit"] == {"hit": False, "list_type": None,
                                "entity_type": None, "entity_value": None}, (
        "`list_hit` 是对象（契约裁定）：任何情况下都不是 bool"
    )
    # hits 的元素形状（§3.1）
    if data["hits"]:
        hit = data["hits"][0]
        assert set(hit) == {"rule_code", "rule_name", "rule_version", "score",
                            "reason", "matched_facts"}


async def test_trace_is_null_unless_requested(client):
    """§3.1：`trace=false` 时是 `null`（不是空数组），`true` 时给逐规则过程。

    `[]` 与 `null` 的区别是有意义的：前者会被前端读成"求值了但一条规则都没有"，
    后者才是"没要求返回过程"。
    """
    await _prepare_ip()
    await _prepare_login_rules()

    off = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                            headers=WRITER)
    assert off.json()["data"]["trace"] is None

    on = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True,
                                      "trace": True}, headers=WRITER)
    trace = on.json()["data"]["trace"]
    assert isinstance(trace, list) and trace
    assert set(trace[0]) == {"rule_code", "evaluated", "matched", "reason"}
    assert {row["rule_code"] for row in trace} == {"RLOGIN001", "RLOGIN002"}


async def test_rule_hits_carry_actual_values_and_versions(client):
    """命中明细带**实际特征值**（BR-05-22）与版本快照（BR-05-20）。

    这里把 `device_user_cnt` 做成**确定值**：09 的 `MongoLinkedUserCountProvider`
    在 `devices` 有画像行时直接返回它的 `linked_user_cnt`（冗余计数的唯一真源，
    BR-09-12/13），因此写入 `linked_user_cnt=9` 之后 `device_user_cnt` 必定是 9，
    规则 `gte 5` 必定命中——断言不必再靠 `if hits:` 兜着（那样等于没有断言）。
    """
    await _prepare_ip()
    await _prepare_login_rules()
    await db.get_db()[COLL_DEVICES].replace_one(
        {"_id": "DN00001"},
        {"_id": "DN00001", "linked_user_cnt": 9,
         "first_seen_at": 1_700_000_000_000},
        upsert=True,
    )
    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                          headers=WRITER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["hit_rule_count"] == 1
    assert data["rule_score"] == 40
    # `rule_versions` 是**本次生效规则集**的快照：两条规则都被取来求值了，
    # 因此即使 `RLOGIN002`（代理 IP）没命中，它的版本也在快照里——
    # 只记命中项无法重放（重放要先知道"当时有哪些规则参与"，BR-05-20）。
    assert data["rule_versions"] == {"RLOGIN001": 1, "RLOGIN002": 1}
    hit = data["hits"][0]
    assert hit["rule_code"] == "RLOGIN001"
    assert hit["rule_name"] == "同设备聚集登录"
    assert hit["rule_version"] == 1
    assert hit["matched_facts"] == {"device_user_cnt": 9}
    assert "9" in hit["reason"], f"BR-05-22：reason 必须含实际值，实际 {hit['reason']!r}"
    assert "5" in hit["reason"], "阈值也要写出来"
    assert data["risk_level"] == "low" and data["decision"] == "pass", (
        "40 分属低档（BR-05-17）：分档口径与 `final_score` 必须一致"
    )


# ============================================================
# dry_run
# ============================================================
async def test_dry_run_true_writes_nothing_but_dry_run_false_writes(client):
    """§3.1：`dry_run=true` 不落库；`false` 必须落 `decisions` + `decision_hits`。"""
    await _prepare_ip()
    await _prepare_login_rules()

    dry = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                            headers=WRITER)
    assert dry.status_code == 200
    await flush_decision_writes()
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 0
    assert await db.get_db()[COLL_DECISION_HITS].count_documents({}) == 0

    real = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": False},
                             headers=WRITER)
    assert real.status_code == 200, real.text
    await flush_decision_writes()
    docs = await db.get_db()[COLL_DECISIONS].find({}).to_list(length=10)
    assert len(docs) == 1
    assert docs[0]["engine_version"] == "rule-engine-v1"
    assert docs[0]["degraded"] is False
    assert docs[0]["event_id"] == real.json()["data"]["event_id"]


async def test_dry_run_defaults_to_true(client):
    """§3.1 的请求表：`dry_run` **默认 `true`**（仿真/调试接口不该默认写库）。"""
    await _prepare_ip()
    r = await client.post(URL, json={"event": LOGIN_EVENT}, headers=WRITER)
    assert r.status_code == 200
    await flush_decision_writes()
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 0


# ============================================================
# 入参校验（RUL-4001 / 03 的 EVT-*）
# ============================================================
@pytest.mark.parametrize("event,missing", [
    ({"user_id": "U000001"}, "event_type"),
    ({"event_type": "login"}, "user_id"),
    ({"event_type": "  ", "user_id": "U000001"}, "event_type"),
])
async def test_invalid_event_body_returns_rul_4001(client, event, missing):
    """`RUL-4001`(400)：缺 `event_type` / `user_id`（Spec §5 对它的定义）。"""
    r = await client.post(URL, json={"event": event}, headers=WRITER)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "RUL-4001"
    assert missing in body["message"]
    assert body["data"]["missing"] == [missing]


async def test_empty_event_object_is_rejected(client):
    """`event` 为空对象同样是 RUL-4001（而不是 500 或静默 pass）。"""
    r = await client.post(URL, json={"event": {}}, headers=WRITER)
    assert r.status_code == 400
    assert r.json()["code"] == "RUL-4001"


async def test_event_body_must_be_an_object(client):
    """`event` 不是对象 → 400（Pydantic 层拦下，仍是 4xx 客户端错误）。"""
    r = await client.post(URL, json={"event": "oops"}, headers=WRITER)
    assert r.status_code in (400, 422)


async def test_unknown_request_field_is_rejected(client):
    """请求体多余键 → 422（`extra="forbid"`）：拼错字段名不该被静默忽略。"""
    r = await client.post(URL, json={"event": LOGIN_EVENT, "dryrun": True},
                          headers=WRITER)
    assert r.status_code == 422


async def test_deeper_validation_keeps_the_evt_prefix(client):
    """更深一层的场景字段缺失由 03 的校验器报，**错误码保持 EVT 前缀**（ER-02）。

    两条链路对同一个坏报文给同一个码，调用方才能用一套分流逻辑（模块 10 的
    仿真页正是靠 `EVT-4004` 的 `data.missing` 渲染"缺失字段：device_id"）。
    """
    event = dict(LOGIN_EVENT)
    del event["device_id"]
    r = await client.post(URL, json={"event": event}, headers=WRITER)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "EVT-4004"
    assert "device_id" in body["data"]["missing"]


async def test_unsupported_event_type_keeps_the_evt_prefix(client):
    """不支持的事件类型 → `EVT-4003`（400），仍保持 03 的前缀。"""
    r = await client.post(URL, json={"event": {**LOGIN_EVENT, "event_type": "refund"}},
                          headers=WRITER)
    assert r.status_code == 400
    assert r.json()["code"] == "EVT-4003"


# ============================================================
# 依赖不可用（RUL-5001 / RUL-5002）——503 但**结论不丢**
# ============================================================
async def test_list_failure_returns_503_with_a_degraded_decision(client, monkeypatch):
    """RUL-5001：名单依赖不可用 → 503，但 `data` 里带着降级决策块（且已落库）。

    Spec §3.1 的状态码栏要求「503 …返回 degraded=true 与人工审核建议，不返回
    pass」。只断言状态码会漏掉"结论丢了"这一半；只断言决策块又会漏掉"这是故障
    而不是正常的人审"。
    """
    from app.repos.list_repo import ListRepo

    async def boom(self, list_type, entity_type, entity_value):
        raise RuntimeError("simulated mongo failure")

    monkeypatch.setattr(ListRepo, "find_active", boom)
    list_filter.LIST_CACHE.clear()
    await _prepare_ip()

    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": False},
                          headers=WRITER)
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["code"] == "RUL-5001"
    data = body["data"]
    assert data["degraded"] is True
    assert data["decision"] == "review", "fail-closed：绝不返回 pass"
    assert data["decision"] != "pass"
    assert data["rule_score"] == 0 and data["hits"] == []
    assert data["engine_version"] == "rule-engine-v1"
    assert any(w["code"] == "RUL-5001" for w in data["warnings"])

    # D5：降级也已经落库建案（08 据此建案），所以这个 503 不代表请求丢了
    await flush_decision_writes()
    doc = await db.get_db()[COLL_DECISIONS].find_one({})
    assert doc is not None and doc["decision"] == "review" and doc["degraded"] is True


async def test_rule_set_failure_returns_503_with_a_degraded_decision(client, monkeypatch):
    """RUL-5002：`rules` 读不出来 → 503 + 降级决策块（同样绝不 pass）。"""
    async def boom(self, scene_code):
        raise RuntimeError("simulated mongo failure")

    monkeypatch.setattr(RuleRepo, "list_enabled_rules", boom)
    await _prepare_ip()

    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                          headers=WRITER)
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["code"] == "RUL-5002"
    assert body["data"]["decision"] == "review"
    assert body["data"]["degraded"] is True


async def test_per_rule_failure_is_a_200_with_a_warning(client):
    """`RUL-5003`：单条规则非法 → **200**（结论照常）+ `warnings` 里能看到条数。

    页面要在命中明细下方提示「有 N 条规则求值失败」（Spec §5），因此失败信息
    必须随响应下发——`RUL-5003` 的 HTTP 是 200，它不是错误响应。
    """
    await _prepare_ip()
    await install_rules(
        rule_doc("RBROKEN", {"field": "login_cnt_1h", "op": "between", "value": 5},
                 score=60, scene_code="login"),
        rule_doc("RLOGIN001", leaf("device_user_cnt", "gte", 5), score=40,
                 scene_code="login", priority=20),
    )
    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                          headers=WRITER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    warnings = [w for w in data["warnings"] if w["code"] == "RUL-5003"]
    assert len(warnings) == 1
    assert "RBROKEN" in warnings[0]["message"]


# ============================================================
# V-05-13：仿真链路复用同一引擎
# ============================================================
async def test_v05_13_endpoint_and_provider_agree_on_the_same_features(client):
    """V-05-13：`/engine/evaluate` 与 03 调的 `RuleDecisionProvider` 结果一致。

    做法：先让本接口算一次（`dry_run=true`，它自己向 04 取快照），再把 04
    落库的那份**真实特征**喂给 provider，两边比较共享字段。

    这是"仿真禁止另写判定逻辑"（Spec §3.1）在 05 侧的门禁：任何一处若多算了
    一次、少取一条规则、或换了排序口径，这条都会红。
    """
    from app.engine.decision_provider import RuleDecisionProvider
    from app.services import feature_service

    await _prepare_ip()
    await _prepare_login_rules()

    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                          headers=WRITER)
    assert r.status_code == 200, r.text
    sim = r.json()["data"]
    event_id = sim["event_id"]

    # 取 04 落库的真实快照特征（与接口内部用的是同一份）
    await feature_service.get_feature_service().flush()
    snapshot = await db.get_db()["feature_snapshots"].find_one({"event_id": event_id})
    assert snapshot is not None, "接口必须真的调用了 04（快照已落库）"

    provider_block = await RuleDecisionProvider().evaluate(
        {"_id": event_id, "event_type": LOGIN_EVENT["event_type"],
         "user_id": LOGIN_EVENT["user_id"], "device_id": LOGIN_EVENT["device_id"],
         "ip": LOGIN_EVENT["ip"], "ts": snapshot["computed_at"], "scene_extra": {}},
        dict(snapshot["features"]),
    )
    for key in ("list_hit", "rule_score", "model_score", "final_score", "risk_level",
                "decision", "hit_rule_count", "hits", "rule_versions",
                "engine_version"):
        assert sim[key] == provider_block[key], f"{key} 在两条链路上不一致"
    # provider 走的是非 dry_run（03 的真实路径），落库收尾后再结束用例
    assert await flush_decision_writes() is True


# ============================================================
# 名单直通的接口面（§3.1 的 `list_hit` 对象契约）
# ============================================================
@pytest.mark.parametrize("list_type,decision", [("black", "reject"), ("white", "pass")])
async def test_list_direct_through_uses_the_object_contract(client, list_type, decision):
    """名单直通的响应：`list_hit` 是**对象**、`hits=[]`、`rule_score=0`。

    这条同时钉住契约裁定（`list_hit` 任何情况下都不是 bool）与 BR-05-02/03
    （直通时**不求值任何规则**）。为了让"不求值"可判别，这里配了一条
    "必然命中"的高分规则：若直通路径还去求值，分数一定不为 0。

    前端在 `list_hit.hit === true` 时**不展示分值**、改为「白名单直通放行」/
    「黑名单直通拦截」并隐藏明细表（Spec §2.1），因此这三个值必须同时成立。
    """
    await _prepare_ip()
    await install_rules(
        rule_doc("RMUSTHIT", leaf("login_cnt_1h", "gte", 1), score=100,
                 scene_code="login"),
    )
    await install_lists(list_doc(list_type, "user", "U000001"))

    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": True},
                          headers=WRITER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["decision"] == decision
    assert data["list_hit"] == {"hit": True, "list_type": list_type,
                                "entity_type": "user", "entity_value": "U000001"}
    assert data["hits"] == [] and data["hit_rule_count"] == 0
    assert data["rule_score"] == 0 and data["final_score"] == 0
    assert data["rule_versions"] == {}, "没有规则参与，版本快照必须是空的"
    assert data["degraded"] is False


async def test_list_direct_through_is_persisted_with_the_object(client):
    """直通决策同样落库（E03 的 `list_hit` 也是对象）——08 要据此建案/放行留痕。"""
    await _prepare_ip()
    await install_lists(list_doc("black", "user", "U000001", entry_id="LBAPI001"))

    r = await client.post(URL, json={"event": LOGIN_EVENT, "dry_run": False},
                          headers=WRITER)
    assert r.status_code == 200, r.text
    assert await flush_decision_writes() is True
    doc = await db.get_db()[COLL_DECISIONS].find_one({})
    assert doc is not None
    assert doc["decision"] == "reject"
    assert doc["list_hit"]["hit"] is True and doc["list_hit"]["list_type"] == "black"
    assert doc["rule_score"] == 0 and doc["hit_rule_count"] == 0


# ============================================================
# 路由登记与 OpenAPI
# ============================================================
async def test_endpoint_is_registered_and_documented():
    """端点必须登记在 OpenAPI 里（`scripts/self_audit.py` 会逐条核对）。"""
    paths = app.openapi().get("paths", {})
    assert "/api/v1/engine/evaluate" in paths
    assert "post" in paths["/api/v1/engine/evaluate"]
    # 模块编号只登记一次（重复登记会在导入时抛错，这里顺带确认没被登记两次）
    from app.api import MODULE_ROUTERS

    assert "05" in MODULE_ROUTERS
