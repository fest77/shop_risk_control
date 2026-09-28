# -*- coding: utf-8 -*-
"""模块 04 真实链路验收：服务层组装、快照落库、HTTP 接口、sweep、统计基线。

覆盖 `V-04-01`（发事件产生快照）、`V-04-11`（计算失败不伪造 0 且 03 降级）、
`V-04-12`（落库失败不阻塞 + 重试队列）、`V-04-13`（`/features/meta` 下发基线），
以及 `FEA-4001/4004`、权限、E21 统计基线的 `sample_size < 100` 规则。

## 为什么这里必须走真实的服务与数据库

纯逻辑用例证明了算法正确，但"快照是否真的落库""接口是否真的能读到"
只有真实链路能证明。尤其是 `V-04-12`：它断言的是**落库失败之后**系统仍然
正常（决策已返回、失败进了重试队列），这条只能在有真实落库路径时测。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from app import db
from app.constants import COLL_FEATURE_BASELINES, COLL_FEATURE_SNAPSHOTS
from app.core.feature_sweep_task import refresh_baselines, sweep_once
from app.engine.feature_compute import FEATURE_DATA_TYPES, FEATURE_KEYS, ProfileData
from app.errors import FEA_CODES, FEA_NOTICE
from app.protocols import configure_components, get_components
from app.repos.feature_repo import (
    MIN_BASELINE_SAMPLE,
    FeatureRepo,
    MongoProfileReader,
)
from app.services import feature_service
from app.services.feature_service import (
    DECISION_GATING_FEATURES,
    FeatureService,
    build_feature_meta,
    empty_snapshot,
    require_event_id,
    snapshot_id_fallback,
)
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER
from tests.event_testlib import (
    PAYLOAD_FACTORIES,
    component_reset,
    coupon_payload,
    login_payload,
    reset_runtime_state,
)
from tests.feature_testlib import (
    ANCHOR_TS,
    FULL_PROFILE,
    CountingSnapshotRepo,
    ExplodingProfileReader,
    FakeLinkedUserCountProvider,
    FakeProfileReader,
    FailingSnapshotRepo,
)

pytestmark = pytest.mark.anyio

FEATURES_URL = "/api/v1/features"

#: E02 的 18 个键名（与 `test_feature_engine.py` 的常量同源；此处再列一份是为了
#: 让"接口下发的键名"与"文档的键名"在本文件里也能独立比对）
E02_KEYS: tuple[str, ...] = (
    "login_cnt_1h", "coupon_cnt_1h", "order_cnt_1h", "order_cnt_24h",
    "pay_fail_cnt_24h", "aftersale_cnt_24h", "aftersale_rate_24h",
    "device_user_cnt", "device_order_cnt_1h", "device_age_hours",
    "ip_user_cnt", "ip_order_cnt_1h", "ip_is_proxy",
    "address_user_cnt", "address_aftersale_cnt",
    "user_age_days", "user_level", "user_risk_tag_cnt",
)


@pytest.fixture(autouse=True)
async def feature_state():
    """逐用例复位：组件、窗口、注入依赖、E02/E21 集合。

    `tests/conftest.py` 的清库列表里没有 E02/E21（那是新增集合），
    因此在**本文件**的夹具体里清——放在 conftest 会牵动既有测试的行为，
    而"快照表里有没有别的用例留下的行"会让按 `event_id` 的断言变得不可靠。
    """
    component_reset()
    reset_runtime_state()
    service = feature_service.get_feature_service()
    # `component_reset()` 装的是"明确不可用"的 04（那是模块 03 的 fail-closed
    # 用例需要的装配）。本文件测的是**真实的 04**，因此显式把它装回链路——
    # 这也正是生产默认装配（`protocols.default_feature_provider`）的行为。
    configure_components(feature_provider=service)
    service.window.reset()
    # 解除用例可能注入的假仓储：`_repo` 是实例属性，一旦被某个用例替换，
    # 后面的用例会继续用它（表现为"单独跑通过、一起跑失败"的幽灵缺陷）
    service._repo = FeatureService._repo.__get__(service)
    service._profile_reader = None
    service._linked_provider = None
    service._retry_queue.clear()
    service.stats.update({
        "computed": 0, "ingested": 0, "duplicate": 0, "errors": 0,
        "degraded": 0, "persisted": 0, "persist_failed": 0, "requeued": 0,
    })
    await db.get_db()[COLL_FEATURE_SNAPSHOTS].delete_many({})
    await db.get_db()[COLL_FEATURE_BASELINES].delete_many({})
    yield
    await service.flush()
    service._repo = FeatureService._repo.__get__(service)
    service._profile_reader = None
    service._linked_provider = None
    service.window.reset()
    component_reset()
    await db.get_db()[COLL_FEATURE_SNAPSHOTS].delete_many({})
    await db.get_db()[COLL_FEATURE_BASELINES].delete_many({})


def install_inputs(
    *,
    profile: Optional[ProfileData] = None,
    linked: Optional[dict[str, Optional[int]]] = None,
) -> tuple[FakeProfileReader, FakeLinkedUserCountProvider]:
    """给真实的服务单例注入假画像与假 09（其余部分走真实实现与真实 Mongo）。"""
    service = feature_service.get_feature_service()
    reader = FakeProfileReader(profile or ProfileData.empty())
    provider = FakeLinkedUserCountProvider(linked or {})
    service._profile_reader = reader
    service._linked_provider = provider
    return reader, provider


def internal_event(event_id: str, **over: Any) -> dict:
    """构造内部事件（形状同 `validate_event` 的产物）。"""
    base: dict[str, Any] = {
        "_id": event_id,
        "event_type": "order_create",
        "user_id": "u_pipe",
        "ts": now_ms(),
        "device_id": "D_PIPE",
        "ip": "198.51.100.30",
        "address_id": "A_PIPE",
        "scene_extra": {},
    }
    base.update(over)
    return base


# ============================================================
# 默认装配
# ============================================================
async def test_default_feature_provider_is_the_real_module_04():
    """默认装配必须装上**真实的**模块 04（而不是"不可用"占位）。

    断言的是 `Components` 的**字段默认值**（新建一个实例），而不是进程内单例：
    单例可能被别的用例的夹具改成"不可用"（那是模块 03 的降级用例需要的装配），
    用它来断言默认装配会把"用例执行顺序"引入结论。
    """
    from app.protocols import Components, UnavailableFeatureProvider, default_feature_provider

    fresh = Components()
    assert isinstance(fresh.feature_provider, FeatureService), (
        f"默认 feature_provider 应为 FeatureService，实际 {type(fresh.feature_provider).__name__}"
    )
    assert isinstance(default_feature_provider(), FeatureService)
    assert fresh.describe()["feature_provider"] == "FeatureService"
    # 05 已落地（决策 D45）：默认必须是它的**真实实现**，而不是占位实现。
    # 继续断言占位就等于把"05 还没做"写进验收，而"名单 → 条件树 → 累加 → 仲裁"
    # 这条真实链路将永远不被默认装配覆盖。
    assert type(fresh.decision_provider).__name__ == "RuleDecisionProvider"
    assert getattr(fresh.decision_provider, "available") is True
    # 09 已落地（决策 D45）：默认必须是它的**真实实现**，而不是占位实现。
    # 语义断言换成"真实实现能给出确定答案"：不支持的实体类型才是 `None`
    # （= 无法计算），而一个没有任何边、也没有画像行的设备，关联账号数是
    # **可证实的 0**（0 在这里是结论，不是"用 0 冒充未知"）。
    linked = fresh.linked_user_count_provider
    assert type(linked).__name__ == "MongoLinkedUserCountProvider"
    assert linked.available is True
    assert await linked.get_linked_user_count("phone", "139****0001") is None, (
        "手机号不是图节点：给不出关联账号数时必须是 None（无法计算），不是 0"
    )
    assert await linked.get_linked_user_count("device", "D_NO_SUCH_DEVICE") == 0
    # 兜底路径：04 不可用时必须回落到显式的"不可用"实现（而不是让应用起不来）
    assert isinstance(UnavailableFeatureProvider(), object)


async def test_service_satisfies_both_protocols():
    """04 同时满足 `FeatureProvider`（03 用）与 `FeatureStore` 的兼容面（§3.1）。"""
    from app.protocols import FeatureProvider, FeatureStore, LinkedUserCountProvider

    service = feature_service.get_feature_service()
    assert isinstance(service, FeatureProvider)
    assert isinstance(service, FeatureStore)
    assert isinstance(
        get_components().linked_user_count_provider, LinkedUserCountProvider
    )
    # `FeatureStore` 的四个方法都在
    for name in ("record" if hasattr(service, "record") else "ingest",
                 "window_stats", "baseline"):
        assert callable(getattr(service, name)), f"缺少 {name}"
    stats = await service.window_stats("user", "u1", 60)
    assert "dimensions" in stats and "estimated_bytes" in stats


# ============================================================
# V-04-01 / V-04-03 真实链路：快照产出与当前事件计入
# ============================================================
async def test_compute_returns_full_contract_and_persists_snapshot():
    """`V-04-01`：算一次 → 契约 6 个键齐全 + `feature_snapshots` 新增一条含 18 项。"""
    install_inputs(
        profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1},
    )
    service = feature_service.get_feature_service()
    event = internal_event("EVT" + "0" * 20)

    snapshot = await service.compute(event)

    # 与 `app/protocols.FeatureProvider` 的契约**逐键**核对（03 读的就是这些键）
    for key in ("snapshot_id", "features", "missing_features", "window_config",
                "compute_ms", "degrade_suggested"):
        assert key in snapshot, f"契约缺键 {key}"
    assert snapshot["snapshot_id"].startswith("SNP")
    assert snapshot["degrade_suggested"] is False, "画像齐全时不该建议降级"
    assert set(snapshot["missing_features"]) == set(), (
        f"画像与 09 都给全了，不该有缺失：{snapshot['missing_features']}"
    )
    assert set(snapshot["features"]) == set(E02_KEYS), "features 必须恰好 18 项"
    assert snapshot["features"]["order_cnt_1h"] == 1, (
        "V-04-03：当前事件必须计入窗口（首发下单 → 1 而不是 0）"
    )
    assert snapshot["window_config"]["long_window_min"] == 1440

    assert await service.flush() is True, "落库任务应能收尾"
    doc = await db.get_db()[COLL_FEATURE_SNAPSHOTS].find_one(
        {"event_id": event["_id"]}
    )
    assert doc is not None, "V-04-01：快照必须落进 feature_snapshots"
    assert doc["_id"] == snapshot["snapshot_id"]
    assert set(doc["features"]) == set(E02_KEYS)
    assert doc["window_config"]["short_window_min"] == 60


async def test_default_provider_marks_three_linked_features_missing():
    """默认 09 提供者下，三项聚集度**如实进缺失**（BR-04-07/09）。"""
    install_inputs(profile=FULL_PROFILE)
    service = feature_service.get_feature_service()
    snapshot = await service.compute(internal_event("EVT" + "1" * 20))

    for key in ("device_user_cnt", "ip_user_cnt", "address_user_cnt"):
        assert key in snapshot["missing_features"], f"{key} 应在缺失列表里"
        assert key not in snapshot["features"], f"{key} 不得用 0 冒充"
    assert snapshot["missing_reasons"]["device_user_cnt"]
    await service.flush()


async def test_repeated_compute_does_not_double_count():
    """重复算同一事件不重复计数（§3.1 的幂等），且第二份快照不再落库。"""
    install_inputs(linked={"device": 1, "ip": 1, "address": 1})
    service = feature_service.get_feature_service()
    event = internal_event("EVT" + "2" * 20, event_type="coupon_receive")

    first = await service.compute(event)
    second = await service.compute(event)
    await service.flush()

    assert first["features"]["coupon_cnt_1h"] == 1
    assert second["features"]["coupon_cnt_1h"] == 1, "重复 ingest 不得把计数翻倍"
    count = await db.get_db()[COLL_FEATURE_SNAPSHOTS].count_documents({})
    assert count == 1, f"同一事件只应有一份快照（uq_event 唯一索引），实际 {count}"


# ============================================================
# V-04-11 计算异常不伪造 0，且 03 因此降到 review
# ============================================================
async def test_compute_exception_returns_subset_and_suggests_degrade(monkeypatch):
    """`V-04-11` / FEA-5001：计算抛异常 → 标记降级、返回可用子集、**不伪造 0**。"""
    install_inputs(linked={"device": 1, "ip": 1, "address": 1})
    service = feature_service.get_feature_service()

    def boom(*args, **kwargs):
        raise TypeError("simulated field type error")

    monkeypatch.setattr(feature_service, "compute_features", boom)
    snapshot = await service.compute(internal_event("EVT" + "3" * 20))

    assert snapshot["degrade_suggested"] is True, "FEA-5001 必须建议降级"
    assert snapshot["features"] == {}, (
        "算不出来时**绝不能**返回各项为 0 的假特征（那会让规则全部不命中）"
    )
    assert set(snapshot["missing_features"]) == set(FEATURE_KEYS), (
        "18 项必须全部进缺失，而不是悄悄消失"
    )
    assert snapshot["status"] == "error"
    assert FEA_NOTICE["FEA-5001"] in snapshot["error"]


async def test_single_feature_error_keeps_the_other_seventeen(monkeypatch):
    """单项异常只让该项缺失，其余 17 项照常产出（FEA-5001 的"可用子集"）。"""
    import app.engine.feature_compute as compute_mod

    install_inputs(profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1})
    service = feature_service.get_feature_service()

    original = compute_mod.WindowView.count_short

    def broken(self, dimension: str, event_type: str) -> int:
        if event_type == "login":
            raise ValueError("simulated bad value")
        return original(self, dimension, event_type)

    monkeypatch.setattr(compute_mod.WindowView, "count_short", broken)
    snapshot = await service.compute(
        internal_event("EVT" + "4" * 20, event_type="coupon_receive")
    )

    assert "login_cnt_1h" in snapshot["missing_features"], "异常项必须进缺失"
    assert "login_cnt_1h" not in snapshot["features"]
    assert snapshot["features"]["coupon_cnt_1h"] == 1, "其余特征必须仍然可用"
    assert snapshot["degrade_suggested"] is True
    await service.flush()


async def test_gating_feature_missing_suggests_degrade():
    """关键特征（`ip_is_proxy`）缺失时建议降级——它的缺失本身就是风险项。"""
    install_inputs(linked={"device": 1, "ip": 1, "address": 1})
    assert DECISION_GATING_FEATURES == ("ip_is_proxy",)
    service = feature_service.get_feature_service()
    snapshot = await service.compute(internal_event("EVT" + "5" * 20))
    assert "ip_is_proxy" in snapshot["missing_features"]
    assert snapshot["degrade_suggested"] is True
    assert snapshot["status"] == "degraded"
    await service.flush()


async def test_cold_start_without_gating_feature_does_not_degrade():
    """冷启动（缺年龄/等级/聚集度）**不应**触发降级：那是数据不足，不是算错。"""
    install_inputs(profile=ProfileData(ip_is_proxy=False))
    service = feature_service.get_feature_service()
    snapshot = await service.compute(
        internal_event("EVT" + "6" * 20, address_id=None)
    )
    assert "user_age_days" in snapshot["missing_features"], "冷启动必然缺画像项"
    assert "ip_is_proxy" not in snapshot["missing_features"]
    assert snapshot["degrade_suggested"] is False, (
        "画像缺失不触发降级（否则每个新用户都被打成 review，信号即刻作废）"
    )
    await service.flush()


async def test_exploding_profile_reader_keeps_frequency_features():
    """画像读取抛异常：行为频次照常产出，画像类特征缺失并写明"读取失败"。

    这条钉住的是"依赖故障 ≠ 整次计算失败"：若让异常冒泡，服务层只能交出一份
    空特征（`FEA-5001` 的兜底），而实际上 18 项里有 10 项根本不依赖画像。
    """
    service = feature_service.get_feature_service()
    reader = ExplodingProfileReader()
    service._profile_reader = reader
    service._linked_provider = FakeLinkedUserCountProvider(
        {"device": 1, "ip": 1, "address": 1}
    )
    event = internal_event("EVT" + "8" * 20, event_type="order_create")
    snapshot = await service.compute(event)

    assert reader.calls == 1, "画像读取应被真实调用过"
    assert snapshot["features"]["order_cnt_1h"] == 1, "行为频次不受画像故障影响"
    assert snapshot["features"]["device_order_cnt_1h"] == 1
    assert snapshot["features"]["device_user_cnt"] == 1, "09 给的值照常可用"
    for key in ("user_age_days", "user_level", "user_risk_tag_cnt",
                "ip_is_proxy", "address_aftersale_cnt"):
        assert key in snapshot["missing_features"], f"{key} 应标记缺失"
        assert key not in snapshot["features"], f"{key} 不得用 0 冒充"
    # `device_age_hours` 是**唯一有回落依据**的画像项：E11 读不到时用窗口内
    # 最早出现时间（本次事件自己，约 0 小时），因此它是真实值而不是缺失
    assert "device_age_hours" not in snapshot["missing_features"]
    assert snapshot["features"]["device_age_hours"] == pytest.approx(0.0, abs=0.01)
    # 缺失原因必须能区分"库里没有"与"读库失败"——两者对研判的含义不同
    assert "读取失败" in snapshot["missing_reasons"]["user_age_days"], (
        snapshot["missing_reasons"]
    )
    await service.flush()


async def test_empty_profile_collection_without_ip_marks_gating_missing():
    """画像库完全为空（真实冷启动）：行为频次有值，关键特征缺失并建议降级。"""
    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData.empty())
    service._linked_provider = FakeLinkedUserCountProvider({})
    event = internal_event("EVT" + "B" * 20, event_type="coupon_receive")
    snapshot = await service.compute(event)

    assert snapshot["features"]["coupon_cnt_1h"] == 1, "事件本身的计数必须算得出来"
    assert "ip_is_proxy" in snapshot["missing_features"]
    assert snapshot["degrade_suggested"] is True, (
        "关键特征缺失（不知道是不是代理出口）时必须建议转人工"
    )
    await service.flush()


async def test_event_pipeline_degrades_to_review_when_compute_raises(client, monkeypatch):
    """`V-04-11` 的**真实链路**：04 计算异常 → 03 降级 `review`（不是 pass）。"""
    def boom(*args, **kwargs):
        raise TypeError("simulated compute failure")

    monkeypatch.setattr(feature_service, "compute_features", boom)
    response = await client.post("/api/v1/events", json=coupon_payload(), headers=WRITER)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["decision"] == "review", "降级必须转人工，绝不能 pass"
    assert data["decision"] != "pass"
    assert data["degrade"] is not None
    assert data["degrade"]["stage"] == "feature"
    assert "不完整" in data["degrade"]["reason"], data["degrade"]["reason"]


async def test_event_pipeline_uses_real_features_end_to_end(client):
    """真实链路（真实 04 + 真实 05）：特征真的算出来了，决策也真的被引擎给出。

    这条证明默认装配下 04 与 05 **都在链路上**：`degrade` 为 `null`（没有任何
    环节短路），决策块的 `engine_version` 是 `rule-engine-v1`、`snapshot_id`
    是 04 真实落库的快照编号。05 落地前这里断言的是 `stage=rule`——现在那句
    不再是事实，而**判别力没有降低**：`engine_version` 只有真实 05 才写得出来
    （03 自造的降级块写 `degraded`，占位实现只会抛异常）。
    """
    # 必须把画像喂全：`ip_is_proxy` 是关键特征（缺失即建议降级），
    # 少了它这条用例会走到降级分支，测的就不是"两侧都真的在工作"了
    install_inputs(profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1})
    response = await client.post("/api/v1/events", json=login_payload(), headers=WRITER)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["degrade"] is None, (
        f"04 给了完整快照、05 已落地，链路上不该再有降级环节：{data['degrade']}"
    )
    assert data["engine_version"] == "rule-engine-v1", (
        "决策块必须来自真实的规则引擎（03 的自造降级块写的是 degraded）"
    )
    assert data["model_score"] is None, "AD-09：模型引擎是空实现"
    assert isinstance(data["rule_score"], int) and data["final_score"] == data["rule_score"]
    assert data["snapshot_id"], "决策块里的快照编号由 04 的快照契约提供"
    # 04 真的产出并落库了快照（这是"04 在工作"的直接证据）
    service = feature_service.get_feature_service()
    assert await service.flush() is True
    doc = await db.get_db()[COLL_FEATURE_SNAPSHOTS].find_one(
        {"event_id": data["event_id"]}
    )
    assert doc is not None, "真实 04 必须落下一份快照"
    assert doc["snapshot_id"].startswith("SNP")
    assert doc["_id"] == doc["snapshot_id"], "E02 的主键就是快照编号"
    assert data["snapshot_id"] == doc["snapshot_id"], (
        "决策块里的 snapshot_id 必须与 04 落库的那份一致（三段贯通的证据）"
    )


# ============================================================
# V-04-12 落库失败不阻塞 + 重试队列
# ============================================================
async def test_persist_failure_does_not_block_and_enters_retry_queue():
    """`V-04-12` / BR-04-23：落库失败 → 决策已返回、失败计数 +1、重试队列 +1。"""
    install_inputs(profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1})
    service = feature_service.get_feature_service()
    failing = FailingSnapshotRepo()
    service._repo = lambda: failing       # type: ignore[method-assign]

    snapshot = await service.compute(internal_event("EVT" + "7" * 20))
    # compute 必须**已经返回**（落库是后台旁路）
    assert snapshot["snapshot_id"], "落库失败不得影响同步返回的快照"
    assert set(snapshot["features"]) == set(E02_KEYS)

    assert await service.flush() is True
    assert failing.attempts == feature_service.PERSIST_RETRY, (
        f"应重试 {feature_service.PERSIST_RETRY} 次，实际 {failing.attempts}"
    )
    assert service.stats["persist_failed"] == 1
    assert len(service._retry_queue) == 1, "失败快照必须进重试队列（最终一致）"
    assert service._retry_queue[0]["event_id"] == "EVT" + "7" * 20


async def test_retry_queue_is_bounded():
    """重试队列必须有界：Mongo 长时间不可用时，无界队列会把内存吃光。"""
    service = feature_service.get_feature_service()
    for index in range(feature_service.RETRY_QUEUE_MAX + 5):
        service._enqueue_retry({"_id": f"SNP{index}", "event_id": f"EVT{index}"})
    assert len(service._retry_queue) == feature_service.RETRY_QUEUE_MAX
    # 丢的是最旧的（最新数据对排障最有用）
    assert service._retry_queue[-1]["_id"] == f"SNP{feature_service.RETRY_QUEUE_MAX + 4}"


async def test_retry_pending_succeeds_after_recovery():
    """重试队列里的快照在库恢复后应能被真正写入（不是只躺在内存里）。"""
    service = feature_service.get_feature_service()
    good = CountingSnapshotRepo()
    service._repo = lambda: good         # type: ignore[method-assign]

    service._enqueue_retry({"_id": "SNP_RETRY", "event_id": "EVT_RETRY",
                            "features": {}, "feature_snapshot": None})
    written = await service.retry_pending()
    assert written == 1
    assert good.docs[0]["_id"] == "SNP_RETRY"
    assert service._retry_queue == [], "成功之后队列应清空"


async def test_pipeline_still_returns_decision_when_snapshot_persist_fails(client):
    """`V-04-12` 的链路面：快照写不进库时，事件接口仍返回 200 + 决策。"""
    from app.protocols import configure_components

    install_inputs(profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1})
    service = feature_service.get_feature_service()
    failing = FailingSnapshotRepo()
    service._repo = lambda: failing       # type: ignore[method-assign]
    configure_components(feature_provider=service)
    try:
        response = await client.post("/api/v1/events", json=coupon_payload(),
                                     headers=WRITER)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        # 快照落库全失败**不影响决策**：这里拿到的是 05 的真实决策
        # （05 落地前是"05 不可用 → 降级 review"；判别依据是 engine_version）
        assert data["decision"] in {"pass", "review", "reject"}
        assert data["engine_version"] == "rule-engine-v1"
        assert data["event_id"].startswith("EVT")
        # 快照落库全失败，但决策照常返回（BR-04-23：不回滚、不阻塞）
        await service.flush()
        assert failing.attempts >= 1, "落库确实被试过"
        assert service.stats["persist_failed"] == 1
        assert len(service._retry_queue) == 1, "失败快照必须进重试队列"
    finally:
        await service.flush()


# ============================================================
# §3.2 接口：GET /features/{event_id}
# ============================================================
async def test_get_snapshot_after_event_is_persisted(client):
    """`V-04-01` 的接口面：发一个事件 → `feature_snapshots` 有行 → 接口能读到 18 项。"""
    install_inputs(profile=FULL_PROFILE, linked={"device": 2, "ip": 3, "address": 1})
    response = await client.post("/api/v1/events", json=coupon_payload(), headers=WRITER)
    assert response.status_code == 200, response.text
    event_id = response.json()["data"]["event_id"]
    service = feature_service.get_feature_service()
    assert await service.flush() is True

    got = await client.get(f"{FEATURES_URL}/{event_id}", headers=READER)
    assert got.status_code == 200, got.text
    data = got.json()["data"]
    assert data["snapshot_id"].startswith("SNP")
    assert data["event_id"] == event_id
    # 18 项键名**恒在**：能算的在 features 里，算不出的在 missing_features 里
    assert set(data["features"]) | set(data["missing_features"]) == set(E02_KEYS), (
        "接口必须覆盖 18 项（缺失项出现在 missing_features，而不是凭空消失）"
    )
    assert not (set(data["features"]) & set(data["missing_features"]))
    assert data["window_config"]["long_window_min"] == 1440
    assert isinstance(data["missing_features"], list)
    assert data["compute_ms"] >= 0
    assert data["features"]["coupon_cnt_1h"] == 1
    assert data["features"]["device_user_cnt"] == 2, "09 给的值原样透传"
    assert data["features"]["ip_user_cnt"] == 3


async def test_get_snapshot_404_with_fea_4004(client):
    """§5 / FEA-4004：查不到快照 → 404 + 明确提示（这是正常状态，不是故障）。"""
    response = await client.get(f"{FEATURES_URL}/EVT{'9' * 20}", headers=READER)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "FEA-4004"
    assert body["ok"] is False
    assert body["message"] == FEA_CODES["FEA-4004"][1]
    assert body["data"]["event_id"] == "EVT" + "9" * 20
    assert body["trace_id"]


async def test_get_snapshot_422_with_fea_4001(client):
    """§5 / FEA-4001：编号格式非法 → 422（而不是框架的 `COM-4001`）。"""
    for bad in ("abc", "EVT123", "SNP20240101000000000001", "EVT" + "1" * 19):
        response = await client.get(f"{FEATURES_URL}/{bad}", headers=READER)
        assert response.status_code == 422, f"{bad} -> {response.status_code}"
        assert response.json()["code"] == "FEA-4001", f"{bad} -> {response.json()['code']}"


async def test_feature_endpoints_require_dashboard_read(client, bearer):
    """权限假设：两个读接口都用 `dashboard:read`（三角色可读，见 `feature_api`）。"""
    for headers in (WRITER, READER, ADMIN):
        response = await client.get(f"{FEATURES_URL}/meta", headers=headers)
        assert response.status_code == 200, f"{headers} -> {response.text}"

    # 未登录必须 401（而不是放行）
    anon = await client.get(f"{FEATURES_URL}/meta")
    assert anon.status_code == 401, anon.text


# ============================================================
# V-04-13 §3.3 接口：GET /features/meta
# ============================================================
async def test_meta_delivers_all_eighteen_items_with_baselines(client):
    """`V-04-13` / BR-04-16：`/features/meta` 下发 18 项，每项带基线（或显式"—"）。"""
    response = await client.get(f"{FEATURES_URL}/meta", headers=READER)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 18
    assert [item["key"] for item in data["items"]] == list(E02_KEYS), (
        "元数据的键名与顺序必须与 E02 一致"
    )

    for item in data["items"]:
        # 前端渲染表头所需的字段一个都不能少（BR-04-16：不得硬编码）
        for field in ("key", "label", "group", "unit", "data_type",
                      "baseline", "baseline_desc", "has_baseline", "direction_hint"):
            assert field in item, f"{item['key']} 缺少 {field}"
        assert item["label"] and item["group"], f"{item['key']} 缺中文标签或分组"
        assert item["baseline"] in ("__none__",) or item["baseline"], item["key"]
        if not item["has_baseline"]:
            assert item["baseline"] == "__none__", "无基线必须用哨兵值，前端显示 —"
            assert item["baseline_desc"], "无基线必须写明理由（BR-04-19）"

    by_key = {item["key"]: item for item in data["items"]}
    assert by_key["device_user_cnt"]["baseline"] == "≤1"
    assert by_key["order_cnt_24h"]["baseline"] == "2~5"
    assert by_key["ip_is_proxy"]["baseline"] == "false"
    assert by_key["aftersale_rate_24h"]["baseline"] == "≤5%"
    assert by_key["user_age_days"]["baseline"] == "__none__"
    assert by_key["user_age_days"]["has_baseline"] is False
    # E21 统计基线尚未刷新 → 必须是 null，不得用 0 冒充
    assert by_key["device_user_cnt"]["stat_baseline"] is None
    assert by_key["device_user_cnt"]["stat_baseline_available"] is False


async def test_meta_reports_stat_baseline_when_available(client):
    """E21 有足够样本时，`/features/meta` 的 `stat_baseline` 给出 P50/P95。"""
    repo = FeatureRepo(db.get_db())
    await repo.upsert_baselines([{
        "_id": repo.baseline_id("coupon_cnt_1h", "all"),
        "feature_name": "coupon_cnt_1h",
        "segment": "all",
        "p50": 1.0,
        "p95": 3.0,
        "sample_size": MIN_BASELINE_SAMPLE + 20,
        "window_days": 7,
        "computed_at": now_ms(),
    }])
    response = await client.get(f"{FEATURES_URL}/meta", headers=READER)
    items = {item["key"]: item for item in response.json()["data"]["items"]}
    stat = items["coupon_cnt_1h"]["stat_baseline"]
    assert stat is not None, "有足够样本时必须下发统计基线"
    assert stat["p50"] == 1.0 and stat["p95"] == 3.0
    assert stat["sample_size"] == MIN_BASELINE_SAMPLE + 20
    assert items["coupon_cnt_1h"]["stat_baseline_available"] is True


async def test_meta_hides_stat_baseline_below_sample_threshold(client):
    """E21：`sample_size < 100` 该基线**不可用** → 接口返回 `null`（不是 0）。"""
    repo = FeatureRepo(db.get_db())
    await repo.upsert_baselines([{
        "_id": repo.baseline_id("coupon_cnt_1h", "all"),
        "feature_name": "coupon_cnt_1h",
        "segment": "all",
        "p50": 1.0,
        "p95": 3.0,
        "sample_size": MIN_BASELINE_SAMPLE - 1,
        "window_days": 7,
        "computed_at": now_ms(),
    }])
    response = await client.get(f"{FEATURES_URL}/meta", headers=READER)
    items = {item["key"]: item for item in response.json()["data"]["items"]}
    assert items["coupon_cnt_1h"]["stat_baseline"] is None
    assert items["coupon_cnt_1h"]["stat_baseline_available"] is False

    # 服务层的 baseline() 同样必须返回 None（与 FeatureStore 的契约一致）
    service = feature_service.get_feature_service()
    assert await service.baseline("coupon_cnt_1h", "all") is None


async def test_meta_route_is_not_shadowed_by_event_id_route(client):
    """路由顺序守卫：`/features/meta` 不能被 `/features/{event_id}` 吞掉。

    若顺序反了，这里会得到一个 `FEA-4001`（把 "meta" 当事件编号校验）。
    """
    response = await client.get(f"{FEATURES_URL}/meta", headers=READER)
    assert response.status_code == 200
    assert response.json()["code"] == "OK"
    assert response.json()["data"]["total"] == 18


# ============================================================
# sweep 定时任务
# ============================================================
async def test_sweep_once_reclaims_expired_entries():
    """`V-04-09` 的服务面：`sweep_once()` 回收超长窗条目并清理空键。"""
    service = feature_service.get_feature_service()
    old = now_ms() - 25 * 3_600_000
    await service.window.ingest(internal_event("EVT_S1", ts=old, user_id="u_sweep_old"))
    await service.window.ingest(internal_event("EVT_S2", ts=now_ms(), user_id="u_sweep_new"))
    # 一条事件同时进它**确实带有**的四个维度（`internal_event` 四个实体都有值）；
    # 若某维度为 None 则不建桶——"不知道地址"与"地址是空串"必须分开
    assert "u_sweep_old" in service.window._queues["user"]
    assert "u_sweep_new" in service.window._queues["user"]
    assert service.window.stats()["distinct_keys"] == 5, "旧事件 4 个维度 + 新事件仅 user 不同"

    result = await sweep_once()
    assert result["error"] is None
    assert result["dropped"] == 4, "旧事件在四个维度上各留 1 条，应全部回收"
    assert "u_sweep_old" not in service.window._queues["user"], "空键必须被移除"
    assert "u_sweep_new" in service.window._queues["user"], "窗口内的数据不得被误删"
    assert service.window.stats()["last_sweep_at"] is not None
    assert service.window.stats()["distinct_keys"] == 4, "只剩新事件的四个键"


async def test_sweep_once_retries_pending_snapshots():
    """sweep 顺带推进重试队列（给失败的快照第二次机会）。"""
    service = feature_service.get_feature_service()
    service._enqueue_retry({
        "_id": "SNP_SWEEP", "event_id": "EVT_SWEEP",
        "features": {}, "window_config": {}, "computed_at": now_ms(),
        "missing_features": [], "compute_ms": 0, "user_id": "u",
    })
    result = await sweep_once()
    assert result["error"] is None
    assert result["retried"] == 1
    assert service._retry_queue == []


async def test_scheduler_start_and_stop_are_idempotent(monkeypatch):
    """调度器启停必须幂等（重复 start 不产生第二份任务；未启动 stop 也安全）。"""
    from app.core import feature_sweep_task

    calls: list[str] = []

    async def fake_sweep(now=None):
        calls.append("sweep")
        return {"swept_at": 0, "dropped": 0, "retried": 0,
                "memory_warned": False, "error": None}

    async def fake_baseline(now=None, *, window_days=7):
        calls.append("baseline")
        return {"written": 0, "error": None}

    monkeypatch.setattr(feature_sweep_task, "sweep_once", fake_sweep)
    monkeypatch.setattr(feature_sweep_task, "refresh_baselines", fake_baseline)

    scheduler = feature_sweep_task.FeatureSweepScheduler(
        interval_sec=0.05, baseline_hour=3, baseline_minute=0,
    )
    await scheduler.start()
    await scheduler.start()          # 幂等：不得重复启动
    assert len([t for t in scheduler._tasks if not t.done()]) == 2
    # 让两个后台协程各跑完第一轮（它们先跑一轮再进入周期性等待）
    await asyncio.sleep(0.08)
    await scheduler.stop()
    await scheduler.stop()           # 幂等：未运行时的 stop 是安全空操作
    assert scheduler._tasks == []
    assert "sweep" in calls and "baseline" in calls, (
        "启动后应立刻各跑一轮（覆盖停机期间攒下的数据）"
    )
    assert calls.count("sweep") <= 2, "重复 start 不得产生第二份 sweep 循环"


# ============================================================
# E21 统计基线刷新
# ============================================================
async def test_refresh_baselines_computes_p50_p95_from_snapshots():
    """`D9`：每日刷新按 E21 算 P50/P95，`segment` 固定 `all`，样本足够才写。"""
    repo = FeatureRepo(db.get_db())
    moment = now_ms()
    samples = list(range(100, 300))          # 200 条，值是 100..299
    docs = [
        {
            "_id": f"SNP{index:020d}",
            "event_id": f"EVT{index:020d}",
            "user_id": "u_base",
            "features": {"coupon_cnt_1h": value, "ip_is_proxy": bool(value % 2),
                         "user_level": "gold"},
            "window_config": {"short_window_min": 60, "long_window_min": 1440,
                              "agg_mode": "in_memory_sliding", "config_version": "w1"},
            "computed_at": moment - 60_000,
            "compute_ms": 1,
            "missing_features": [],
        }
        for index, value in enumerate(samples)
    ]
    await db.get_db()[COLL_FEATURE_SNAPSHOTS].insert_many(docs)

    result = await refresh_baselines()
    assert result["error"] is None
    assert result["scanned"] == len(docs)
    assert result["written"] >= 1
    assert "ip_is_proxy" in result["unavailable"], "布尔特征不做分位统计"
    assert "user_level" in result["unavailable"], "类别特征不做分位统计"

    stored = await repo.list_baselines("all")
    assert "coupon_cnt_1h" in stored
    doc = stored["coupon_cnt_1h"]
    assert doc["sample_size"] == len(samples)
    # 100..299 的中位数：pos=(200-1)*0.5=99.5 → 在 199 与 200 之间插值取半
    assert doc["p50"] == pytest.approx(199.5)
    assert doc["p95"] == pytest.approx(289.05)      # pos=189.05 → 289 + 0.05
    assert doc["segment"] == "all", "09 落地前分段固定为 all（见模块 docstring）"
    assert doc["window_days"] == 7


async def test_refresh_baselines_skips_and_removes_thin_samples():
    """样本不足（`<100`）的特征**不写基线**，并删除同名的旧记录。"""
    repo = FeatureRepo(db.get_db())
    # 先放一条"旧基线"，再灌入不足 100 条的快照
    await repo.upsert_baselines([{
        "_id": repo.baseline_id("coupon_cnt_1h", "all"),
        "feature_name": "coupon_cnt_1h", "segment": "all",
        "p50": 9.0, "p95": 99.0, "sample_size": 500,
        "window_days": 7, "computed_at": now_ms() - 86_400_000,
    }])
    await db.get_db()[COLL_FEATURE_SNAPSHOTS].insert_many([
        {
            "_id": f"SNPTHIN{index}", "event_id": f"EVTTHIN{index}", "user_id": "u",
            "features": {"coupon_cnt_1h": index}, "computed_at": now_ms(),
            "window_config": {}, "missing_features": [], "compute_ms": 0,
        }
        for index in range(5)
    ])

    result = await refresh_baselines()
    assert result["error"] is None
    assert "coupon_cnt_1h" in result["unavailable"]
    stored = await repo.list_baselines("all")
    assert "coupon_cnt_1h" not in stored, (
        "样本不足必须删掉旧基线，否则界面会显示一条过期的 P95"
    )


async def test_refresh_baselines_ignores_snapshots_outside_window():
    """窗口外的旧快照不参与统计（`window_days` 生效）。"""
    moment = now_ms()
    await db.get_db()[COLL_FEATURE_SNAPSHOTS].insert_many([
        {
            "_id": f"SNPOLD{index}", "event_id": f"EVTOLD{index}", "user_id": "u",
            "features": {"coupon_cnt_1h": 100},
            "computed_at": moment - 30 * 24 * 3_600_000,     # 30 天前
            "window_config": {}, "missing_features": [], "compute_ms": 0,
        }
        for index in range(200)
    ])
    result = await refresh_baselines()
    assert result["error"] is None
    assert result["scanned"] == 0, "30 天前的快照不该进 7 天窗口"


# ============================================================
# 其它契约细节
# ============================================================
async def test_require_event_id_enforces_the_pattern():
    """`FEA-4001` 的判定面：只有 `EVT + 20 位数字` 才算合法编号。"""
    assert require_event_id("EVT" + "1" * 20) == "EVT" + "1" * 20
    for bad in ("", None, "evt123", "EVT123", "SNP" + "1" * 20, "EVT" + "1" * 21):
        with pytest.raises(Exception) as caught:
            require_event_id(bad)
        assert getattr(caught.value, "code", None) == "FEA-4001", f"{bad!r} 未报 FEA-4001"


async def test_empty_snapshot_is_explicitly_unavailable():
    """`empty_snapshot` 的语义：明确"不可用"，而**不是**"特征全为 0"。"""
    snapshot = empty_snapshot({"_id": "EVT1", "user_id": "u1"}, "boom")
    assert snapshot["features"] == {}, "绝不能填 0"
    assert set(snapshot["missing_features"]) == set(FEATURE_KEYS)
    assert snapshot["degrade_suggested"] is True
    assert snapshot["snapshot_id"] is None


async def test_snapshot_id_fallback_is_deterministic():
    """兜底编号必须**确定**（同一事件重算得到同一编号 → 唯一索引如实报冲突）。"""
    first = snapshot_id_fallback(ANCHOR_TS)
    assert first == snapshot_id_fallback(ANCHOR_TS)
    assert first.startswith("SNP") and len(first) == 23


async def test_build_feature_meta_covers_every_key():
    """`build_feature_meta` 是接口与测试的共同真源：必须逐项覆盖 18 个键。"""
    items = build_feature_meta()
    assert [item["key"] for item in items] == list(FEATURE_KEYS)
    for item in items:
        assert FEATURE_DATA_TYPES[item["key"]] == item["data_type"]
        assert item["stat_baseline"] is None, "未传统计基线时必须为 null"


async def test_mongo_profile_reader_returns_none_for_missing_docs():
    """真实画像读取器：库里没记录时必须返回 `None`（进缺失），不得编造。"""
    reader = MongoProfileReader(db.get_db())
    profile = await reader.load({
        "user_id": "u_absent", "device_id": "D_absent",
        "ip": "203.0.113.250", "address_id": "A_absent",
    })
    assert profile.user_register_at is None
    assert profile.user_level is None
    assert profile.user_risk_tag_cnt is None
    assert profile.device_first_seen_at is None
    assert profile.ip_is_proxy is None
    assert profile.address_aftersale_cnt is None
    assert profile.errors == (), "查不到不是错误（冷启动是正常状态）"


async def test_mongo_profile_reader_reads_real_documents():
    """真实画像读取器：有记录时按 E10~E13 的字段读出正确值。"""
    moment = now_ms()
    await db.get_db()["users"].insert_one({
        "_id": "u_real", "register_at": moment - 5 * 86_400_000,
        "level": "vip", "risk_tags": ["new_account", "proxy_ip"],
    })
    await db.get_db()["devices"].insert_one({
        "_id": "D_real", "first_seen_at": moment - 72 * 3_600_000, "linked_user_cnt": 7,
    })
    await db.get_db()["ip_pool"].insert_one({
        "_id": "198.51.100.77", "is_proxy": False, "is_idc": True,
    })
    await db.get_db()["user_addresses"].insert_one({
        "_id": "A_real", "user_id": "u_real", "aftersale_cnt": 3, "linked_user_cnt": 2,
    })
    try:
        reader = MongoProfileReader(db.get_db())
        profile = await reader.load({
            "user_id": "u_real", "device_id": "D_real",
            "ip": "198.51.100.77", "address_id": "A_real",
        })
        assert profile.user_level == "vip"
        assert profile.user_risk_tag_cnt == 2
        assert profile.device_first_seen_at == moment - 72 * 3_600_000
        # `is_idc=True` 即为真：Spec §4.2 明确 `is_proxy`/`is_idc` 两者共同判定
        assert profile.ip_is_proxy is True
        assert profile.address_aftersale_cnt == 3
        assert profile.errors == ()
    finally:
        for coll in ("users", "devices", "ip_pool", "user_addresses"):
            await db.get_db()[coll].delete_many({"_id": {"$in": [
                "u_real", "D_real", "198.51.100.77", "A_real",
            ]}})


async def test_snapshot_written_by_event_pipeline_is_readable_by_detail(client):
    """03 的详情接口应能读到 04 落的快照（两个模块对 E02 的读写一致）。"""
    install_inputs(profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1})
    response = await client.post("/api/v1/events", json=coupon_payload(), headers=WRITER)
    event_id = response.json()["data"]["event_id"]
    service = feature_service.get_feature_service()
    await service.flush()

    detail = await client.get(f"/api/v1/events/{event_id}", headers=READER)
    assert detail.status_code == 200, detail.text
    snapshot = detail.json()["data"]["snapshot"]
    assert snapshot is not None, "详情接口必须能读到 04 落的快照"
    assert set(snapshot["features"]) | set(snapshot["missing_features"]) == set(E02_KEYS)


@pytest.mark.parametrize("event_type,factory", PAYLOAD_FACTORIES)
async def test_all_five_event_types_get_features(client, event_type, factory):
    """五类事件都要能算出特征快照（08 的仿真页要逐类展示）。

    这里不断言响应里的 `snapshot_id`：默认装配下 05 不可用，03 的降级路径
    由 `degraded_decision()` 自己造块（那是 05 的职责），拿不到 04 的编号。
    因此改为断言"**04 真的产出并落库了快照**"——那才是本模块的产出。
    """
    install_inputs(profile=FULL_PROFILE, linked={"device": 1, "ip": 1, "address": 1})
    response = await client.post("/api/v1/events", json=factory(), headers=WRITER)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    service = feature_service.get_feature_service()
    assert await service.flush() is True
    doc = await db.get_db()[COLL_FEATURE_SNAPSHOTS].find_one(
        {"event_id": data["event_id"]}
    )
    assert doc is not None, f"{event_type} 的快照未落库"
    assert doc["snapshot_id"].startswith("SNP"), f"{event_type} 未产出快照编号"
    assert set(doc["features"]) | set(doc["missing_features"]) == set(E02_KEYS)
