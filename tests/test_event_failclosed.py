# -*- coding: utf-8 -*-
"""模块 03 降级（fail-closed）与事件流模拟器验收测试。

覆盖 V-03-06 / V-03-07 / V-03-08 / V-03-09 / V-03-11 / V-03-12 / V-03-13，
对应 BR-03-21 ~ 32。

## 本文件的中心命题

**"不确定就不放行"**。模块 04（特征计算）与 05（规则决策）**都已落地**，
因此这条链路的真实行为现在是"能算就真算、算不出来才降级"；降级的环节与原因
分成三条，必须分别钉住，不能再用一句话概括：

1. **默认装配下**，若关键特征不可用，事件全部是 `review`，绝不 `pass`。此时 04
   是真实的 `FeatureService`：它算得出 18 项里的行为频次，但关键特征
   `ip_is_proxy` 依赖 E12 IP 画像；查不到时它**如实建议降级**
   （`degrade_suggested=True`，BR-04-09：不得用 0 冒充缺失），03 据此在调 05
   **之前**短路在 `stage=feature`。这条是**数据缺口**，不是"04 不在链路上"——
   判据就是降级原因文案："快照不完整" vs "服务不可用"。
2. **04 真的故障**（抛异常 / 返回空结构 / 被摘除成占位实现）时同样降级到
   `stage=feature`，且**下游没有被假装成功**。这条路现在必须由用例显式安装故障
   实现来覆盖：默认装配已经不是"不可用的 04"了。
3. 04 给出**完整快照**时，链路会真正走到 05 并拿到**真实决策**——05 落地前
   这里停在 `stage=rule`（`EVT-5003`），现在那不再是事实，相关断言已改为
   断言真实决策的判别式证据（`engine_version` / `model_score` / `rule_score`）。
   "05 被摘除 → `stage=rule`"这条契约仍然被覆盖，但必须由用例**显式装回
   `UnavailableDecisionProvider`**（见 `test_unavailable_decision_provider_degrades_at_rule_stage`）。
4. 超时必须 `stage=timeout`、耗时≈200ms，且**未完成的下游协程被取消**
   （否则接口返回了 review、下游还在跑并可能写库，形成分裂状态）。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import db
from app.constants import COLL_IP_POOL, COLL_RISK_EVENTS
from app.core.event_simulator import (
    MODE_BRUSH,
    MODE_NORMAL,
    MODE_REFUND,
    MODE_WOOL,
    MODES,
    build_event,
    build_profiles,
    get_simulator,
)
from app.core.event_simulator import validate_start_params
from app.core import edge_writer
from app.errors import AppError
from app.protocols import (
    UnavailableDecisionProvider,
    UnavailableFeatureProvider,
    configure_components,
)
from app.schemas.event_schema import DECISION_FIELDS
from app.services import event_service
from app.services.event_service import TIMEOUT_SEC, validate_event
from app.utils.timeutil import now_ms
from tests.conftest import WRITER
from tests.event_testlib import (
    PAYLOAD_FACTORIES,
    ExplodingDecisionProvider,
    ExplodingFeatureProvider,
    FakeFeatureProvider,
    FIXED_DECISION,
    SleepingDecisionProvider,
    component_reset,
    coupon_payload,
    install_fakes,
    login_payload,
    order_create_payload,
    order_pay_payload,
    reset_runtime_state,
)

pytestmark = pytest.mark.anyio

EVENTS_URL = "/api/v1/events"
MOCK_START = "/api/v1/mock/start"
MOCK_STOP = "/api/v1/mock/stop"
MOCK_STATUS = "/api/v1/mock/status"


@pytest.fixture(autouse=True)
async def event_state():
    """逐用例复位进程内状态（实现见 `tests/event_testlib.py`）。"""
    component_reset()
    reset_runtime_state()
    await db.get_db()[COLL_RISK_EVENTS].delete_many({})
    yield
    sim = get_simulator()
    if sim.running:
        await sim.stop()
    await event_service.flush()
    reset_runtime_state()


async def post_event(client, payload: dict, headers=None):
    return await client.post(EVENTS_URL, json=payload, headers=headers or WRITER)


# ============================================================ 默认组件必须"已落地什么就装什么"
async def test_default_providers_reflect_landed_modules():
    """默认装配必须如实反映"哪些模块已经落地"：04 / 05 / 09 **都是真的**。

    为什么现在三格都是真实实现：

    - 模块 04（特征计算）**已落地**。`app/protocols.py` 的 `Components.feature_provider`
      默认指向它的真实实现 `FeatureService`（见 `default_feature_provider`）。
      继续断言"默认 04 不可用"就等于把"04 还没做"写进验收，而且会让默认装配
      这条路径永远不被测试覆盖。
    - 模块 05（规则决策）**已落地**，`decision_provider` 默认指向它的真实实现
      `RuleDecisionProvider`（决策 D45：默认装配必须体现真实存在的组件）。
      **旧的断言"默认 05 不可用"必须改**——继续断言占位就等于把"05 还没做"
      写进验收，而这条链路的真实行为（名单 → 条件树 → 累加 → 仲裁）将永远
      不被默认装配覆盖。
    - 模块 09（画像与关联图谱）**已落地**，`linked_user_count_provider` 默认指向
      它的真实实现 `MongoLinkedUserCountProvider`。04 的三项聚集度特征因此
      不再进 `missing_features`。

    ## 判别式证据（不是裸断言）

    光断言"类型名是 RuleDecisionProvider"是无法证伪的（改个类名就能骗过）。
    这里同时断言三条**只有真实实现才可能满足**的性质：

    1. `available is True`（占位实现是 `False`）；
    2. 它真的能产出一个决策块，且 `model_score is None` / `engine_version`
       是 `rule-engine-v1`（占位实现只会抛异常）；
    3. **占位实现仍然存在且仍然抛异常**——"05 被摘除"这条契约没有被删掉，
       只是不再是默认值（本文件末尾的用例会显式装回它）。

    ## 为什么用 `Components()`（新实例）而不是只看进程内单例

    单例可能被别的用例的夹具改成"不可用"（那是 03 的降级用例需要的装配），
    用它断言默认装配会把"用例执行顺序"引入结论。
    """
    from app.protocols import (
        Components,
        DecisionProvider,
        UnavailableDecisionProvider,
        default_decision_provider,
    )
    from app.services import feature_service
    from app.services.feature_service import FeatureService
    from app.services.profile_service import MongoLinkedUserCountProvider

    fresh = Components()
    # 04：真实实现（不是占位），且就是模块 04 的进程内单例
    assert isinstance(fresh.feature_provider, FeatureService)
    assert not isinstance(fresh.feature_provider, UnavailableFeatureProvider)
    assert feature_service.get_feature_service() is not None
    # 05：真实实现 + `available=True`（占位实现是 False）
    assert type(fresh.decision_provider).__name__ == "RuleDecisionProvider"
    assert isinstance(fresh.decision_provider, DecisionProvider)
    assert not isinstance(fresh.decision_provider, UnavailableDecisionProvider)
    assert fresh.decision_provider.available is True
    assert type(default_decision_provider()).__name__ == "RuleDecisionProvider"
    # 09：真实实现，且 `available=True`（占位实现是 False）
    assert isinstance(fresh.linked_user_count_provider, MongoLinkedUserCountProvider)
    assert fresh.linked_user_count_provider.available is True

    # `/health` 的组件说明里必须能看出装的是哪套实现（便于一眼识破占位）
    described = fresh.describe()
    assert described["feature_provider"] == "FeatureService"
    assert described["decision_provider"] == "RuleDecisionProvider"
    assert described["linked_user_count_provider"] == "MongoLinkedUserCountProvider"

    # 真实 05 能产出决策块（VB-05：占位实现只会抛异常）
    block = await fresh.decision_provider.evaluate(
        {"_id": "EVT20240101000000000001", "event_type": "login", "user_id": "U000001",
         "ts": now_ms(), "scene_extra": {}},
        {"login_cnt_1h": 1},
    )
    assert isinstance(block, dict)
    assert block["decision"] in {"pass", "review", "reject"}
    assert block["model_score"] is None, "AD-09：模型引擎是空实现，模型分恒为 null"
    assert block["engine_version"] == "rule-engine-v1"

    # 占位实现**仍然在**，且"不可用必须表现为抛异常"这条契约没有被删掉
    placeholder = UnavailableDecisionProvider()
    assert placeholder.available is False
    with pytest.raises(RuntimeError):
        await placeholder.evaluate({"event_type": "login"}, {})


@pytest.mark.parametrize("event_type,factory", PAYLOAD_FACTORIES)
async def test_default_components_never_return_pass(client, event_type, factory):
    """默认装配下**每一类**事件都必须是 `review`，绝不出现 `pass`。

    这是 fail-closed 的最直接证据（也是当前阶段链路的真实行为）。

    `stage` 仍是 `feature`，但原因已经变了——默认 04 是真实的 `FeatureService`，
    它没有故障，只是**算不出关键特征** `ip_is_proxy`：该特征来自 E12 IP 画像
    （画像库归模块 09，尚未落地），04 因此如实给出 `degrade_suggested=True`，
    03 在调 05 之前短路（见 `event_service._compute_and_decide`）。所以这里断言
    的是"快照不完整"这条**数据缺口**文案，而**不是**"特征服务不可用"那条故障文案。
    """
    r = await post_event(client, factory())
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["decision"] == "review", f"{event_type} 在默认组件下返回了 {data['decision']}"
    assert data["decision"] != "pass"
    assert data["degrade"] is not None
    assert data["degrade"]["degraded"] is True
    assert data["degrade"]["stage"] == "feature"
    assert data["degrade"]["reason"], "降级必须给出原因（BR-03-24：供人工复核定位）"
    assert "特征快照不完整" in data["degrade"]["reason"], data["degrade"]["reason"]
    # `degrade_suggested` 是 04 快照契约里的字段名：03 只有**拿到 04 返回的快照字典**
    # 才可能把它写进原因文案。因此这条字符串本身就是"04 在这条链路上真的跑了"的证据
    # ——旧的 `UnavailableFeatureProvider` 只会抛异常，原因文案会是"特征服务不可用"。
    assert "degrade_suggested" in data["degrade"]["reason"], data["degrade"]["reason"]
    assert "特征服务不可用" not in data["degrade"]["reason"], (
        "默认装配装的是真实 04（FeatureService），不该出现'特征服务不可用'这条故障文案；"
        "出现它就说明默认装配退回了占位实现"
    )
    assert data["rule_score"] == 0
    assert data["hits"] == []
    assert data["engine_version"] == "degraded", "降级块的来源必须可辨识"


async def test_unavailable_feature_provider_degrades_at_feature_stage(client):
    """BR-03-21 的**04 故障路径**：`UnavailableFeatureProvider` 被装回链路 → `stage=feature`。

    这条以前由"默认装配就是不可用"隐式覆盖；04 落地后默认装配换成了真实的
    `FeatureService`，因此必须**在这个用例内部显式**把"04 被摘除/未接入"的占位实现
    装回去，才能继续钉住"04 不可用 → 200 + review + `stage=feature`"这条契约。
    它同时划清了两种降级的边界：这里的原因文案是"**服务不可用**"（04 故障），
    而上一条默认链路的文案是"**快照不完整**"（04 正常、数据缺口）。

    复原方式与其它用例一致：本文件 autouse 夹具每个用例开头都会调用
    `component_reset()`（真实默认装配），因此不需要在本用例里手写回滚。
    """
    configure_components(feature_provider=UnavailableFeatureProvider())
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200, "降级不是错误响应，HTTP 必须仍是 200"
    data = r.json()["data"]
    assert data["decision"] == "review"
    assert data["decision"] != "pass"
    assert data["degrade"]["stage"] == "feature"
    assert "特征服务不可用" in data["degrade"]["reason"]
    assert "特征快照不完整" not in data["degrade"]["reason"], (
        "占位实现是抛异常，走的是'服务不可用'分支，不是'快照不完整'分支"
    )
    assert data["rule_score"] == 0 and data["final_score"] == 0


async def test_default_assembly_reaches_rule_engine_once_snapshot_is_complete(client):
    """默认装配（真实 04 + 真实 05）下，04 给出完整快照后**链路真的走到 05**。

    这条以前断言的是"05 尚未落地 → `stage=rule`"。05 落地后那句不再是事实，
    因此改为断言**真实决策的判别式证据**（而不是放宽成"有 decision 就算过"）：

    - `degrade is None`：链路没有在任何环节短路；
    - `engine_version == "rule-engine-v1"`：块是 05 的真实产物（03 自己造的
      降级块写的是 `degraded`，占位实现只会抛异常）；
    - `model_score is None`：AD-09 的模型引擎仍是空实现，这一点没被顺带改掉；
    - `snapshot_id` 以 `SNP` 开头：03 把 04 的真实快照编号补进了决策块
      （`normalize_decision` 的 `setdefault`），证明是三段贯通而不是某一段伪造。

    本次库里**没有配任何规则**（夹具逐用例清空 E05），因此 0 分 → `pass` 是
    正确的结论：没有规则可命中时，`rule_score` 就是 0（BR-05-15）。
    与上一条默认链路用例互为对照：不给 E12 画像时短路在 `stage=feature`；
    补上 E12 后 04 不再建议降级，决策由 05 给出。
    """
    ip = coupon_payload()["ip"]
    # 只补 E12 一行真实画像（`is_proxy=False` = 已知不是代理出口），不伪造任何特征值：
    # 04、画像读取器、Mongo 全是真实路径
    await db.get_db()[COLL_IP_POOL].insert_one(
        {"_id": ip, "is_proxy": False, "is_idc": False}
    )
    try:
        r = await post_event(client, coupon_payload())
    finally:
        # 画像库不归模块 03 维护，用完即删，不给后续用例留状态
        await db.get_db()[COLL_IP_POOL].delete_many({"_id": ip})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["degrade"] is None, (
        "04 给了完整快照、05 已落地，链路上不该再有任何降级环节"
    )
    assert data["decision"] in {"pass", "review", "reject"}
    assert data["engine_version"] == "rule-engine-v1", (
        "决策块必须来自真实的规则引擎；03 自造的降级块写的是 `degraded`，"
        f"实际 {data['engine_version']!r}"
    )
    assert data["model_score"] is None, "AD-09：模型引擎是空实现，模型分恒为 null"
    assert isinstance(data["rule_score"], int) and data["final_score"] == data["rule_score"]
    assert str(data["snapshot_id"] or "").startswith("SNP"), (
        "03 必须把 04 的真实快照编号补进决策块（三段贯通的证据）"
    )
    assert data["list_hit"] == {"hit": False, "list_type": None,
                                "entity_type": None, "entity_value": None}, (
        "`list_hit` 是对象（契约裁定），未命中时 hit=false 且三个描述字段为 null"
    )


async def test_degraded_events_are_still_persisted(client):
    """BR-03-24：降级时**必须仍写入** `risk_events`（保住不可篡改的原始证据）。

    落库的降级原因同样来自默认链路的真实环节（04 报告快照不完整），因此这里
    连原因文案一起断言：只写 `stage` 的话，"04 故障"与"04 报告数据缺口"两种
    完全不同的处置会共用同一个断言。
    """
    r = await post_event(client, coupon_payload())
    event_id = r.json()["data"]["event_id"]
    await event_service.flush()
    doc = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id})
    assert doc is not None, "降级事件也必须落库"
    assert doc["degrade"]["stage"] == "feature"
    assert "特征快照不完整" in doc["degrade"]["reason"], doc["degrade"]["reason"]
    assert doc["decision"]["decision"] == "review"


# ============================================================ V-03-07 04 不可用（显式故障路径）
async def test_feature_service_failure_degrades_to_review(client):
    """V-03-07 / BR-03-21：04 抛异常 → 200 + `review` + `stage=feature`。

    这是这条契约的**显式**覆盖：默认装配里的 04 是真实的 `FeatureService`，
    它对载荷不会抛异常（只会如实报告快照不完整），所以"04 挂了也绝不放过"这件事
    必须由本用例自己装上抛异常的假 04 来守——不能再指望默认装配顺带覆盖它。
    """
    provider = ExplodingFeatureProvider()
    configure_components(feature_provider=provider)
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200, "降级不是错误响应，HTTP 必须仍是 200"
    data = r.json()["data"]
    assert data["decision"] == "review"
    assert data["decision"] != "pass"
    assert data["degrade"]["stage"] == "feature"
    assert "特征服务不可用" in data["degrade"]["reason"]
    assert data["rule_score"] == 0 and data["final_score"] == 0
    assert provider.calls == 1, "04 应当被调用过一次（不能跳过它直接判 review）"


async def test_rule_engine_is_not_called_when_feature_fails(client):
    """04 挂了就不该再调 05：05 拿不到特征快照时给出的结论没有依据。

    这条挡住一类很隐蔽的错误实现——"04 失败后拿空特征继续问 05"，
    那会让 05 基于"没有特征"给出结论，等价于用假数据决策。
    """
    decision = ExplodingDecisionProvider()
    configure_components(feature_provider=ExplodingFeatureProvider(),
                         decision_provider=decision)
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200
    assert r.json()["data"]["degrade"]["stage"] == "feature"
    assert decision.calls == 0, "04 不可用时不得继续调用 05"


async def test_feature_provider_returning_empty_snapshot_is_degraded(client):
    """04 返回空结构（不是异常）同样必须 fail-closed：不能拿空特征去问 05。"""

    class EmptyFeatureProvider:
        available = True

        async def compute(self, event: dict) -> dict:
            return {"snapshot_id": None, "features": {}, "degrade_suggested": True}

    configure_components(feature_provider=EmptyFeatureProvider())
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["decision"] == "review"
    assert data["degrade"]["stage"] == "feature"
    assert "不完整" in data["degrade"]["reason"]


# ============================================================ V-03-08 05 不可用
async def test_decision_service_failure_degrades_to_review(client):
    """V-03-08 / BR-03-22：05 抛异常 → 200 + `review` + `stage=rule`。"""
    decision = ExplodingDecisionProvider()
    configure_components(feature_provider=FakeFeatureProvider(delay=0),
                         decision_provider=decision)
    r = await post_event(client, order_pay_payload())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["decision"] == "review"
    assert data["degrade"]["stage"] == "rule"
    assert "决策服务不可用" in data["degrade"]["reason"]
    assert decision.calls == 1


async def test_unavailable_decision_provider_degrades_at_rule_stage(client):
    """"05 被摘除"这条契约：装回 `UnavailableDecisionProvider` → `stage=rule`。

    05 落地后它**不再是默认装配**（决策 D45），因此这条以前被默认链路顺带覆盖
    的契约必须由用例**显式**装回占位实现来守——否则"引擎没跑"与"引擎判了
    review"这两件事会重新变得无法区分，而这正是 `UnavailableDecisionProvider`
    存在的全部理由（伪造一个决策块比故障更危险）。

    与上一条的分工：上一条模拟"05 抛异常"，本条模拟"05 **根本没有实现**/被摘除"。
    两条合起来覆盖 BR-03-22 的两条入口。
    """
    configure_components(
        feature_provider=FakeFeatureProvider(delay=0),
        decision_provider=UnavailableDecisionProvider(),
    )
    r = await post_event(client, order_pay_payload())
    assert r.status_code == 200, "降级不是错误响应，HTTP 必须仍是 200"
    data = r.json()["data"]
    assert data["decision"] == "review", "占位 05 必须由 03 fail-closed 转人工"
    assert data["decision"] != "pass"
    assert data["degrade"]["stage"] == "rule"
    assert "决策服务不可用" in data["degrade"]["reason"], data["degrade"]["reason"]
    # 降级块由 03 自造，`engine_version` 必须是 `degraded`（与 05 的真实产物区分）
    assert data["engine_version"] == "degraded"
    assert data["list_hit"] == {"hit": False, "list_type": None,
                                "entity_type": None, "entity_value": None}


async def test_illegal_decision_value_is_clamped_to_review(client):
    """05 返回了不在三档里的取值 → 必须按最保守的 `review` 处理。

    绝不能写成"非 reject 即放行"——那会把一次契约违例变成一次放行。
    """

    class WeirdDecisionProvider:
        available = True

        async def evaluate(self, event: dict, features: dict) -> dict:
            return {**FIXED_DECISION, "decision": "maybe"}

    configure_components(feature_provider=FakeFeatureProvider(delay=0),
                         decision_provider=WeirdDecisionProvider())
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200
    assert r.json()["data"]["decision"] == "review"


# ============================================================ V-03-06 决策块由 05 产出
async def test_decision_block_is_passed_through_verbatim(client):
    """V-03-06：mock 05 返回固定分值 → 响应决策块与之**逐字段一致**。

    这是"03 没自造决策块"的直接证据：只要 03 动了任何一个字段（例如自己算
    一个 `final_score`、把 `risk_level` 归一成 low），这条就会红。
    """
    fixed = {
        "list_hit": {"hit": True, "list_type": "black", "entity_type": "device",
                     "entity_value": "DBRUSH0001"},
        "rule_score": 72,
        "model_score": None,
        "final_score": 72,
        "risk_level": "high",
        "decision": "reject",
        "hit_rule_count": 2,
        "hits": [{"rule_code": "R_A", "score": 40}, {"rule_code": "R_B", "score": 32}],
        "rule_versions": {"R_A": "v3", "R_B": "v1"},
        "engine_version": "rule-engine-2.1",
        "elapsed_ms": 7,
    }

    class FixedProvider:
        available = True

        async def evaluate(self, event: dict, features: dict) -> dict:
            return dict(fixed)

    configure_components(feature_provider=FakeFeatureProvider(delay=0),
                         decision_provider=FixedProvider())
    r = await post_event(client, order_create_payload())
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    for field in DECISION_FIELDS:
        if field == "snapshot_id":
            # 快照编号由 04 给出，03 只负责把它透传（05 可能不回传它）
            assert data[field] == "SNP000000000001"
            continue
        if field == "elapsed_ms":
            continue        # 该字段是"本次 05 求值耗时"，由 05 给、03 不覆盖
        assert data[field] == fixed[field], f"字段 {field} 被 03 改写了"
    assert data["decision"] == "reject"
    assert data["degrade"] is None, "05 正常产出时不得出现降级信封"


async def test_normal_path_reports_no_degrade(client):
    """04/05 都正常时 `degrade` 必须是 `null`（§3.1 响应表）。"""
    install_fakes()
    r = await post_event(client, login_payload())
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["degrade"] is None
    assert data["decision"] == "review"      # 05 自己判的 review，不是降级
    assert data["engine_version"] == "rule-1.0"
    assert data["rule_score"] == 55
    assert data["snapshot_id"] == "SNP000000000001"


# ============================================================ V-03-09 超时降级
async def test_timeout_degrades_and_cancels_the_downstream(client):
    """V-03-09 / BR-03-23：05 慢于 200ms → 200 + `review` + `stage=timeout`。

    同时断言两件事：
    ① 耗时 ≈ 200ms（没有被下游拖到 1s）；
    ② **未完成的下游协程被取消**（`cancelled=True` 且 `finished=False`）。
       不取消的话，接口已经返回 review，下游仍在跑并可能写库，
       库里会出现"接口说 review、决策表说 pass"的分裂状态。
    """
    sleeper = SleepingDecisionProvider(seconds=1.0)
    configure_components(feature_provider=FakeFeatureProvider(delay=0),
                         decision_provider=sleeper)

    started = time.perf_counter()
    r = await post_event(client, coupon_payload())
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["decision"] == "review"
    assert data["degrade"]["stage"] == "timeout"
    assert "超时" in data["degrade"]["reason"]
    assert elapsed_ms < 700, f"超时预算未生效：耗时 {elapsed_ms:.0f}ms"
    assert elapsed_ms >= TIMEOUT_SEC * 1000 * 0.8, (
        f"耗时 {elapsed_ms:.0f}ms 明显小于预算，说明不是超时触发而是别的路径"
    )
    # 让取消真正传播到下游协程（wait_for 已在超时时发出取消）
    await asyncio.sleep(0.05)
    assert sleeper.cancelled is True, "超时必须取消未完成的下游协程"
    assert sleeper.finished is False, "下游不该在超时后继续跑完"


async def test_feature_timeout_also_degrades(client):
    """同样的预算也覆盖 04：慢的是 04 时 `stage` 同样是 `timeout`。

    Spec §5 只给了 `EVT-5001` 一个"决策链路超时"码，没有区分是 04 还是 05
    慢——`stage` 的语义是"链路在哪一步被超时打断"，这里如实反映为 timeout。
    """

    class SlowFeatureProvider:
        available = True

        async def compute(self, event: dict) -> dict:
            await asyncio.sleep(1.0)
            return {"snapshot_id": "SNP1", "features": {}}

    configure_components(feature_provider=SlowFeatureProvider(),
                         decision_provider=SleepingDecisionProvider(seconds=0.01))
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200
    assert r.json()["data"]["degrade"]["stage"] == "timeout"


async def test_batch_degrades_each_item_independently(client):
    """批内每条都独立降级：一条的 fail-closed 不影响其它条的对错计数。"""
    configure_components(feature_provider=ExplodingFeatureProvider())
    r = await client.post("/api/v1/events/batch",
                          json={"events": [coupon_payload(), coupon_payload()],
                                "verbose": True},
                          headers=WRITER)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["review_cnt"] == 2
    assert data["failed_cnt"] == 0
    assert all(item["decision"] == "review" for item in data["results"])


# ============================================================
# 事件流模拟器（BR-03-25 ~ 32 / §3.4）
# ============================================================
async def wait_until(predicate, timeout: float = 6.0, interval: float = 0.05) -> None:
    """轮询等待条件成立（模拟器是后台任务，产出时刻由虚拟时钟决定）。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("等待模拟器产出超时")


async def start_sim(client, **over):
    body = {"mode": MODE_NORMAL, "rate": 50, "seed": 42, "max_events": 5,
            "duration_sec": 60}
    body.update(over)
    return await client.post(MOCK_START, json=body, headers=WRITER)


# ------------------------------------------------------------ 四模式
@pytest.mark.parametrize("mode", MODES)
async def test_each_mode_starts_stops_and_reports_status(client, mode):
    """V-03-11：四模式均可启动 → 产出事件 → 停止并给出汇总。

    断言 `decision_counts` 之和 == `emitted`（§7 V-03-11 的字面要求）。

    ## 为什么不再断言"全部落到 review"

    05 落地前这条写的是 `review == 5 / pass == 0`——那是真实的（无论 04 是否
    在链路上，事件都必然降级）。现在这句话不再是事实：同一个 IP 的第 2 条起
    `ip_is_proxy` 可解析、快照完整，链路会走到 05 并给出**真实决策**；而本用例
    的库里**没有任何规则**（夹具逐用例清空 E05），0 分 → `pass` 是正确结论。

    保留的判别力（没有放宽成"有 decision 就算过"）：

    - `sum(decision_counts) == emitted`：每条事件都必须**恰好**落进一档；
    - `reject == 0`：库里既无规则也无名单条目，没有任何东西该被判拦截——
      出现 `reject` 就说明名单或规则读到了不该读的数据；
    - `review >= 1`：每个模式的首条事件都对着一个**全新的 IP**（夹具清空了
      E12），`ip_is_proxy` 缺失 → 04 如实建议降级 → 这条必须是 `review`
      而不是 `pass`（fail-closed 仍在）。
    """
    r = await start_sim(client, mode=mode, max_events=5, rate=50)
    assert r.status_code == 200, r.text
    started = r.json()["data"]
    assert started["running"] is True
    assert started["mode"] == mode
    assert started["seed"] == 42

    sim = get_simulator()
    await wait_until(lambda: sim.emitted >= 5)

    st = await client.get(MOCK_STATUS, headers=WRITER)
    assert st.status_code == 200
    status = st.json()["data"]
    assert status["running"] is True or status["emitted"] == 5
    assert status["emitted"] == 5
    assert sum(status["decision_counts"].values()) == status["emitted"]

    stop = await client.post(MOCK_STOP, headers=WRITER)
    assert stop.status_code == 200, stop.text
    summary = stop.json()["data"]
    assert summary["running"] is False
    assert summary["emitted"] == 5
    assert sum(summary["decision_counts"].values()) == 5
    assert summary["decision_counts"]["reject"] == 0, (
        "无规则、无名单：没有任何依据能判拦截（出现 reject 说明读到了脏数据）"
    )
    assert summary["decision_counts"]["review"] >= 1, (
        "首条事件对着全新 IP，`ip_is_proxy` 缺失必须 fail-closed 转人工"
    )


async def test_simulator_events_are_really_persisted_and_validated(client):
    """BR-03-29：模拟器事件走**同一条接入路径**（真实校验 + 真实落库）。

    ## 这条用例现在同时是**决策 D46 落地的证据**

    `wool` 模式的 4 条事件共用同一个 IP（`203.0.113.10`），因此：

    - **第 1 条**：E12 还没有这个 IP 的行 → `ip_is_proxy` 进 `missing_features`
      → 04 如实给出 `degrade_suggested=True` → 03 在调 05 之前短路，
      `stage=feature`（数据缺口，不是 04 故障）；
    - **第 2~4 条**：第 1 条的决策返回**之后**，模块 09 已经异步把 E12 行落好
      （显式 `is_proxy=False`/`is_idc=False`，见 `repos/profile_repo.ensure_ip`），
      于是 `ip_is_proxy` 可解析、快照完整，链路真正走到 05 并给出**真实决策**。

    这正是 D46 要解决的问题："若 09 只给出现过的 IP 建画像，则每个新 IP 事件都会
    永久降级、05 永远调不到"。**第一个事件仍降级是正确行为**（当时确实不知道），
    关键是它不会**永远**降级。

    ⚠️ 两处旧断言已被 05 的落地推翻（"4 条全部 `stage=feature"` → "后三条
    `stage=rule`" → 现在是"后三条 `degrade` 不存在、拿到真实决策"）。这条用例
    被判别的依据是**决策块的来源字段**（`engine_version`/`model_score`），
    而不是"有没有 decision 字段"。
    """
    await start_sim(client, mode=MODE_WOOL, max_events=4, rate=50)
    sim = get_simulator()
    await wait_until(lambda: sim.emitted >= 4)
    await client.post(MOCK_STOP, headers=WRITER)
    await event_service.flush()
    # 09 的建边是异步旁路：等它收尾，否则最后一条事件可能还没补上 E12 行
    await edge_writer.flush()

    rows = await db.get_db()[COLL_RISK_EVENTS].find({}).to_list(length=10)
    assert len(rows) == 4
    assert all(row["source"] == "mock_biz" for row in rows), "BR-03-30：source 固定 mock_biz"
    assert all(row["event_type"] == "coupon_receive" for row in rows)
    rows.sort(key=lambda row: row["ts"])          # 按事件时间排出先后
    assert all(row["decision"]["decision"] in {"pass", "review", "reject"} for row in rows)
    assert rows[0]["degrade"]["stage"] == "feature"
    assert "特征快照不完整" in rows[0]["degrade"]["reason"], rows[0]["degrade"]["reason"]
    assert "degrade" not in rows[1:][0], (
        "首个事件之后 E12 已有该 IP 的画像行，快照应当完整 —— 若这里仍带 degrade，"
        "说明 09 的 IP 自动落画像没有生效（D46 的跨模块义务被破坏）"
    )
    for row in rows[1:]:
        assert "degrade" not in row, row.get("degrade")
        assert row["decision"]["engine_version"] == "rule-engine-v1", (
            "后三条必须由真实的规则引擎给出决策（03 自造的降级块写的是 degraded）"
        )
        assert row["decision"]["model_score"] is None
    # IP 画像确实被 09 落下来了，且**布尔字段显式在场**（04 的 `_pick_proxy` 只有
    # 看到 bool 才会给出 False，字段缺失会返回 None 并继续判"缺失"）
    ip_doc = await db.get_db()[COLL_IP_POOL].find_one({"_id": "203.0.113.10"})
    assert ip_doc is not None, "D46：事件带 IP 时必须自动落 E12 画像行"
    assert ip_doc["is_proxy"] is False and ip_doc["is_idc"] is False
    assert ip_doc["first_seen_at"] and ip_doc["linked_user_cnt"] >= 1


# ------------------------------------------------------------ 重复启动 / 启动失败
async def test_duplicate_start_returns_409_evt_4008(client):
    """BR-03-31：重复 `start` → `409 EVT-4008`（Spec 原写 `EVT-5006`，见交付报告）。"""
    first = await start_sim(client)
    assert first.status_code == 200, first.text
    second = await start_sim(client)
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["code"] == "EVT-4008"
    assert "已在运行" in body["message"]
    assert body["data"]["mode"] == MODE_NORMAL
    await client.post(MOCK_STOP, headers=WRITER)


async def test_invalid_mode_returns_500_evt_5005(client):
    """§5 / EVT-5005：非法 `mode` → 500（并释放资源，不留"运行中"的假状态）。"""
    r = await start_sim(client, mode="nonexistent")
    assert r.status_code == 500, r.text
    assert r.json()["code"] == "EVT-5005"
    assert get_simulator().running is False, "启动失败后不得停留在运行态"


async def test_invalid_rate_returns_com_4001(client):
    """越界参数归通用参数校验（`COM-4001`/422），与 `mode` 非法（500）分开。"""
    for bad in ({"rate": 0}, {"rate": 500}, {"max_events": 0},
                {"max_events": 99_999}, {"duration_sec": 0}, {"duration_sec": 9999}):
        r = await start_sim(client, **bad)
        assert r.status_code == 422, f"{bad} -> {r.status_code}"
        assert r.json()["code"] == "COM-4001"


async def test_validate_start_params_rejects_bad_mode_with_evt_5005():
    """参数校验的纯逻辑面：`mode` 归 EVT-5005、其余归 COM-4001。"""
    with pytest.raises(AppError) as e:
        validate_start_params({"mode": "x"})
    assert e.value.code == "EVT-5005"
    with pytest.raises(AppError) as e2:
        validate_start_params({"mode": MODE_WOOL, "rate": 1000})
    assert e2.value.code == "COM-4001"
    cfg = validate_start_params({"mode": MODE_BRUSH, "rate": 10, "seed": 7})
    assert cfg["mode"] == MODE_BRUSH and cfg["seed"] == 7 and cfg["rate"] == 10.0


async def test_stop_when_not_running_is_ok(client):
    """未运行时 `stop` 仍返回 200（`stop` 的语义是"确保它停下来"）。"""
    r = await client.post(MOCK_STOP, headers=WRITER)
    assert r.status_code == 200
    assert r.json()["data"]["running"] is False


async def test_simulator_requires_sim_run_permission(client, bearer):
    """模拟器控制用 `sim:run`（本模块的权限假设，见 `event_api.py`）。"""
    r = await client.post(MOCK_START, json={"mode": MODE_NORMAL},
                          headers=await bearer("admin"))
    assert r.status_code == 403
    assert r.json()["code"] == "AUTH-4020"
    assert get_simulator().running is False


# ------------------------------------------------------------ V-03-12 固定种子可复现
@pytest.mark.parametrize("mode", MODES)
async def test_same_seed_produces_identical_events(mode):
    """V-03-12 / BR-03-32：同 `(mode, seed, rate, max_events)` 两次运行逐字段一致。"""
    anchor = 1_800_000_000_000        # 显式锚点：使 ts 也完全一致（更强的断言）
    p1 = build_profiles(mode, 42)
    p2 = build_profiles(mode, 42)
    assert p1 == p2

    first = [build_event(mode, 42, i, anchor + i * 100, p1) for i in range(20)]
    second = [build_event(mode, 42, i, anchor + i * 100, p2) for i in range(20)]
    assert first == second, f"{mode} 模式在同种子下产出不一致"


@pytest.mark.parametrize("mode", MODES)
async def test_different_seed_produces_different_events(mode):
    """可复现不等于"永远一样"：换种子必须换序列，否则种子是摆设。"""
    anchor = 1_800_000_000_000
    a = build_profiles(mode, 42)
    b = build_profiles(mode, 43)
    assert a != b, f"{mode} 模式换种子后池未变化"
    first = [build_event(mode, 42, i, anchor, a) for i in range(10)]
    second = [build_event(mode, 43, i, anchor, b) for i in range(10)]
    assert first != second


async def test_virtual_clock_follows_the_rate():
    """BR-03-25：`ts = anchor_ts + round(i*1000/rate)`（序列可复现的时间分布）。"""
    anchor = 1_800_000_000_000
    profiles = build_profiles(MODE_NORMAL, 42)
    rate = 5.0
    events = [build_event(MODE_NORMAL, 42, i, anchor + int(round(i * 1000 / rate)),
                          profiles)
              for i in range(5)]
    assert [e["ts"] for e in events] == [anchor + 200 * i for i in range(5)]


# ------------------------------------------------------------ BR-03-28 四模式特征
async def test_normal_mode_produces_all_four_event_kinds():
    """`normal`：常态流量，四类事件轮转（登录/领券/下单/支付）。"""
    anchor = 1_800_000_000_000
    profiles = build_profiles(MODE_NORMAL, 42)
    kinds = {build_event(MODE_NORMAL, 42, i, anchor, profiles)["event_type"]
             for i in range(8)}
    assert kinds == {"login", "coupon_receive", "order_create", "order_pay"}


async def test_wool_mode_uses_new_accounts_with_shared_device_and_ip():
    """`wool`：10 个新账号 + **同** `device_id` + **同** `ip`，密集领券。"""
    anchor = 1_800_000_000_000
    profiles = build_profiles(MODE_WOOL, 42)
    events = [build_event(MODE_WOOL, 42, i, anchor + i * 100, profiles)
              for i in range(30)]
    assert {e["event_type"] for e in events} == {"coupon_receive"}
    assert all(e["user_id"].startswith("w") for e in events), "应为新号段账号"
    assert len({e["user_id"] for e in events}) == 10, "BR-03-28：10 个新账号"
    assert len({e["device_id"] for e in events}) <= 2, "羊毛党共用极少数设备"
    assert len({e["ip"] for e in events}) == 1, "BR-03-28：同 IP"


async def test_brush_mode_is_high_frequency_order_create_and_pay():
    """`brush`：同 sku 高频 `order_create` + `order_pay`，收货地址聚集。"""
    anchor = 1_800_000_000_000
    profiles = build_profiles(MODE_BRUSH, 42)
    events = [build_event(MODE_BRUSH, 42, i, anchor + i * 50, profiles)
              for i in range(20)]
    kinds = {e["event_type"] for e in events}
    assert kinds == {"order_create", "order_pay"}
    addresses = {e.get("address_id") for e in events if e.get("address_id")}
    # "聚集"必须是**事件的属性**：刷单的收货地址只落在极少数地址上
    assert 0 < len(addresses) <= 2, f"BR-03-28：收货地址聚集（实际 {addresses}）"
    sku_counts = {e["scene_extra"]["sku_count"] for e in events
                  if e["event_type"] == "order_create"}
    assert sku_counts <= {1, 2}, "BR-03-28：同 sku 高频"
    # `device_id` 只有 `order_create` 是必填（`order_pay` 不要求），因此只统计下单事件
    creates = [e for e in events if e["event_type"] == "order_create"]
    assert len({e["device_id"] for e in creates}) <= 6, "刷单设备同样聚集"


async def test_refund_mode_is_dense_not_received_applications():
    """`refund`：老账号高额订单 → 密集 `reason_code=not_received` 的售后申请。"""
    anchor = 1_800_000_000_000
    profiles = build_profiles(MODE_REFUND, 42)
    events = [build_event(MODE_REFUND, 42, i, anchor + i * 100, profiles)
              for i in range(20)]
    after_sales = [e for e in events if e["event_type"] == "after_sale_apply"]
    assert after_sales, "refund 模式必须产出售后申请"
    assert {e["scene_extra"]["reason_code"] for e in after_sales} == {"not_received"}
    assert all(e["biz_no"] for e in after_sales), "BR-03-03：售后必须带 biz_no"
    assert all(e["amount"] >= 29000 for e in after_sales), "BR-03-28：高额订单"


@pytest.mark.parametrize("mode", MODES)
async def test_every_generated_event_passes_the_same_validation(mode):
    """BR-03-29 的静态面：模拟器产出的事件**全部**能过接入校验。

    若模拟器自己拼事件时漏了必填字段，这里会直接抛出 `EVT-4xxx`——
    比"跑起来才发现 failed_cnt 不为 0"更早暴露。
    """
    anchor = now_ms() - 60_000
    profiles = build_profiles(mode, 42)
    for i in range(30):
        payload = build_event(mode, 42, i, anchor + i * 100, profiles)
        event = validate_event(payload, anchor + i * 100)
        assert event["event_type"]
        assert event["source"] == "mock_biz"


# ------------------------------------------------------------ V-03-13 同参数同决策
async def test_simulator_event_and_manual_post_get_the_same_decision(client):
    """V-03-13 / BR-03-29：模拟器产出的事件与手工 POST 同参数事件决策一致。

    做法：用同一个 `build_event` 产出一份载荷，一份经模拟器入口
    （`event_service.ingest(payload, source="mock_biz")`）、一份经 HTTP 接口，
    断言决策块除 `elapsed_ms`（每次求值耗时天然不同）与 `snapshot_id`
    （04 的取值，两次求值天然递增）外完全相同。
    """
    install_fakes()
    anchor = now_ms() - 10_000
    payload = build_event(MODE_WOOL, 42, 3, anchor, build_profiles(MODE_WOOL, 42))

    via_simulator = await event_service.ingest(dict(payload), source="mock_biz")
    manual = dict(payload)
    manual["source"] = "mock_biz"
    via_http = (await post_event(client, manual)).json()["data"]

    for field in DECISION_FIELDS:
        # `elapsed_ms` 是每次求值耗时，`snapshot_id` 是 04 每次调用自增的编号：
        # 两者天然不同，与本命题（决策口径一致）无关
        if field in ("elapsed_ms", "snapshot_id"):
            continue
        assert via_simulator[field] == via_http[field], f"字段 {field} 不一致"
    assert via_simulator["decision"] == via_http["decision"]
    assert via_simulator["final_score"] == via_http["final_score"]
    assert via_simulator["degrade"] is None and via_http["degrade"] is None


async def test_simulator_does_not_bypass_validation(client):
    """BR-03-29：模拟器**不实现任何判定逻辑**——它只把产出交给接入层。

    用一个注入的假投喂入口证明两件事：① 模拟器**不写库**（真实落库只发生在
    接入层）；② 模拟器送出的载荷必须能过接入校验，坏数据在这里就会抛出来。
    """
    seen: list[dict] = []

    async def fake_ingest(payload: dict) -> dict:
        seen.append(payload)
        # 走真实校验：坏数据必须在这里抛出来，而不是被模拟器吞掉
        validate_event(dict(payload), now_ms())
        return {"decision": "review"}

    collect = event_service.get_event_service().ingest
    event_service.get_event_service().ingest = fake_ingest
    sim = get_simulator()
    sim.set_ingest(lambda payload: event_service.get_event_service().ingest(payload))
    try:
        r = await start_sim(client, mode=MODE_BRUSH, max_events=3, rate=50)
        assert r.status_code == 200
        await wait_until(lambda: len(seen) >= 3)
    finally:
        await client.post(MOCK_STOP, headers=WRITER)
        event_service.get_event_service().ingest = collect
        sim.set_ingest(lambda payload: event_service.ingest(
            payload, source="mock_biz"))

    assert len(seen) >= 3
    # 换过假投喂入口：这一轮**一条都没落库**（证明模拟器自己不写库）
    await event_service.flush()
    assert await db.get_db()[COLL_RISK_EVENTS].count_documents({}) == 0
    assert all(item["event_type"] in {"order_create", "order_pay"} for item in seen)
    assert all(item["user_id"] for item in seen)
    # `ts` 必须来自虚拟时钟（BR-03-25），因此一定早于"现在"而不是浮点时间
    assert all(isinstance(item["ts"], int) for item in seen)


async def test_simulator_survives_one_bad_event(client):
    """BR-03-16 的精神：单条投喂失败只计入 `failed`，不让整条事件流停摆。"""
    calls = {"n": 0}

    async def flaky_ingest(payload: dict) -> dict:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated downstream failure")
        return {"decision": "review"}

    sim = get_simulator()
    sim.set_ingest(flaky_ingest)
    try:
        await start_sim(client, mode=MODE_NORMAL, max_events=4, rate=50)
        await wait_until(lambda: sim.emitted >= 4)
        status = sim.status()
        assert status["decision_counts"]["failed"] == 1
        assert status["decision_counts"]["review"] == 3
        assert sum(status["decision_counts"].values()) == status["emitted"] == 4
    finally:
        await client.post(MOCK_STOP, headers=WRITER)
        sim.set_ingest(lambda payload: event_service.ingest(
            payload, source="mock_biz"))


async def test_simulator_emitted_total_accumulates_across_runs(client):
    """`emitted_total` 是跨运行的累计量（08 页状态行要用它展示总产出）。"""
    await start_sim(client, max_events=3, rate=50)
    sim = get_simulator()
    await wait_until(lambda: sim.emitted >= 3)
    first = (await client.post(MOCK_STOP, headers=WRITER)).json()["data"]
    assert first["emitted"] == 3

    await start_sim(client, max_events=2, rate=50)
    await wait_until(lambda: sim.emitted >= 2)
    second = (await client.post(MOCK_STOP, headers=WRITER)).json()["data"]
    assert second["emitted"] == 2
    assert second["emitted_total"] == 5, "累计量必须在第二次 start 后仍保留"


async def test_simulator_emits_virtual_clock_ts(client):
    """BR-03-25：落库的 `ts` 来自虚拟时钟而非 `now`（序列可复现的关键）。"""
    anchor = now_ms() - 3_600_000        # 一小时前的锚点
    await start_sim(client, mode=MODE_NORMAL, max_events=3, rate=10,
                    anchor_ts=anchor)
    sim = get_simulator()
    await wait_until(lambda: sim.emitted >= 3)
    await client.post(MOCK_STOP, headers=WRITER)
    await event_service.flush()

    rows = await db.get_db()[COLL_RISK_EVENTS].find({}).sort("ts", 1).to_list(length=10)
    assert [r["ts"] for r in rows] == [anchor + 100 * i for i in range(3)]
    assert sim.status()["anchor_ts"] == anchor


async def test_simulator_stop_does_not_write_any_collection(client):
    """BR-03-31：`stop` 的汇总**只写日志**，不写任何集合。

    E17/E18（仿真用例与执行记录）归模块 10；模块 03 越界写它们会让
    "10 的执行记录"里混进不是它写的行。
    """
    from app.constants import COLL_SIM_CASES, COLL_SIM_RUNS

    await start_sim(client, max_events=2, rate=50)
    sim = get_simulator()
    await wait_until(lambda: sim.emitted >= 2)
    await client.post(MOCK_STOP, headers=WRITER)
    for coll in (COLL_SIM_CASES, COLL_SIM_RUNS):
        assert await db.get_db()[coll].count_documents({}) == 0, f"{coll} 不该被模块 03 写入"
