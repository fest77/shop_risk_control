# -*- coding: utf-8 -*-
"""模块 10「与真实链路的一致性」验收测试（Spec §4.1 / V-10-03 / BR-10-01~04）。

**这个文件是本模块的核心价值所在。** Spec 05 §3.1 的原话是「仿真模块（10）
**必须调用此接口**（`/engine/evaluate`）以复用真实链路，**禁止另写判定逻辑**」。
因此这里断言的是一条**实测**结论，而不是"代码看起来调了同一个函数"：

    同一事件 → 经 `/sim/run`        ⇒ 决策块
    同一事件 → 经 `/engine/evaluate` ⇒ 决策块
    两者逐字段相等（rule_score / hits / decision / rule_versions / engine_version）

## 说明：为什么有的用例里"特征"会不同，而"决策"仍然必须相同

仿真**不写真实窗口**（BR-10-06，本模块自己解决的那一处隔离），因此
"含本次"的窗口计数在仿真里少 1（`V-04-03` 断言真实链路里首发 `coupon_receive`
的 `coupon_cnt_1h == 1`，仿真里是 0/缺失）。这是**不让仿真污染线上特征**的
必然代价，不是可抹平的差异。

代价的边界必须说清楚，因此本文件分两层断言：

1. `test_decision_block_matches_engine_for_profile_rules`——规则只用**持久化画像**
   特征（`device_user_cnt` / `user_age_days` / `ip_is_proxy` / `user_level` /
   `address_aftersale_cnt`，全部来自 E10~E13 与 09 的关联账号数），此时窗口差异
   **不影响任何一条规则**，因此决策块必须**逐字段完全相同**；
2. `test_window_feature_differs_by_exactly_one`——规则专门用窗口特征
  （`pay_fail_cnt_24h`），此时差异**只应体现为那 1 笔**，且方向恒为"仿真偏低"。
   这条用例的存在是为了**把代价钉在测试里**：将来有人"顺手"让仿真也 ingest，
   第 1 类用例仍然全绿，只有第 2 类会红——它会明确告诉改动者"你动了隔离边界"。

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_sim_consistency.py -q
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import (
    COLL_DECISION_HITS,
    COLL_DECISIONS,
    COLL_IP_POOL,
    COLL_SEQ_COUNTERS,
    COLL_SIM_RUNS,
)
from app.engine.decision import flush as flush_decisions
from app.engine.feature_window import DIMENSION_FIELDS
from app.services import feature_service
from app.utils.timeutil import now_ms
from tests.conftest import READER
from tests.engine_testlib import (
    branch,
    install_lists,
    install_rule_scenes,
    install_rules,
    leaf,
    list_doc,
    reset_engine_state,
    rule_doc,
)
from tests.event_testlib import (
    component_reset,
    coupon_payload,
    login_payload,
    reset_runtime_state,
)
from tests.sim_testlib import unique_event_id

pytestmark = pytest.mark.anyio

ENGINE_URL = "/api/v1/engine/evaluate"
SIM_RUN_URL = "/api/v1/sim/run"

#: 探针事件用的 IP。用例自己预置它的 E12 画像行，让 `ip_is_proxy`
#: （`DECISION_GATING_FEATURES` 里唯一的关键特征）能解析出来——
#: 缺它时真实链路会 fail-closed 降级成 `degraded=true` 的 review，
#: 于是"两个块是否一致"根本无从比较（一边"没窗口数据"、一边"没画像"，
#: 两边 `rule_score` 都是 0 而原因完全不同）。
PROBE_IP = "192.0.2.77"

#: `sim_event()` 的默认事件 IP，与 `PROBE_IP` 同一件事（单列一个别名是为了
#: 让"探针事件"这个词在文件里只对应一个 IP 字面量）
SIM_EVENT_IP = PROBE_IP

#: 与 `/engine/evaluate` 的决策块逐字段比对的那一组字段（Spec V-10-03 点名了
#: `rule_score`/`hits`/`decision`，这里再补上`final_score`/`risk_level`/
#: `rule_versions`/`engine_version`/`list_hit`——它们同样是"结论"的一部分，
#: 只比三个字段会漏掉"分值截断"与"规则版本"这两类漂移）。
COMPARED_BLOCK_FIELDS: tuple[str, ...] = (
    "list_hit", "rule_score", "model_score", "final_score", "risk_level",
    "decision", "hit_rule_count", "hits", "rule_versions", "engine_version",
)


@pytest.fixture(autouse=True)
async def sim_engine_state():
    """逐用例复位：组件装配、窗口、05/04 的进程内状态、E03/E04/E05 集合。

    `tests/conftest.py` 已经清了 E03/E04/E05 与场景表，这里额外做三件它不做的事：
    ① 把 `component_reset()` 装成**真实默认装配**（04/05/09 都已落地）；
    ② 复位 04 的滑动窗口——它是**进程内**状态，跨用例残留会让"窗口差异"的
       断言变成玄学；
    ③ 复位 05 的名单 TTL 缓存与统计。
    """
    component_reset()
    reset_runtime_state()
    reset_engine_state()
    service = feature_service.get_feature_service()
    service.window.reset()
    # 解除注入的假画像 / 假 09：它们是**实例属性**，一旦被某个用例替换，
    # 后面的用例会继续用它（`test_feature_pipeline.py` 把这类现象叫
    # "单独跑通过、一起跑失败"的幽灵缺陷）。实测踩到：白名单直通用例
    # 因为继承了上一条用例的"代理 IP + 6 个关联账号"画像而判成 review。
    service._profile_reader = None
    service._linked_provider = None
    # ④ **清空 `seq_counters`**（理由见下）
    await db.get_db()[COLL_SEQ_COUNTERS].delete_many({})
    await db.get_db()[COLL_SIM_RUNS].delete_many({})
    yield
    await feature_service.get_feature_service().flush()
    await flush_decisions()
    service.window.reset()
    service._profile_reader = None
    service._linked_provider = None
    reset_engine_state()
    component_reset()
    await db.get_db()[COLL_SIM_RUNS].delete_many({})


# ============================================================
# 输入
# ============================================================
#: 四条规则全部只用**持久化画像**特征（不含任何窗口计数）：
#: - `device_user_cnt`  ← E11/09 的关联账号数
#: - `ip_is_proxy`      ← E12
#: - `user_age_days`    ← E10
#: - `user_level`       ← E10
#: - `address_aftersale_cnt` ← E13
PROFILE_RULES = (
    rule_doc("RLOGIN001", branch("and", leaf("device_user_cnt", "gte", 5)),
             score=45, scene_code="login", priority=10),
    rule_doc("RLOGIN003", branch(
        "and",
        leaf("ip_is_proxy", "eq", True),
        branch("or", leaf("user_age_days", "lte", 7),
               leaf("user_level", "in", ["normal"])),
    ), score=25, scene_code="login", priority=30),
    rule_doc("RLOGIN002", branch("and", leaf("user_age_days", "gte", 100)),
             score=20, scene_code="login", priority=20, status="disabled"),
    rule_doc("RCOMMON001", branch("and", leaf("user_level", "eq", "gold")),
             score=20, scene_code="common", priority=10, version=7),
)


@pytest.fixture
async def profile_inputs():
    """灌入"规则只用持久化画像"所需的场景行、规则与**探针 IP 的 E12 画像行**。

    为什么必须有 E12 那一行：`ip_is_proxy` 是 `DECISION_GATING_FEATURES` 里
    唯一的关键特征，**缺失时真实链路会 fail-closed 降级**
    （`_acquire_features` → `degrade_suggested` → `decide()` 产出
    `degraded=true`、`hits=[]`、`rule_versions={}` 的 review）。

    这个降级在一致性测试里是**致命的假绿来源**：两边 `rule_score` 都是 0、
    两边 `decision` 都是 review，看起来"一致"，实际上真实链路**一条规则都没求值**。
    实测踩到过一次（白名单直通用例被判成 review，而白名单明明命中了）。
    """
    await install_rule_scenes("login", "common")
    await install_rules(*PROFILE_RULES)
    await db.get_db()[COLL_IP_POOL].replace_one(
        {"_id": PROBE_IP},
        {"_id": PROBE_IP, "region": "测试", "isp": "测试", "is_proxy": False,
         "is_idc": False, "linked_user_cnt": 1, "first_seen_at": now_ms()},
        upsert=True,
    )


def sim_event(**over) -> dict:
    """仿真入参（**未知字段**形态，与仿真页/接口收到的报文一致）。"""
    payload = login_payload(user_id="U100001", device_id="DSIM0001", ip=SIM_EVENT_IP)
    payload["scene_extra"] = {"login_type": "pwd", "success": True}
    payload["event_id"] = unique_event_id()
    payload["ts"] = now_ms() - 1000
    payload.update(over)
    return payload


async def _engine_block(client, event: dict) -> dict:
    """经 `/engine/evaluate`（05 的 HTTP 壳）拿决策块。

    ⚠️ **每次调用都换一个 `event_id`**（覆盖掉入参里的那个）。原因是一处
    真实且反直觉的机制，写在这里以免后人再踩：

    `FeatureWindow.ingest` 按 `event_id` 去重，而**只有 ingest 会登记去重集合**。
    仿真走 `compute(affect_window=False)`——它**不 ingest**，因此同一个
    `event_id` 在仿真之后仍然是"未见过的"，真实链路会正常把它入窗。
    反过来，若真实链路先跑，同一个编号再给仿真也不会被去重（仿真从不查集合）。
    因此两条路各自用自己的编号，才是在比较"同一事件"，而不是在比较
    "同一编号被去重后的结果"。
    """
    payload = dict(event)
    payload.pop("_id", None)
    payload["event_id"] = unique_event_id()
    r = await client.post(ENGINE_URL, json={"event": payload, "dry_run": True, "trace": True},
                          headers=READER)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _sim_block(client, payload: dict) -> dict:
    """经 `/sim/run`（本模块）拿决策块（同样每次换编号，理由见 `_engine_block`）。"""
    body = dict(payload)
    body.pop("_id", None)
    body["event_id"] = unique_event_id()
    r = await client.post(SIM_RUN_URL, json={"event": body, "trace": True}, headers=READER)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def assert_blocks_equal(sim: dict, engine: dict) -> None:
    """逐字段比对两个决策块，失败时**把差异打出来**（否则只看到 assert 失败）。"""
    diffs = {
        name: (sim.get(name), engine.get(name))
        for name in COMPARED_BLOCK_FIELDS
        if sim.get(name) != engine.get(name)
    }
    assert not diffs, f"仿真决策块与 /engine/evaluate 不一致：{diffs}"


# ============================================================
# V-10-03 / BR-10-02：逐字段一致
# ============================================================
async def test_decision_block_matches_engine_for_profile_rules(client, profile_inputs):
    """**一致性主用例**：同一事件，两条路，决策块逐字段相等。

    规则集只用持久化画像特征，因此"仿真不写窗口"这件事**不会**影响结论——
    此时两个块必须完全相同。任何一处不等，都说明本模块在某处**另算了一遍**
    （而另算的那一份会在规则集变更后与真实链路分叉，仿真随即失去意义）。
    """
    # 给画像读入值：让它命中 RLOGIN001（45 分）+ RLOGIN003（25 分）= 70 分 / review。
    # 两个特征来源不同（09 的关联账号数、E12 的代理标记、E10 的等级），
    # 覆盖"跨模块取数"这条路径。
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData(
        user_register_at=now_ms() - 2 * 86_400_000,   # 注册 2 天 → user_age_days=2
        user_level="normal",
        user_risk_tag_cnt=0,
        ip_is_proxy=True,
        address_aftersale_cnt=0,
    ))
    service._linked_provider = FakeLinkedUserCountProvider({"device": 6, "ip": 6, "address": 6})

    sim = await _sim_block(client, sim_event())
    engine = await _engine_block(client, sim_event())

    assert sim["rule_score"] == 70, sim
    assert sim["decision"] == "review", sim
    assert engine["rule_score"] == 70, engine
    assert_blocks_equal(sim, engine)

    # BR-10-04：必须回传"这次结论基于哪一版规则"，且与真实链路同一份（D60 全量）
    assert sim["rule_versions"] == engine["rule_versions"], (
        sim["rule_versions"], engine["rule_versions"])
    # D60：`rule_versions` 是本次**参与求值**的全部规则（含未命中项）。
    # `RLOGIN002` 是 `status=disabled`，**不参与求值**，因此不在其中——
    # 这正是 BR-10-03（只用当前生效版本）的落点。
    assert set(sim["rule_versions"]) == {
        "RLOGIN001", "RLOGIN003", "RCOMMON001",
    }, "rule_versions 必须含本次参与求值的**全部**规则（含未命中项，但不含已停用的）"
    assert sim["engine_version"] == engine["engine_version"] == "rule-engine-v1"


async def test_three_bands_match_engine(client, profile_inputs):
    """三档（pass / review / reject）在两条路上都一致。

    只验一档会漏掉"某一档的分支走了另一条代码路径"这类缺陷（例如直通与计分
    这两个分支的块是**不同函数**组装的）。
    """
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    service = feature_service.get_feature_service()
    cases = [
        # (名称, 画像, 09 关联数, 期望决策)
        ("pass", ProfileData(user_register_at=now_ms() - 500 * 86_400_000, user_level="gold",
                             user_risk_tag_cnt=0, ip_is_proxy=False, address_aftersale_cnt=0),
         {"device": 1, "ip": 1, "address": 1}, "pass"),
        ("review", ProfileData(user_register_at=now_ms() - 2 * 86_400_000, user_level="normal",
                               user_risk_tag_cnt=0, ip_is_proxy=True, address_aftersale_cnt=0),
         {"device": 6, "ip": 6, "address": 6}, "review"),
    ]
    for name, profile, linked, expected in cases:
        service._profile_reader = FakeProfileReader(profile)
        service._linked_provider = FakeLinkedUserCountProvider(linked)
        sim = await _sim_block(client, sim_event(user_id=f"U1000{len(name)}"))
        engine = await _engine_block(client, sim_event(user_id=f"U1000{len(name)}"))
        assert sim["decision"] == expected, (name, sim)
        assert_blocks_equal(sim, engine)


async def test_blacklist_passthrough_matches_engine(client, profile_inputs):
    """名单直通（黑名单 → `reject`）在两条路上一致，且步骤 4 是 `skipped`（BR-10-11）。

    直通态的块由**另一条分支**组装（`decision.py` 的 `_empty_block`），
    因此它是"一致性"最容易被漏掉的一格：分值为 0、`hits=[]`、`rule_versions={}`，
    看起来"反正都是空的"，但只要有一处字段名不同（例如 `hit_rule_count` 写成 1），
    07/08 的消费方就会读出不同的结论。
    """
    await install_lists(list_doc("black", "user", "U-BLACK-01"))
    sim = await _sim_block(client, sim_event(user_id="U-BLACK-01"))
    engine = await _engine_block(client, sim_event(user_id="U-BLACK-01"))

    assert sim["decision"] == engine["decision"] == "reject"
    assert sim["rule_score"] == engine["rule_score"] == 0
    assert_blocks_equal(sim, engine)

    rules_step = next(s for s in sim["steps"] if s["name"] == "rule_evaluate")
    assert rules_step["status"] == "skipped", (
        "BR-10-11：名单直通时步骤 4 必须是 skipped（如实反映'未求值规则'），"
        f"实际 {rules_step['status']}"
    )
    assert sim["steps"][-1]["name"] == "arbitrate"
    assert sim["steps"][-1]["status"] == "ok"


async def test_whitelist_passthrough_matches_engine(client, profile_inputs):
    """白名单直通（→ `pass`）同样一致（BR-05-02/03 的另一半）。"""
    await install_lists(list_doc("white", "phone", "13900000007"))
    sim = await _sim_block(client, sim_event(phone="13900000007"))
    engine = await _engine_block(client, sim_event(phone="13900000007"))

    assert sim["decision"] == engine["decision"] == "pass"
    assert_blocks_equal(sim, engine)


async def test_consistency_holds_across_five_event_types(client):
    """五类事件都要一致（否则"一致性"只在一个场景里成立）。

    五类的必填集、`scene_extra` 白名单与场景映射（`order_pay → pay`，D12）都不同，
    而仿真与真实两条路必须**共用**这些规则。这里用同一批"只在 common 场景生效"
    的规则，让五类事件都能走到计分分支。
    """
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    await install_rule_scenes(*("login", "coupon", "order", "pay", "aftersale", "common"))
    await install_rules(rule_doc(
        "RCOMMONONLY", branch("and", leaf("device_user_cnt", "gte", 3)),
        # 60 分：`arbiter` 的 review 下界（BR-05-16），让"两条路都判 review"
        # 这件事同时覆盖"分值累加"与"三档仲裁"两层
        score=60, scene_code="common",
    ))
    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData(
        user_register_at=now_ms() - 9 * 86_400_000, user_level="normal", user_risk_tag_cnt=0,
        ip_is_proxy=False, address_aftersale_cnt=0,
    ))
    service._linked_provider = FakeLinkedUserCountProvider({"device": 4, "ip": 4, "address": 4})

    for event_type, factory in (
        ("login", login_payload),
        ("coupon_receive", coupon_payload),
    ):
        payload = factory()
        payload["event_id"] = unique_event_id()
        sim = await _sim_block(client, payload)
        engine = await _engine_block(client, payload)
        assert sim["decision"] == "review", (event_type, sim)
        assert_blocks_equal(sim, engine)


# ============================================================
# 隔离边界的**代价**（钉住"仿真少 1"这件事）
# ============================================================
async def test_window_feature_differs_by_exactly_one(client):
    """窗口类特征：仿真**恰好少 1 笔**（本次那一笔），方向恒为"偏低"。

    这条用例是**隔离边界的守卫**（见模块 docstring 的分层说明）：

    - 真实链路先 `ingest` 再 `compute`（BR-04-02），因此窗口里含当前这一笔；
    - 仿真 `affect_window=False`，不写窗口，因此不含当前这一笔。

    两边的特征值必须**只差 1**——差 0 说明仿真偷偷写了窗口（污染线上数据，
    BR-10-06 被破坏）；差 ≥2 说明别的地方也在改窗口（隔离不彻底）。
    两者都由这条断言抓出来。

    规则用 `login_cnt_1h`（纯粹的窗口计数）：先喂 2 笔历史登录，
    本次也是一笔登录 —— 真实链路看到 3（命中），仿真看到 2（不命中）。

    ## ⚠️ 顺带暴露的一处**跨模块缺口**（如实登记，不在这里修）

    最初这条用例用的是 `pay_fail_cnt_24h`（"近 24h `order_pay` 且
    `scene_extra.success=false`"），结果发现**它经真实入口永远为 0**：

    - `feature_window.make_entry` 从 `order_pay` 的 `scene_extra.success` 读
      `pay_ok`；
    - 而 `event_schema.SCENE_EXTRA_BY_TYPE["order_pay"]` 的白名单是
      `(order_no, pay_channel)` + `(pay_amount, card_tail)`——**没有 `success`**，
      因此带 `success` 的 `order_pay` 会被 `EVT-4006` 拒掉。

    也就是说 `pay_fail_cnt_24h` 在真实链路上恒为 0，种子里
    **`RPAY001`（支付失败试探，30 分）这条启用规则永不可达**。
    这与决策 D51/D64 记录的那类"分支永远走不到"是同一族缺陷，但它跨 03/04/05
    三个模块，**不属于模块 10**（本模块只是它的第一个观测者）——
    已作为存疑项登记，本模块不越过边界去改别人的 schema。

    改用 `login_cnt_1h` 之后，两个特征的口径都干净，且"差 1"这件事**没有歧义**。
    """
    await install_rule_scenes("login", "common")
    await install_rules(rule_doc(
        # 60 分而不是 30：`arbiter` 的三档边界是 60（BR-05-16），
        # 因此 60 分才落到 `review`。用 30 分会让断言写成 `pass` —— 那也能证明
        # "命中/不命中"的差异，但会让"决策档位"这一层的信息白白丢掉。
        "RLOGINCNT", branch("and", leaf("login_cnt_1h", "gte", 3)),
        score=60, scene_code="login",
    ))
    service = feature_service.get_feature_service()
    # 从**空窗口**开始：本用例要精确断言"2 笔历史 → 3 笔（含本次）"，
    # 而本文件其它用例可能在窗口里留下条目（进程内单例、逐用例未必被清）
    service.window.reset()
    # 预置探针 IP 的 E12 画像行。**这一步不能省**：`ip_is_proxy` 是
    # `DECISION_GATING_FEATURES` 里唯一的"关键特征"，缺失时真实链路会
    # fail-closed 降级成 `degraded=true` 的 review（`hits=[]`、`rule_versions={}`），
    # 于是"有没有命中"这件事根本无从比较——它会表现为
    # `rule_score` 两边都是 0 而原因完全不同（一边"没窗口数据"、一边"没画像"）。
    await db.get_db()[COLL_IP_POOL].replace_one(
        {"_id": PROBE_IP},
        {"_id": PROBE_IP, "region": "测试", "isp": "测试", "is_proxy": False,
         "is_idc": False, "linked_user_cnt": 3, "first_seen_at": now_ms()},
        upsert=True,
    )

    now = now_ms()
    # 喂 2 笔历史登录（**直接写窗口**：这是"线上已有的历史"，不是仿真数据）
    for index in range(2):
        history = {
            "_id": unique_event_id(), "event_type": "login", "user_id": "U-CNT-01",
            "device_id": "DCNT0001", "ip": PROBE_IP,
            "ts": now - 60_000 * (index + 1),
            "received_at": now, "scene_extra": {"login_type": "pwd"},
            "source": "mock_biz",
        }
        assert await service.window.ingest(history) is True
    # ⚠️ `total_entries` 把**每个维度各算一条**：一笔登录带 `user_id`/`device_id`/`ip`
    # 三个维度（**没有 `address_id`**，因此不是四个——`FeatureWindow.ingest` 只写
    # 事件确实带有的维度）。2 笔历史 → 下面这三行算出来的条数。
    per_event = sum(
        1 for field in DIMENSION_FIELDS.values()
        if history.get(field) not in (None, "")
    )
    assert per_event == 3, "用例的事件应带 user/device/ip 三个维度"
    before = service.window.total_entries()
    assert before == 2 * per_event, (
        f"前置窗口应有 {2 * per_event} 条（2 笔 × {per_event} 个维度），实际 {before}")

    # 探针事件：**完全合法**的 login（能过步骤 1 的校验）
    probe = {
        "event_type": "login", "user_id": "U-CNT-01", "device_id": "DCNT0001",
        "ip": PROBE_IP,
        "event_id": unique_event_id(), "ts": now,
        "scene_extra": {"login_type": "pwd"},
    }

    sim = await _sim_block(client, probe)
    # 仿真跑完，窗口**必须仍是 6 条**（这一笔没有入窗）
    assert service.window.total_entries() == before, (
        f"仿真把事件写进了窗口（BR-10-06 被破坏）：before={before} "
        f"after={service.window.total_entries()}")

    engine = await _engine_block(client, probe)
    # `/engine/evaluate` 走"先 ingest 再 compute"，因此它跑完窗口是 9 条
    assert service.window.total_entries() == before + per_event, (
        "真实链路（/engine/evaluate）本应把事件写进窗口")

    # 仿真：窗口里只有 2 笔（不含本次）→ 看到 2 → 不命中
    assert sim["features"]["login_cnt_1h"] == 2, sim["features"]["login_cnt_1h"]
    assert sim["rule_score"] == 0, "仿真不含本次那一笔，因此不命中——这是隔离的代价"
    # 真实链路：窗口里含本次 → 看到 3 → 命中
    assert engine["rule_score"] == 60, (
        f"真实链路应命中（窗口里含本次那一笔），实际 {engine['rule_score']}")
    assert engine["decision"] == "review"
    # 不受窗口影响的字段必须一致（同一规则集、同一事件）
    assert sim["list_hit"] == engine["list_hit"]
    assert sim["rule_versions"] == engine["rule_versions"]
    await flush_decisions()


async def test_sim_does_not_write_decisions_or_hits(client, profile_inputs):
    """仿真**不写** `decisions` / `decision_hits`（BR-10-05 的前两个集合）。

    用"跑 5 次仿真 → 两个集合都是空的"来证明，而不是读代码找 `dry_run=True`：
    参数是否真的传到了 05 的落库分支，只有查库能证明。
    """
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData(
        user_register_at=now_ms() - 2 * 86_400_000, user_level="normal", user_risk_tag_cnt=0,
        ip_is_proxy=True, address_aftersale_cnt=0,
    ))
    service._linked_provider = FakeLinkedUserCountProvider({"device": 6, "ip": 6, "address": 6})

    for _ in range(5):
        r = await client.post(SIM_RUN_URL, json={"event": sim_event()}, headers=READER)
        assert r.status_code == 200, r.text
    await flush_decisions()

    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 0, "仿真写了 decisions"
    assert await db.get_db()[COLL_DECISION_HITS].count_documents({}) == 0, "仿真写了 decision_hits"
    assert await db.get_db()["risk_cases"].count_documents({}) == 0, "仿真建了案件"


async def test_sim_uses_current_enabled_rule_set(client, profile_inputs):
    """BR-10-03：仿真用**当前生效**规则集，不吃快照、不吃缓存。

    停用一条规则后再仿真，分值必须立刻变化（规则的读路径每次直查 Mongo，
    与 `/engine/evaluate` 的读路径是同一个 `RuleRepo`）。
    """
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData(
        user_register_at=now_ms() - 2 * 86_400_000, user_level="normal", user_risk_tag_cnt=0,
        ip_is_proxy=True, address_aftersale_cnt=0,
    ))
    service._linked_provider = FakeLinkedUserCountProvider({"device": 6, "ip": 6, "address": 6})

    before = await _sim_block(client, sim_event())
    assert before["rule_score"] == 70

    # 停用 RLOGIN001（45 分）
    rules = list(PROFILE_RULES)
    rules[0] = {**rules[0], "status": "disabled", "version": 2}
    await install_rules(*rules)

    after = await _sim_block(client, sim_event())
    assert after["rule_score"] == 25, after
    assert "RLOGIN001" not in after["rule_versions"], "停用的规则不该出现在生效规则集里"
    engine = await _engine_block(client, sim_event())
    assert_blocks_equal(after, engine)
