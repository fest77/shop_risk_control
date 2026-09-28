# -*- coding: utf-8 -*-
"""模块 10「接口契约与错误处理」验收测试（Spec §3.1 ~ §3.5 / §5 / V-10-01~17）。

覆盖四个端点（`GET/POST /sim/cases`、`POST /sim/run`、`POST /sim/batch`、
`GET /sim/runs/{run_id}`）的字段形状、错误码、权限与审计。

## 这个文件与另外两个的分工

| 文件 | 覆盖 |
|---|---|
| `test_sim_consistency.py` | **与真实链路逐字段一致**（V-10-03 / BR-10-01~04） |
| `test_sim_isolation.py` | **数据隔离**（BR-10-05~09 / D66）——任务书 §2 的重点 |
| **本文件** | 接口契约、五个 `SIM-*` 错误码、权限、审计恰好一条、批量统计与可复现 |

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_sim_api.py -q
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import (
    COLL_AUDIT_LOGS,
    COLL_DECISIONS,
    COLL_IP_POOL,
    COLL_RISK_CASES,
    COLL_SEQ_COUNTERS,
    COLL_SIM_CASES,
    COLL_SIM_RUNS,
)
from app.engine.decision import flush as flush_decisions
from app.services import audit_service, feature_service
from app.services.sim_service import WINDOW_PREMISE
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER
from tests.engine_testlib import (
    branch,
    install_rule_scenes,
    install_rules,
    leaf,
    reset_engine_state,
    rule_doc,
)
from tests.event_testlib import component_reset, reset_runtime_state
from tests.sim_testlib import install_sim_cases

pytestmark = pytest.mark.anyio

CASES_URL = "/api/v1/sim/cases"
RUN_URL = "/api/v1/sim/run"
BATCH_URL = "/api/v1/sim/batch"
RUNS_URL = "/api/v1/sim/runs"
ENGINE_URL = "/api/v1/engine/evaluate"

#: 本用例集使用的探针 IP（预置 E12 画像行，理由见 `profile_inputs`）
PROBE_IP = "192.0.2.99"
PROBE_DEVICE = "DSIMAPI01"


@pytest.fixture(autouse=True)
async def api_state():
    """逐用例复位（理由见 `test_sim_consistency.py` 的同名夹具）。"""
    component_reset()
    reset_runtime_state()
    reset_engine_state()
    service = feature_service.get_feature_service()
    service.window.reset()
    service._profile_reader = None
    service._linked_provider = None
    # 逐用例复位的编号必须与逐用例复位的集合成对出现（否则跨用例重号）
    await db.get_db()[COLL_SEQ_COUNTERS].delete_many({})
    await db.get_db()[COLL_SIM_RUNS].delete_many({})
    await db.get_db()[COLL_SIM_CASES].delete_many({})
    yield
    await service.flush()
    await flush_decisions()
    service.window.reset()
    service._profile_reader = None
    service._linked_provider = None
    reset_engine_state()
    component_reset()
    await db.get_db()[COLL_SIM_RUNS].delete_many({})


@pytest.fixture
async def sim_inputs():
    """场景字典 + 一条 60 分的规则 + 探针 IP 的 E12 画像行 + 4 条演示用例。"""
    await install_rule_scenes("login", "coupon", "order", "pay", "aftersale", "common")
    await install_rules(rule_doc(
        "RLOGINAPI", branch("and", leaf("device_user_cnt", "gte", 5)),
        score=60, scene_code="login",
    ))
    await db.get_db()[COLL_IP_POOL].replace_one(
        {"_id": PROBE_IP},
        {"_id": PROBE_IP, "region": "测试", "isp": "测试", "is_proxy": False,
         "is_idc": False, "linked_user_cnt": 6, "first_seen_at": now_ms()},
        upsert=True,
    )
    return await install_sim_cases()


async def _install_profiles(device_cnt: int = 6) -> None:
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData(
        user_register_at=now_ms() - 2 * 86_400_000, user_level="normal",
        user_risk_tag_cnt=0, ip_is_proxy=False, address_aftersale_cnt=0,
    ))
    service._linked_provider = FakeLinkedUserCountProvider(
        {"device": device_cnt, "ip": device_cnt, "address": device_cnt}
    )


def login_event(**over) -> dict:
    event = {
        "event_type": "login",
        "user_id": "U-API-01",
        "device_id": PROBE_DEVICE,
        "ip": PROBE_IP,
        "scene_extra": {"login_type": "pwd", "success": True},
    }
    event.update(over)
    return event


def body_of(resp) -> dict:
    """统一响应包结构断言（与 06-A/06-B 的既有口径一致）。"""
    b = resp.json()
    assert set(b.keys()) == {"ok", "code", "message", "trace_id", "data"}, b
    assert b["ok"] is (b["code"] == "OK"), "ok 必须由 code 派生，不允许两处状态打架"
    assert isinstance(b["trace_id"], str) and b["trace_id"]
    return b


def data_of(resp) -> dict:
    return body_of(resp)["data"]


async def audit_rows(action: str) -> list[dict]:
    cursor = db.get_db()[COLL_AUDIT_LOGS].find({"action": action})
    return await cursor.to_list(length=1000)


# ============================================================
# §3.1 / §3.2 用例
# ============================================================
async def test_list_cases_contract(client, sim_inputs):
    """`GET /sim/cases`：Spec §3.1 的 8 个字段一个不少，且只列未归档。"""
    r = await client.get(CASES_URL, headers=READER)
    assert r.status_code == 200, r.text
    data = data_of(r)
    assert data["total"] == 4
    assert len(data["items"]) == 4
    expected_fields = {"case_id", "name", "category", "expected_decision",
                       "description", "event_template", "created_by", "created_at"}
    for item in data["items"]:
        assert expected_fields <= set(item), f"用例项缺字段：{expected_fields - set(item)}"
        # BR-10-17：模板必须是**完整事件体**（不是"部分字段"）
        assert item["event_template"].get("event_type"), item
        assert item["event_template"].get("user_id"), item
        assert item["category"] in (
            "coupon_abuse", "aftersale_abuse", "normal", "boundary", "other")
        assert item["expected_decision"] in ("pass", "review", "reject")

    # 已归档的用例不出现在列表里（BR-10-20 的软删语义）
    await db.get_db()[COLL_SIM_CASES].update_one(
        {"_id": sim_inputs[0]}, {"$set": {"status": "archived"}})
    r2 = await client.get(CASES_URL, headers=READER)
    assert data_of(r2)["total"] == 3


async def test_create_case_writes_exactly_one_audit(client, sim_inputs):
    """`POST /sim/cases`：**恰好一条**审计（D41），且落库字段与请求一致。"""
    payload = {
        "name": "接口测试用例",
        "category": "boundary",
        "expected_decision": "review",
        "description": "由 API 用例创建",
        "event_template": login_event(),
    }
    r = await client.post(CASES_URL, json=payload, headers=WRITER)
    assert r.status_code == 201, r.text
    data = data_of(r)
    assert data["case_id"].startswith("SIMC"), data
    assert isinstance(data["created_at"], int) and data["created_at"] > 0
    assert data["validation"]["valid"] is True, data["validation"]

    assert await audit_service.flush()
    rows = await audit_rows("sim.case.create")
    assert len(rows) == 1, f"sim.case.create 应恰好一条，实际 {len(rows)}"
    assert rows[0]["target_id"] == data["case_id"]
    assert rows[0]["actor"] == "strategy01", rows[0]["actor"]
    assert rows[0]["before"] is None
    assert rows[0]["after"]["name"] == payload["name"]

    doc = await db.get_db()[COLL_SIM_CASES].find_one({"_id": data["case_id"]})
    assert doc is not None
    assert doc["status"] == "active"
    assert doc["event_template"]["user_id"] == "U-API-01"
    assert doc["created_by"] == "strategy01"


async def test_create_case_duplicate_name_is_sim_4003(client, sim_inputs):
    """同名用例 → `409 SIM-4003`（Spec §5）。"""
    payload = {
        "name": "重名用例",
        "category": "other",
        "expected_decision": "pass",
        "event_template": login_event(),
    }
    first = await client.post(CASES_URL, json=payload, headers=WRITER)
    assert first.status_code == 201, first.text
    second = await client.post(CASES_URL, json=payload, headers=WRITER)
    assert second.status_code == 409, second.text
    assert body_of(second)["code"] == "SIM-4003"
    # 失败的那次**不得**留下审计（"恰好一条"包含"失败不留痕"这一半）
    assert await audit_service.flush()
    assert len(await audit_rows("sim.case.create")) == 1


async def test_create_case_rejects_bad_enum(client, sim_inputs):
    """`category` / `expected_decision` 越界 → `422 COM-4001`（字段级参数错误）。"""
    r = await client.post(CASES_URL, json={
        "name": "非法分类", "category": "not_a_category",
        "expected_decision": "review", "event_template": login_event(),
    }, headers=WRITER)
    assert r.status_code == 422, r.text
    assert body_of(r)["code"] == "COM-4001"

    r2 = await client.post(CASES_URL, json={
        "name": "非法预期", "category": "other",
        "expected_decision": "maybe", "event_template": login_event(),
    }, headers=WRITER)
    assert r2.status_code == 422, r2.text


async def test_create_case_reports_template_invalidity(client, sim_inputs):
    """保存一条**跑不通**的模板：仍然 201，但 `validation.valid=false` 并说明原因。

    为什么允许保存：用例可以是一个"待补全的草稿"（Spec 没有禁止），
    但页面必须**当场**知道它跑不通，而不是点开才发现。
    """
    r = await client.post(CASES_URL, json={
        "name": "缺字段的草稿", "category": "other", "expected_decision": "pass",
        # login 缺 device_id / ip（顶层）与 login_type（scene_extra）
        "event_template": {"event_type": "login", "user_id": "U-API-01"},
    }, headers=WRITER)
    assert r.status_code == 201, r.text
    validation = data_of(r)["validation"]
    assert validation["valid"] is False
    assert validation["source_code"] == "EVT-4004"
    assert set(validation["missing"]) == {"device_id", "ip", "login_type"}, validation


# ============================================================
# §3.3 单条仿真执行
# ============================================================
async def test_run_contract_and_five_steps(client, sim_inputs):
    """V-10-01 / V-10-02：五步固定名字 + 每步有状态/结论/耗时（BR-10-10）。"""
    await _install_profiles()
    case_id = sim_inputs[3]      # 代理 IP 聚集登录（expected=review）
    r = await client.post(RUN_URL, json={"event": login_event(), "case_id": case_id},
                          headers=READER)
    assert r.status_code == 200, r.text
    data = data_of(r)

    # Spec §3.3 的冻结字段（逐个点名，缺一个都算契约破坏）
    for field in ("run_id", "steps", "features", "missing_features", "list_hit",
                  "hits", "rule_score", "final_score", "risk_level", "decision",
                  "expected_decision", "matched_expected", "elapsed_ms", "dry_run"):
        assert field in data, f"响应 data 缺字段 {field}"
    assert data["run_id"].startswith("SIMR")
    assert data["dry_run"] is True, "Spec §3.3：dry_run 恒 true"
    # BR-10-04：结论基于哪一版规则
    assert "RLOGINAPI" in data["rule_versions"]

    steps = data["steps"]
    assert [s["name"] for s in steps] == [
        "event_validate", "feature_extract", "list_filter", "rule_evaluate", "arbitrate",
    ], "Spec §3.3：steps[].name 取值固定"
    for index, step in enumerate(steps, start=1):
        assert step["seq"] == index
        assert step["status"] in ("ok", "failed", "skipped"), step
        assert isinstance(step["detail"], str) and step["detail"], step
        assert isinstance(step["elapsed_ms"], int) and step["elapsed_ms"] >= 0, step
        assert isinstance(step["payload"], dict), step

    # 预期比对（用例的 expected_decision = review，实际也是 review）
    assert data["expected_decision"] == "review"
    assert data["matched_expected"] is True
    assert data["decision"] == "review"
    assert data["rule_score"] == 60

    # BR-10-12：总耗时与各步之和的差值可解释
    timing = data["timing"]
    assert timing["total_ms"] >= 0
    assert timing["orchestration_ms"] == max(
        0, timing["total_ms"] - timing["steps_sum_ms"])
    assert isinstance(timing["over_threshold"], bool)

    # BR-10-07：必须如实声明"特征基于当前真实窗口"
    assert data["window_premise"] == WINDOW_PREMISE
    assert data["affects_window"] is False


async def test_run_event_invalid_is_sim_4001_with_evt_source(client, sim_inputs):
    """V-10-15：步骤 1 红、步骤 2~5 显示"未执行"，且错误码带 03 的原码与缺失字段。

    `missing` 的期望值包含 `login_type`：`login` 的 `scene_extra` **必填键**就是
    `login_type`，一次 `EVT-4004` 会把"顶层必填缺什么"与"`scene_extra` 必填缺什么"
    **一起列出来**——这正是页面需要的粒度（它要一次把所有红框标上，
    而不是让用户修一个、再发现下一个）。
    """
    r = await client.post(RUN_URL, json={
        # 缺 device_id / ip（顶层）与 login_type（scene_extra）
        "event": {"event_type": "login", "user_id": "U-API-01"},
    }, headers=READER)
    assert r.status_code == 400, r.text
    body = body_of(r)
    assert body["code"] == "SIM-4001"
    assert body["data"]["source_code"] == "EVT-4004", body["data"]
    assert set(body["data"]["missing"]) == {"device_id", "ip", "login_type"}
    # Spec §5 的 `SIM-5001` 处置：**不返回任何决策结论**
    assert "decision" not in body["data"] or not body["data"].get("decision")

    # 这次失败**仍然留了一条 sim_runs**（Spec BR-10-08 的反面：不写业务库，
    # 但执行记录要留，否则"我点了但什么都没发生"无从排查）
    docs = await db.get_db()[COLL_SIM_RUNS].find({}).to_list(length=10)
    assert len(docs) == 1, docs
    assert docs[0]["status"] == "failed"
    assert docs[0]["final_decision"] == ""
    steps = docs[0]["trace"]["steps"]
    assert steps[0]["name"] == "event_validate" and steps[0]["status"] == "failed"
    assert [s["status"] for s in steps[1:]] == ["skipped"] * 4, steps


async def test_run_scene_extra_invalid_json_is_sim_4002(client, sim_inputs):
    """V-10-12 / SIM-4002：`scene_extra` 是非法 JSON 字符串 → `422`。"""
    r = await client.post(RUN_URL, json={
        "event": {**login_event(), "scene_extra": "{bad"},
    }, headers=READER)
    assert r.status_code == 422, r.text
    body = body_of(r)
    assert body["code"] == "SIM-4002"
    assert body["data"]["field"] == "scene_extra"


async def test_run_accepts_scene_extra_as_json_string(client, sim_inputs):
    """`scene_extra` 传**合法 JSON 字符串**时按对象处理（仿真页表单的形态）。"""
    await _install_profiles()
    event = login_event()
    event["scene_extra"] = '{"login_type": "pwd", "success": true}'
    r = await client.post(RUN_URL, json={"event": event}, headers=READER)
    assert r.status_code == 200, r.text
    data = data_of(r)
    assert data["features"]["user_level"] == "normal"
    assert data["decision"] == "review"


async def test_run_event_type_mismatch_matches_real_gateway(client, sim_inputs):
    """任务书 §3 的核心要求：**仿真不放宽任何校验**。

    同一份坏报文（`coupon_receive` 的 `scene_extra` 里塞了 `login_type`）经
    仿真与经 `POST /events` 必须得到**同一个错误码**——否则会出现
    "仿真通过、真实入口 422"，一致性当场作废。
    """
    bad = {
        "event_type": "coupon_receive",
        "user_id": "U-API-01",
        "device_id": PROBE_DEVICE,
        "ip": PROBE_IP,
        "amount": 5000,
        # `login_type` 不属于 `coupon_receive`（EVT-4006 的"串味"）
        "scene_extra": {"login_type": "pwd", "coupon_id": "C1", "activity_id": "A1",
                        "face_value": 5000},
    }
    sim = await client.post(RUN_URL, json={"event": bad}, headers=READER)
    real = await client.post("/api/v1/events", json=bad, headers=WRITER)
    assert sim.status_code == 400, sim.text
    assert real.status_code == 400, real.text
    assert body_of(sim)["data"]["source_code"] == "EVT-4006"
    assert body_of(real)["code"] == "EVT-4006"
    # 两个响应的 code 体系不同（SIM-4001 是仿真页的结论码），但**都指向同一个
    # 03 原码**——这正是 ER-02「引用别人的码保持原前缀」的落点
    assert body_of(real)["code"] == body_of(sim)["data"]["source_code"]


async def test_run_engine_unavailable_is_sim_5001_without_conclusion(
    client, sim_inputs, monkeypatch
):
    """V-10-13 / SIM-5001：引擎降级时 `503` 且**绝不返回任何决策结论**。

    注入方式：让规则集读不出来（`RuleRepo.list_enabled_rules` 抛异常），
    走 05 的 `RUL-5002`（fail-closed）分支。Spec §5 的原话是
    「503 且**未返回任何决策结论**」「**不得降级为"用简化逻辑算一下"**」。
    """
    from app.repos.rule_repo import RuleRepo

    async def boom(self, scene_code):  # noqa: ANN001
        raise RuntimeError("simulated mongo failure on rules")

    monkeypatch.setattr(RuleRepo, "list_enabled_rules", boom)
    r = await client.post(RUN_URL, json={"event": login_event()}, headers=READER)
    assert r.status_code == 503, r.text
    body = body_of(r)
    assert body["code"] == "SIM-5001"
    # 不得有任何"看起来像结论"的字段
    assert body["data"].get("conclusion_available") is False
    assert "decision" not in body["data"]
    assert "rule_score" not in body["data"]
    assert body["data"]["degraded"] is True
    assert body["data"]["source_code"] == "RUL-5002"
    assert body["data"]["run_id"], "降级也必须留下 run_id（否则这次执行无痕）"

    # 记录如实写成 degraded，且 final_decision 为空（不是 review）
    doc = await db.get_db()[COLL_SIM_RUNS].find_one({"_id": body["data"]["run_id"]})
    assert doc is not None
    assert doc["degraded"] is True
    assert doc["final_decision"] == "", doc["final_decision"]
    # 且**没有**写业务库
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 0
    assert await db.get_db()[COLL_RISK_CASES].count_documents({}) == 0


async def test_run_timeout_is_sim_5003(client, sim_inputs, monkeypatch):
    """`SIM-5003`：超过 5s 预算 → `504`，且不返回半个结论。

    注入方式：把墙钟预算改成 0（等价于"必然超时"）——比真的 sleep 5 秒快得多，
    而它验的正是"超时会翻成 `SIM-5003` 而不是 500"这条**契约**。
    """
    import app.api.sim_api as sim_api

    monkeypatch.setattr(sim_api, "SIM_TIMEOUT_MS", 0)
    r = await client.post(RUN_URL, json={"event": login_event()}, headers=READER)
    assert r.status_code == 504, r.text
    body = body_of(r)
    assert body["code"] == "SIM-5003"
    assert body["data"]["conclusion_available"] is False


async def test_batch_limit_is_sim_4005(client, sim_inputs):
    """V-10-11 / BR-10-19：`repeat=500` → `422 SIM-4005`，**不静默截断**。"""
    r = await client.post(BATCH_URL, json={
        "case_id": sim_inputs[0], "repeat": 500, "seed": 42,
    }, headers=READER)
    assert r.status_code == 422, r.text
    body = body_of(r)
    assert body["code"] == "SIM-4005"
    assert body["data"]["limit"] == 200


async def test_run_not_found_is_sim_4004(client, sim_inputs):
    """V-10-17 / SIM-4004：查不到的 `run_id` → `404`。"""
    r = await client.get(f"{RUNS_URL}/SIMR20260101000000000099", headers=READER)
    assert r.status_code == 404, r.text
    assert body_of(r)["code"] == "SIM-4004"


# ============================================================
# §3.5 记录可回看（BR-10-13）
# ============================================================
async def test_run_detail_round_trip(client, sim_inputs):
    """BR-10-13：用 `run_id` 回看，链路与保存时**逐字段一致**。"""
    await _install_profiles()
    created = await client.post(RUN_URL, json={"event": login_event()}, headers=READER)
    assert created.status_code == 200, created.text
    original = data_of(created)

    fetched = await client.get(f"{RUNS_URL}/{original['run_id']}", headers=READER)
    assert fetched.status_code == 200, fetched.text
    replayed = data_of(fetched)

    for field in ("run_id", "steps", "features", "missing_features", "list_hit",
                  "hits", "rule_score", "final_score", "risk_level", "decision",
                  "expected_decision", "matched_expected", "elapsed_ms",
                  "dry_run", "rule_versions", "engine_version", "timing"):
        assert replayed[field] == original[field], f"回看的 {field} 与保存时不一致"
    assert replayed["status"] == "ok"
    assert replayed["run_by"] == "reviewer01"
    assert isinstance(replayed["run_at"], int)


# ============================================================
# 批量回放（§3.4 / V-10-09 / V-10-10）
# ============================================================
async def test_batch_returns_statistics_and_is_reproducible(client, sim_inputs):
    """V-10-09 + V-10-10：给出匹配/不符/误伤统计，且**同一 seed 两次结果完全一致**。"""
    await _install_profiles()
    # "正常下单支付"用例预期 pass
    case_id = sim_inputs[2]

    first = await client.post(BATCH_URL, json={
        "case_id": case_id, "repeat": 8, "seed": 42,
    }, headers=READER)
    assert first.status_code == 200, first.text
    d1 = data_of(first)
    for field in ("total", "matched", "mismatched", "false_positive",
                  "mismatch_samples", "elapsed_ms"):
        assert field in d1, f"批量响应缺字段 {field}"
    assert d1["total"] == 8
    assert d1["matched"] + d1["mismatched"] + d1["failed"] == d1["total"], (
        "总条数必须等于匹配 + 不符 + 失败（账要对得上）")
    assert d1["expected_decision"] == "pass"
    assert d1["affects_window"] is False, "D11：批量回放强制 affect_window=false"

    second = await client.post(BATCH_URL, json={
        "case_id": case_id, "repeat": 8, "seed": 42,
    }, headers=READER)
    d2 = data_of(second)
    # BR-10-18：同一 seed + 同一用例 → **统计数字完全一致**
    for field in ("total", "matched", "mismatched", "false_positive"):
        assert d1[field] == d2[field], (
            f"同一 seed 两次的 {field} 不一致：{d1[field]} vs {d2[field]}")
    assert d1["decision_counts"] == d2["decision_counts"], (
        d1["decision_counts"], d2["decision_counts"])
    # `run_id` 必须不同（每次执行是**一次新的执行**，可复现的是统计而不是编号）
    assert set(d1["run_ids"]).isdisjoint(d2["run_ids"])
    assert len(d1["run_ids"]) == 8


async def test_batch_different_seed_may_differ(client, sim_inputs):
    """不同 seed 允许产生不同的事件序列（否则 `seed` 参数没有意义）。

    这条不是为了断言"一定不同"（金额扰动恰好可能落在同一档），
    而是为了确认**换 seed 不会报错**、且统计仍然自洽——一条"换 seed 就崩"
    的实现在演示时最尴尬。
    """
    await _install_profiles()
    case_id = sim_inputs[2]
    r = await client.post(BATCH_URL, json={
        "case_id": case_id, "repeat": 5, "seed": 7,
    }, headers=READER)
    assert r.status_code == 200, r.text
    data = data_of(r)
    assert data["seed"] == 7
    assert data["matched"] + data["mismatched"] + data["failed"] == data["total"]


async def test_batch_default_repeat_is_twenty(client, sim_inputs):
    """BR-10-19：默认条数 20（Spec §3.4 的请求示例就是 `repeat: 20`）。"""
    await _install_profiles()
    r = await client.post(BATCH_URL, json={"case_id": sim_inputs[2]}, headers=READER)
    assert r.status_code == 200, r.text
    assert data_of(r)["total"] == 20


async def test_batch_writes_exactly_one_aggregate_audit(client, sim_inputs):
    """批量回放写**恰好一条**聚合审计（`sim.batch`），而不是逐条写。"""
    await _install_profiles()
    r = await client.post(BATCH_URL, json={
        "case_id": sim_inputs[2], "repeat": 3, "seed": 5,
    }, headers=READER)
    assert r.status_code == 200, r.text
    assert await audit_service.flush()
    rows = await audit_rows("sim.batch")
    assert len(rows) == 1, f"sim.batch 应恰好一条，实际 {len(rows)}"
    assert rows[0]["after"]["repeat"] == 3
    assert rows[0]["after"]["total"] == 3
    assert rows[0]["after"]["affects_window"] is False
    # 3 条执行记录 + 3 条 `sim.run` 审计（每次单条仿真各有自己的 sim.run）
    assert await db.get_db()[COLL_SIM_RUNS].count_documents({}) == 3
    assert len(await audit_rows("sim.run")) == 3


async def test_batch_without_case_or_event_is_rejected(client, sim_inputs):
    """既没有 `case_id` 也没有 `event` → `400 SIM-4001`（而不是跑一条空事件）。"""
    r = await client.post(BATCH_URL, json={"repeat": 3}, headers=READER)
    assert r.status_code == 400, r.text
    assert body_of(r)["code"] == "SIM-4001"


# ============================================================
# 权限（V-10-16 / BR-10-21）
# ============================================================
@pytest.mark.parametrize("method,url,payload", [
    ("get", CASES_URL, None),
    ("post", CASES_URL, {"name": "x1", "category": "other",
                         "expected_decision": "pass", "event_template": {}}),
    ("post", RUN_URL, {"event": {"event_type": "login", "user_id": "U-1"}}),
    ("post", BATCH_URL, {"repeat": 2}),
    ("get", f"{RUNS_URL}/SIMR20260101000000000001", None),
])
async def test_admin_has_no_sim_permission(client, sim_inputs, method, url, payload):
    """V-10-16 / BR-10-21：`admin` **没有** `sim:run`，五个端点全部 403。"""
    kwargs = {"headers": ADMIN}
    if payload is not None:
        kwargs["json"] = payload
    r = await getattr(client, method)(url, **kwargs)
    assert r.status_code == 403, f"{method.upper()} {url} 应 403，实际 {r.status_code}"
    assert body_of(r)["code"] == "AUTH-4020"


async def test_reviewer_can_use_all_five_endpoints(client, sim_inputs):
    """BR-10-21 的另一半：**reviewer 可以用**（矩阵里 `sim:run` 的持有者之一）。"""
    await _install_profiles()
    assert (await client.get(CASES_URL, headers=READER)).status_code == 200
    assert (await client.post(CASES_URL, json={
        "name": "审核员保存的用例", "category": "other",
        "expected_decision": "pass", "event_template": login_event(),
    }, headers=READER)).status_code == 201
    run = await client.post(RUN_URL, json={"event": login_event()}, headers=READER)
    assert run.status_code == 200, run.text
    run_id = data_of(run)["run_id"]
    assert (await client.get(f"{RUNS_URL}/{run_id}", headers=READER)).status_code == 200
    assert (await client.post(BATCH_URL, json={
        "case_id": sim_inputs[2], "repeat": 2}, headers=READER)).status_code == 200


async def test_unauthenticated_is_401(client, sim_inputs):
    """未带令牌 → `401`（鉴权中间件拦在最前面，不进业务代码）。"""
    r = await client.get(CASES_URL)
    assert r.status_code == 401, r.text
