# -*- coding: utf-8 -*-
"""模块 13 运行参数 / 吞吐健康度 / 决策引擎配置的验收测试。

覆盖 Spec 13 §7 的 V-13-01 ~ V-13-10 与 V-13-18（其余属浏览器行为，见任务书 §5）：

| 用例 | 验收项 |
|---|---|
| `test_runtime_defaults_mirror_module_owners` | 默认值只在一处声明（镜像常量必须相等） |
| `test_v13_01_read_write_and_version_increment` | V-13-01 读写 + `config_version` +1 |
| `test_v13_02_window_order_rejected_atomically` | V-13-02 `SYS-4002` 且一个值都没保存 |
| `test_v13_02b_value_out_of_range_is_rejected` | BR-13-01 取值域 -> `SYS-4001` |
| `test_v13_03_applied_and_requires_restart` | V-13-03 / BR-13-02 生效方式如实返回 |
| `test_v13_04_ttl_change_clears_list_cache` | V-13-04 / BR-13-04 立即清缓存 |
| `test_v13_05_window_params_hot_update` | V-13-05 / BR-13-05 下一事件生效 |
| `test_v13_06_audit_exactly_one_with_before_after` | V-13-06 / D41 / BR-13-03 |
| `test_v13_07_atomic_save_keeps_all_old_values` | V-13-07 / `SYS-5001` |
| `test_v13_08_stats_exposes_four_cards` | V-13-08 / §3.3 |
| `test_v13_09_p95_over_threshold_is_visible` | V-13-09 / BR-13-27 |
| `test_v13_10_probe_timeout_does_not_break_card` | V-13-10 / BR-13-28 |
| `test_v13_18_model_engine_is_explicitly_rejected` | V-13-18 / BR-13-22 |
| `test_v13_20_*` | V-13-20 权限（reviewer/strategist -> 403） |
| `test_config_never_feeds_back_into_startup_config` | 任务书 §2③：不回灌启动期配置 |

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_system_config.py -q
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app import config, db
from app.constants import (
    COLL_AUDIT_LOGS,
    COLL_METRIC_BUCKETS,
    COLL_MODEL_CONFIGS,
    COLL_SYSTEM_CONFIG,
    RUNTIME_CONFIG_DEFAULTS,
    RUNTIME_CONFIG_ID,
)
from app.engine import feature_window, list_filter
from app.engine.metric_bucket import align, bucket_id, expire_at
from app.errors import SYS_CODES, SYS_NOTICE, AppError, ConfigSaveFailedError
from app.repos.config_repo import ConfigRepo
from app.repos.metric_repo import MetricRepo
from app.services import audit_service, config_service, metric_service
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

BASE = f"{config.API_PREFIX}/system"
CONFIG_URL = f"{BASE}/config"
STATS_URL = f"{BASE}/stats"
ENGINE_URL = f"{BASE}/engine-config"


@pytest.fixture(autouse=True)
async def system_state():
    """逐用例复位模块 13 的进程内状态与指标缓存（理由见模块 docstring）。

    **名单缓存的命中/未命中计数器也要清零**：它们是 `list_cache_hit_rate` 的
    分子分母，而 `LIST_CACHE` 是进程内单例、跨用例累积。不清的话
    "无样本时命中率返回 null"这条断言只在**单独跑本文件**时成立，
    整套跑时会被别的用例的登录/名单查询样本污染（实测 0.8418）。
    """
    from app.services.feature_service import get_feature_service

    def _reset_cache_counters() -> None:
        list_filter.LIST_CACHE.clear()
        list_filter.LIST_CACHE.hits = 0
        list_filter.LIST_CACHE.misses = 0

    config_service.reset_runtime_state()
    metric_service.get_metric_service().clear_cache()
    _reset_cache_counters()
    await db.get_db()[COLL_METRIC_BUCKETS].delete_many({})
    yield
    config_service.reset_runtime_state()
    metric_service.get_metric_service().clear_cache()
    _reset_cache_counters()
    # 本文件有一个用例会往窗口里灌 1200 条事件。**必须清掉**：FeatureWindow 是
    # 进程内单例，残留会让别的模块的用例看到"窗口里有 1000 条登录事件"——
    # 那种失败出现在与 13 无关的用例上，极难定位（与 `_reset_feature_injections`
    # 踩过的是同一类坑）。
    get_feature_service().window.reset()


# ============================================================
# 默认值的单一来源
# ============================================================
async def test_runtime_defaults_mirror_module_owners():
    """参数默认值只声明在 `constants` 一处，且与各消费者的真源相等。

    `window_capacity` / `list_cache_ttl_sec` 的真源分别在 04 与 05 的模块里
    （从 constants 反向 import 会形成循环），因此 constants 里是**镜像字面量**。
    这条用例就是那对镜像的守卫：任何一边被改动，这里立刻变红——比悄悄漂移成
    两个数字好得多（"容量上限显示 10000、实际截断在 5000"是最难查的一类问题）。
    """
    defaults = RUNTIME_CONFIG_DEFAULTS
    assert defaults["short_window_min"] == feature_window.FEATURE_WINDOW_SHORT_MIN
    assert defaults["long_window_min"] == feature_window.FEATURE_WINDOW_LONG_MIN
    assert defaults["window_capacity"] == feature_window.QUEUE_CAPACITY
    assert defaults["list_cache_ttl_sec"] == int(list_filter.DEFAULT_CACHE_TTL_SEC)
    # 默认版本号与 04 的 `window_config.config_version` 是同一个值：
    # "从未保存过运行参数"=="w1"
    assert config_service.format_config_version(1) == feature_window.WINDOW_CONFIG_VERSION
    assert list_config_fields() == [
        "short_window_min", "long_window_min", "window_capacity",
        "list_cache_ttl_sec", "decision_timeout_ms", "metric_bucket_granularity",
    ]


def list_config_fields() -> list[str]:
    from app.schemas.system_schema import CONFIG_FIELDS

    return list(CONFIG_FIELDS)


async def test_sys_codes_are_well_formed():
    """`SYS-*` 的段位与 HTTP 状态一致、文案非空（同 `test_errors.py` 的表驱动口径）。"""
    for code, (status, message) in SYS_CODES.items():
        assert code.startswith("SYS-"), code
        assert message.strip(), f"{code} 缺少面向用户的中文提示"
        assert status is not None
        if code[4] == "4":
            assert 400 <= status <= 499, f"{code} 段位与 HTTP {status} 不一致"
        else:
            assert 500 <= status <= 599, f"{code} 段位与 HTTP {status} 不一致"
    # 结论码（HTTP 200）不进 HTTP 错误表
    assert set(SYS_NOTICE) == {"SYS-5003", "SYS-5004"}
    assert "SYS-5003" not in SYS_CODES and "SYS-5004" not in SYS_CODES


# ============================================================
# V-13-01 读写与版本号
# ============================================================
async def test_v13_01_read_write_and_version_increment(client):
    """V-13-01：GET 默认值 -> PUT 修改 -> 再 GET，值已变且 `config_version` +1。"""
    first = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert first["short_window_min"] == 60
    assert first["config_version"] == "w1" and first["config_version_num"] == 1
    assert first["ranges"]["short_window_min"] == [1, 1440]
    assert first["defaults"]["long_window_min"] == 1440

    r = await client.put(CONFIG_URL, json={"short_window_min": 120}, headers=ADMIN)
    assert r.status_code == 200, r.text
    saved = r.json()["data"]
    assert saved["config_version"] == "w2" and saved["config_version_num"] == 2
    assert saved["applied"] == ["short_window_min"]
    assert saved["requires_restart"] == []
    assert saved["changed"]["short_window_min"] == {"before": 60, "after": 120}

    second = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert second["short_window_min"] == 120
    assert second["config_version"] == "w2"
    assert second["long_window_min"] == 1440, "未提交的字段必须保持原值（部分更新）"

    # 第二次保存继续 +1
    again = (await client.put(CONFIG_URL, json={"long_window_min": 2880}, headers=ADMIN)).json()
    assert again["data"]["config_version"] == "w3"
    third = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert (third["short_window_min"], third["long_window_min"]) == (120, 2880)

    # 无变化的保存不推进版本（与 06 的启停用幂等同一裁定）
    noop = (await client.put(CONFIG_URL, json={"short_window_min": 120}, headers=ADMIN)).json()
    assert noop["data"]["changed"] == {}
    assert noop["data"]["config_version"] == "w3"
    assert "没有变化" in noop["message"]


# ============================================================
# V-13-02 校验与原子性
# ============================================================
async def test_v13_02_window_order_rejected_atomically(client):
    """V-13-02：提交「短窗 2000、长窗 100」-> `SYS-4002` 且**一个值都没保存**。

    注意顺序：**先判短窗 < 长窗，再判单字段取值域**。V-13-02 的输入同时违反
    两条规则（2000 超出 1~1440），而它要求的是 `SYS-4002`；若先判取值域，
    这个用例永远拿不到契约要求的码（与 06 的 `score` 越界必须给 `CFG-4004`
    是同一条取舍：**契约点名的码优先于更"自然"的码**）。
    """
    r = await client.put(
        CONFIG_URL, json={"short_window_min": 2000, "long_window_min": 100}, headers=ADMIN
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "SYS-4002"

    after = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert after["short_window_min"] == 60 and after["long_window_min"] == 1440
    assert after["config_version"] == "w1"
    assert await db.get_db()[COLL_SYSTEM_CONFIG].count_documents({}) == 0, (
        "校验失败时不得留下任何写入（Spec §5 的原子性）"
    )


@pytest.mark.parametrize("payload,field", [
    ({"short_window_min": 0}, "short_window_min"),
    # 短窗上界单独越界需要长窗一起抬高，否则先撞"短窗 < 长窗"（SYS-4002）
    ({"short_window_min": 1441, "long_window_min": 2000}, "short_window_min"),
    ({"long_window_min": 10081}, "long_window_min"),
    ({"window_capacity": 999}, "window_capacity"),
    ({"window_capacity": 1_000_001}, "window_capacity"),
    ({"list_cache_ttl_sec": 0}, "list_cache_ttl_sec"),
    ({"list_cache_ttl_sec": 601}, "list_cache_ttl_sec"),
    ({"decision_timeout_ms": 49}, "decision_timeout_ms"),
    ({"decision_timeout_ms": 5001}, "decision_timeout_ms"),
])
async def test_v13_02b_value_out_of_range_is_rejected(client, payload, field):
    """BR-13-01：越界即 `SYS-4001`（**不静默截断**），且库里仍是空的。"""
    r = await client.put(CONFIG_URL, json=payload, headers=ADMIN)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "SYS-4001"
    assert body["data"]["field"] == field
    assert await db.get_db()[COLL_SYSTEM_CONFIG].count_documents({}) == 0


@pytest.mark.parametrize("payload", [
    {"short_window_min": True},           # bool 不能被当成 1 分钟
    {"short_window_min": "abc"},          # 非数字字符串
    {"metric_bucket_granularity": "5m"},  # 枚举外取值
])
async def test_v13_02c_type_and_enum_errors_are_generic(client, payload):
    """类型/枚举写错 -> `COM-4001`（Spec §5 未给这些分 `SYS-` 码）。"""
    r = await client.put(CONFIG_URL, json=payload, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "COM-4001"
    assert await db.get_db()[COLL_SYSTEM_CONFIG].count_documents({}) == 0


async def test_empty_patch_is_rejected(client):
    """空 body 不是"保存成功"：没有参数的保存是一次无意义写入。"""
    r = await client.put(CONFIG_URL, json={}, headers=ADMIN)
    assert r.status_code == 422
    assert r.json()["code"] == "COM-4001"


# ============================================================
# V-13-03 生效方式
# ============================================================
async def test_v13_03_applied_and_requires_restart(client):
    """V-13-03 / BR-13-02：TTL 进 `applied`，桶粒度进 `requires_restart`。"""
    r = await client.put(
        CONFIG_URL,
        json={"list_cache_ttl_sec": 30, "metric_bucket_granularity": "1h"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "list_cache_ttl_sec" in data["applied"]
    assert "metric_bucket_granularity" in data["requires_restart"]
    assert "metric_bucket_granularity" not in data["applied"], (
        "需重启的参数绝不能出现在 applied 里（BR-13-07）"
    )


async def test_bucket_granularity_1d_is_saved_but_honestly_flagged(client):
    """选 `1d` 时如实告知"重启后仍按 1m 运行"（BR-11-03 拒绝 1d 作写入粒度）。

    这是"不做假开关"的一个具体落点：Spec 的枚举里有 `1d`（不能拒绝），
    而模块 11 明确拒绝用它做写入基础粒度（一天一个桶会让 24h 趋势只剩一个点）。
    因此保存成功（值确实入库、requires_restart 如实列出），同时给出 `SYS-5004`
    提示——用户重启后不会以为"改了却没生效"。
    """
    r = await client.put(CONFIG_URL, json={"metric_bucket_granularity": "1d"}, headers=ADMIN)
    data = r.json()["data"]
    assert data["requires_restart"] == ["metric_bucket_granularity"]
    notices = [n for n in data["notices"] if n["code"] == "SYS-5004"]
    assert notices and "1d" in notices[0]["message"]
    stored = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert stored["metric_bucket_granularity"] == "1d"


# ============================================================
# V-13-04 TTL 变更立即清缓存
# ============================================================
async def test_v13_04_ttl_change_clears_list_cache(client):
    """V-13-04 / BR-13-04：改 TTL 必须**立即清空现有缓存**，新 TTL 即刻生效。"""
    key = ("black", "user", "U_TEST_TTL")
    list_filter.LIST_CACHE.put(key, None)          # 一个"已缓存未命中"
    assert list_filter.LIST_CACHE.stats()["size"] == 1
    assert list_filter.LIST_CACHE.ttl_sec == 10.0

    r = await client.put(CONFIG_URL, json={"list_cache_ttl_sec": 30}, headers=ADMIN)
    assert r.status_code == 200, r.text
    stats = list_filter.LIST_CACHE.stats()
    assert stats["ttl_sec"] == 30.0, "TTL 必须立即对名单缓存生效"
    assert stats["size"] == 0, (
        "不清缓存的话，已缓存的未命中结果会按旧 TTL 继续有效——"
        "而「新增黑名单的最长生效延迟」承诺的正是这个 TTL（AD-02）"
    )


# ============================================================
# V-13-05 窗口参数下一事件生效
# ============================================================
async def test_v13_05_window_params_hot_update(client):
    """V-13-05 / BR-13-05：改短窗后**不重启**，下一个事件的快照配置就是新值。"""
    from app.services.feature_service import get_feature_service

    window = get_feature_service().window
    assert window.short_window_min == 60

    r = await client.put(
        CONFIG_URL, json={"short_window_min": 2, "window_capacity": 5000}, headers=ADMIN
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["data"]["applied"]) == {"short_window_min", "window_capacity"}

    assert window.short_window_min == 2
    assert window.capacity == 5000
    config_out = window.window_config()
    # 快照落库的就是这一份：下一个事件的 window_config 已是新值（V-13-05）
    assert config_out["short_window_min"] == 2
    assert config_out["long_window_min"] == 1440
    assert config_out["config_version"] == "w2", (
        "BR-13-08：参数变更后的版本号要随快照留存（'当时的参数'可追溯）"
    )


async def test_capacity_reduction_trims_immediately_and_reports(client):
    """BR-13-06：容量调小立即裁剪，并在响应里提示"可能丢弃历史窗口数据"。"""
    from app.services.feature_service import get_feature_service

    window = get_feature_service().window
    for index in range(1200):
        await window.ingest({
            "_id": f"EVT_TRIM{index:05d}", "event_type": "login", "user_id": "u_trim",
            "ts": now_ms() - 1000 + index,
        })
    assert window.max_queue_length() == 1200

    r = await client.put(CONFIG_URL, json={"window_capacity": 1000}, headers=ADMIN)
    data = r.json()["data"]
    assert "window_capacity" in data["applied"]
    assert window.max_queue_length() == 1000, "容量上限必须在保存时立即生效"
    notices = [n for n in data["notices"] if "丢弃" in n["message"]]
    assert notices and notices[0]["dropped_entries"] >= 200


# ============================================================
# V-13-06 审计恰好一条
# ============================================================
async def test_v13_06_audit_exactly_one_with_before_after(client):
    """V-13-06 / BR-13-03 / D41：`config.update` **恰好一条**且含 before/after。"""
    r = await client.put(
        CONFIG_URL, json={"decision_timeout_ms": 500, "list_cache_ttl_sec": 20}, headers=ADMIN
    )
    assert r.status_code == 200, r.text
    assert await audit_service.flush()

    rows = await db.get_db()[COLL_AUDIT_LOGS].find({"action": "config.update"}).to_list(10)
    assert len(rows) == 1, "每次成功保存恰好一条审计（D41）"
    row = rows[0]
    assert row["target_type"] == "config" and row["target_id"] == RUNTIME_CONFIG_ID
    assert row["actor"] == "admin01" and row["actor_role"] == "admin"
    assert row["before"]["decision_timeout_ms"] == 200
    assert row["after"]["decision_timeout_ms"] == 500
    assert row["after"]["list_cache_ttl_sec"] == 20
    # 首次保存前"没有版本"（从未保存过 = 按代码默认值运行），如实记成 null；
    # 而不是编一个 w0/w1 冒充"改之前就是那一版"
    assert row["before"]["config_version"] is None
    assert row["after"]["config_version"] == "w2"


# ============================================================
# V-13-07 原子保存
# ============================================================
async def test_v13_07_atomic_save_keeps_all_old_values(client, monkeypatch):
    """V-13-07 / `SYS-5001`：写入失败时**全部参数保持原值**（没有半个配置）。"""
    ok = await client.put(CONFIG_URL, json={"short_window_min": 30}, headers=ADMIN)
    assert ok.status_code == 200, ok.text
    before = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert before["short_window_min"] == 30 and before["config_version"] == "w2"

    async def _boom(self, expected_version, patch):  # noqa: ANN001
        raise ConfigSaveFailedError("模拟写入失败")

    monkeypatch.setattr(ConfigRepo, "update_with_version", _boom)

    r = await client.put(
        CONFIG_URL,
        json={"short_window_min": 45, "list_cache_ttl_sec": 99, "decision_timeout_ms": 999},
        headers=ADMIN,
    )
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "SYS-5001"

    after = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert after["short_window_min"] == 30, "失败的那次写入不得影响任何参数"
    assert after["list_cache_ttl_sec"] == 10 and after["decision_timeout_ms"] == 200
    assert after["config_version"] == "w2"
    from app.services.feature_service import get_feature_service

    assert get_feature_service().window.short_window_min == 30, (
        "写入失败时热更新也不该发生（否则进程按新值跑、库里还是旧值）"
    )


async def test_audit_failure_rolls_back_config(client, monkeypatch):
    """BR-13-03 / D41：审计写不进去 -> 回滚本次保存并抛 `SYS-5001`（宁可不做）。"""
    async def _audit_boom(**kwargs):  # noqa: ANN003
        raise AppError("AUD-5001", "审计服务不可用，操作已中止", 503)

    monkeypatch.setattr(config_service, "audit", _audit_boom)
    r = await client.put(CONFIG_URL, json={"list_cache_ttl_sec": 25}, headers=ADMIN)
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "SYS-5001"

    doc = await db.get_db()[COLL_SYSTEM_CONFIG].find_one({"_id": RUNTIME_CONFIG_ID})
    assert doc is None, "首次保存的写入必须被完整撤销（回到按默认值运行）"
    after = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert after["list_cache_ttl_sec"] == 10 and after["config_version"] == "w1"


# ============================================================
# V-13-08 / V-13-09 吞吐与健康度
# ============================================================
async def _seed_global_bucket(*, event_cnt: int, hist: dict[str, int]) -> int:
    """灌一个"最近已闭合分钟"的 global 1m 桶（与 11 的现有口径一致）。"""
    ts = align(now_ms(), "1m") - 60_000
    inc = {"metrics.event_cnt": event_cnt}
    inc.update({f"metrics.elapsed_hist.{k}": v for k, v in hist.items()})
    await MetricRepo(db.get_db()).upsert_many([{
        "_id": bucket_id("global", "all", "1m", ts),
        "bucket_type": "global", "bucket_key": "all", "granularity": "1m",
        "bucket_ts": ts, "set_on_insert": {"created_at": ts},
        "expire_at": expire_at("1m", ts), "inc": inc,
    }])
    return ts


async def test_v13_08_stats_exposes_four_cards(client):
    """V-13-08 / §3.3：QPS、延迟分位、缓存命中率、Mongo 连通性 + 四个组件。"""
    await _seed_global_bucket(event_cnt=600, hist={"le5": 100, "le10": 200, "le20": 300})
    metric_service.get_metric_service().clear_cache()

    r = await client.get(STATS_URL, headers=ADMIN)
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    assert data["qps"] == 10.0, "600 事件 / 60 秒（11 的 qps_1m 口径）"
    assert data["decision_p50_ms"] is not None
    assert data["decision_p95_ms"] is not None
    assert data["decision_p99_ms"] is not None
    assert data["decision_p50_ms"] <= data["decision_p95_ms"] <= data["decision_p99_ms"]
    # 无查询样本时命中率是 null 而不是 0（0 会被配色规则误判成橙色告警）
    assert data["list_cache_hit_rate"] is None and data["list_cache_samples"] == 0
    assert data["mongo"]["connected"] is True and data["mongo"]["db"] == config.MONGO_DB_NAME
    assert data["uptime_sec"] >= 0
    assert set(data["window"]) >= {
        "total_events", "per_dimension_max", "est_mem_mb", "last_sweep_at", "capacity",
    }
    names = [c["name"] for c in data["components"]]
    assert names == ["MongoDB", "MinIO", "事件流模拟器", "决策引擎"]
    by_name = {c["name"]: c for c in data["components"]}
    assert by_name["MinIO"]["status"] == "unused", "MinIO 当前不启用，必须如实显示"
    assert "G-01" in by_name["决策引擎"]["detail"], "模型引擎未启用必须在说明列里可见"


async def test_cache_hit_rate_is_reported_when_there_are_samples(client):
    """BR-13-27 的另一半：有样本时命中率是真值（hits / (hits+misses)）。"""
    list_filter.LIST_CACHE.hits = 9
    list_filter.LIST_CACHE.misses = 1
    data = (await client.get(STATS_URL, headers=ADMIN)).json()["data"]
    assert data["list_cache_hit_rate"] == 0.9
    assert data["list_cache_samples"] == 10
    assert data["thresholds"]["list_cache_hit_rate_warn"] == 0.9


async def test_v13_09_p95_over_threshold_is_visible(client):
    """V-13-09 / BR-13-27：P95 > 50ms 时后端给出的就是超线值（配色线随响应下发）。

    本模块**不做标红**（那是页面的事，BR-13-27 的"该卡片标红"属呈现层），
    但必须把可判定的数字与阈值一起给出：前端不得自己硬编码 50ms。
    """
    # 100 个样本全部落在 [75,100) 之内 -> P95 ≈ 95ms（远高于 50ms 的目标线）
    await _seed_global_bucket(event_cnt=100, hist={"le75": 100, "le100": 100})
    metric_service.get_metric_service().clear_cache()

    data = (await client.get(STATS_URL, headers=ADMIN)).json()["data"]
    assert data["thresholds"]["decision_p95_warn_ms"] == 50
    assert data["decision_p95_ms"] is not None
    assert data["decision_p95_ms"] > data["thresholds"]["decision_p95_warn_ms"], (
        f"P95={data['decision_p95_ms']} 必须超过告警线 50ms，页面才有可标红的依据"
    )


async def test_stats_degrades_when_metrics_unavailable(client, monkeypatch):
    """指标不可用时不整卡 503：返回 null + `MET-5001` 告知（其余三项照常可看）。"""
    class _Boom:
        async def throughput(self, **_kwargs):  # noqa: ANN003
            raise AppError("MET-5001", "指标数据暂不可用，且无可用快照", 503)

    monkeypatch.setattr(
        metric_service, "get_metric_service", lambda: _Boom()
    )
    r = await client.get(STATS_URL, headers=ADMIN)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["qps"] is None and data["decision_p95_ms"] is None
    assert any(n["code"] == "MET-5001" for n in data["notices"]), (
        "引用 11 的原码（ER-02：引用别人的码保持原前缀）"
    )
    assert data["mongo"]["connected"] is True, "指标挂了不等于 Mongo 挂了，证据要留着"


# ============================================================
# V-13-10 组件探测超时
# ============================================================
async def test_v13_10_probe_timeout_does_not_break_card():
    """V-13-10 / BR-13-28：一个探针挂起 -> 该组件「探测超时」，其余正常。"""
    from app.tasks import health_probe

    async def _hang() -> dict:
        await asyncio.sleep(5)

    async def _fast() -> dict:
        # 探测函数的返回形状就是 §2.2 的 `{name, status, status_label, detail}`
        return {"name": "快组件", "status": "ok", "status_label": "正常", "detail": "正常"}

    components, notices = await health_probe.probe_components(
        timeout=0.05, probes=(("挂起组件", _hang), ("快组件", _fast))
    )
    assert components[0]["status"] == "timeout"
    assert components[0]["status_label"] == "探测超时"
    assert components[1]["status"] == "ok", "一个组件超时不得影响其他组件"
    assert [n["code"] for n in notices] == ["SYS-5003"]
    assert notices[0]["component"] == "挂起组件"


async def test_probe_exception_degrades_to_error_component():
    """探测抛异常同样只影响该组件（fail-soft，绝不向上抛）。"""
    from app.tasks import health_probe

    async def _boom() -> dict:
        raise RuntimeError("探测炸了")

    components, notices = await health_probe.probe_components(
        timeout=0.2, probes=(("坏组件", _boom),)
    )
    assert components[0]["status"] == "error"
    assert "RuntimeError" in components[0]["detail"]
    assert notices == [], "异常不是超时，不该报 SYS-5003"


# ============================================================
# V-13-18 决策引擎配置
# ============================================================
async def test_v13_18_model_engine_is_explicitly_rejected(client):
    """V-13-18 / BR-13-22：`engine_type=model` -> `SYS-4003` 且**配置未变**。"""
    before = (await client.get(ENGINE_URL, headers=ADMIN)).json()["data"]
    assert before["engine_type"] == "rule"
    assert before["model_available"] is False
    assert before["fuse_mode_effective"] is False, "权重 0 时融合方式不生效，必须如实标注"
    assert "G-01" in before["note"]

    for engine_type in ("model", "hybrid"):
        r = await client.put(ENGINE_URL, json={"engine_type": engine_type}, headers=ADMIN)
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["code"] == "SYS-4003"
        assert "尚未实现" in body["message"] or "模型引擎" in body["message"]
        assert body["data"]["model_available"] is False

    after = (await client.get(ENGINE_URL, headers=ADMIN)).json()["data"]
    assert after["engine_type"] == "rule"
    assert await db.get_db()[COLL_MODEL_CONFIGS].count_documents({"engine_type": "model"}) == 0
    assert await audit_service.flush()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "engine.config.update"}) == 0, "被拒绝的请求不留痕（什么都没改）"


async def test_engine_config_rule_accepted_and_audited(client):
    """仅 `rule` 可保存；`fuse_mode` 可改但被如实标注"对结果无影响"。"""
    r = await client.put(
        ENGINE_URL, json={"engine_type": "rule", "fuse_mode": "weighted"}, headers=ADMIN
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["changed"] is True
    assert data["config"]["engine_type"] == "rule"
    assert data["config"]["fuse_mode"] == "weighted"
    assert data["config"]["rule_weight"] == 1.0 and data["config"]["model_weight"] == 0.0
    assert data["config"]["fuse_mode_effective"] is False

    assert await audit_service.flush()
    rows = await db.get_db()[COLL_AUDIT_LOGS].find(
        {"action": "engine.config.update"}).to_list(5)
    assert len(rows) == 1, "恰好一条（D41）"
    # 只记**真正变化**的字段：engine_type 本来就是 rule，不写进审计
    assert rows[0]["before"] == {"fuse_mode": "rule_first"}
    assert rows[0]["after"] == {"fuse_mode": "weighted"}

    # 幂等：同一状态再保存一次不写审计
    again = await client.put(
        ENGINE_URL, json={"engine_type": "rule", "fuse_mode": "weighted"}, headers=ADMIN
    )
    assert again.json()["data"]["changed"] is False
    assert await audit_service.flush()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "engine.config.update"}) == 1


async def test_engine_config_unknown_type_is_generic_param_error(client):
    """`engine_type` 拼错 -> `COM-4001`（与"取值为 model 但未实现"分开）。"""
    r = await client.put(ENGINE_URL, json={"engine_type": "magic"}, headers=ADMIN)
    assert r.status_code == 422
    assert r.json()["code"] == "COM-4001"


# ============================================================
# V-13-20 权限
# ============================================================
@pytest.mark.parametrize("headers,label", [(READER, "reviewer"), (WRITER, "strategist")])
async def test_v13_20_system_endpoints_are_admin_only(client, headers, label):
    """V-13-20：非 admin 调 `/system/*` 一律 403 `AUTH-4020`（BR-01-14）。"""
    checks = [
        ("get", CONFIG_URL, None),
        ("put", CONFIG_URL, {"short_window_min": 90}),
        ("get", STATS_URL, None),
        ("get", ENGINE_URL, None),
        ("put", ENGINE_URL, {"engine_type": "rule"}),
        ("get", f"{BASE}/users", None),
        ("post", f"{BASE}/users", {"username": "nobody01", "real_name": "无权限",
                                   "role": "reviewer"}),
        ("delete", f"{BASE}/users/admin01", None),
    ]
    for method, url, payload in checks:
        if method == "get":
            r = await client.get(url, headers=headers)
        elif method == "post":
            r = await client.post(url, json=payload, headers=headers)
        elif method == "delete":
            r = await client.delete(url, headers=headers)
        else:
            r = await client.put(url, json=payload, headers=headers)
        assert r.status_code == 403, f"{label} 不该能访问 {method.upper()} {url}：{r.text}"
        assert r.json()["code"] == "AUTH-4020"
    # 越权尝试必须留痕（BR-12-24）
    assert await audit_service.flush()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents(
        {"action": "auth.denied", "actor": "reviewer01" if label == "reviewer" else "strategy01"}
    ) >= 1


# ============================================================
# 任务书 §2③：运行参数**不回灌**启动期配置
# ============================================================
async def test_config_never_feeds_back_into_startup_config(client):
    """保存运行参数不得改动 `app/config.py` 的启动期配置，也不得写环境变量。

    这是本模块最容易犯、且只在**重启时**才暴露的错误：把 UI 值写回环境变量
    或让 `config.validate()` 依赖它们，"保存一个参数"就变成"下次启动失败"。
    """
    snapshot = {
        "MONGO_URL": config.MONGO_URL,
        "MONGO_DB_NAME": config.MONGO_DB_NAME,
        "JWT_EXPIRE_MINUTES": config.JWT_EXPIRE_MINUTES,
        "CASE_CLAIM_TIMEOUT_MIN": config.CASE_CLAIM_TIMEOUT_MIN,
    }
    env_before = {k: os.environ.get(k) for k in
                  ("MONGO_URL", "MONGO_DB_NAME", "JWT_SECRET", "DECISION_TIMEOUT_MS")}

    r = await client.put(
        CONFIG_URL,
        json={"decision_timeout_ms": 800, "short_window_min": 5, "window_capacity": 2000},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text

    for key, value in snapshot.items():
        assert getattr(config, key) == value, f"运行参数不得改写启动期配置 {key}"
    for key, value in env_before.items():
        assert os.environ.get(key) == value, f"运行参数不得写环境变量 {key}"
    config.validate()  # 启动期校验必须依然通过（否则就是"下次启动失败"）

    # 真正生效的地方是**运行期取值点**（03 的 TIMEOUT_SEC / 05 的 SLOW_THRESHOLD_MS）
    from app.engine import decision as decision_engine
    from app.services import event_service

    assert event_service.TIMEOUT_SEC == 0.8
    # 03 只用它拼超时告警文案（"决策链路超时（>800ms）"），不一并改就会出现
    # "实际 800ms 超时、提示却说 200ms"的自相矛盾
    assert event_service.DECISION_TIMEOUT_MS == 800
    assert decision_engine.SLOW_THRESHOLD_MS == 800
    # constants 里的**声明默认值**不变（它仍是"未配置时的默认"，不是当前值）
    from app import constants

    assert constants.DECISION_TIMEOUT_MS == 200


async def test_startup_loader_applies_persisted_values(client):
    """`load_and_apply()`（启动期装载）：库里的值必须真的下发到各消费者。

    为什么必须有它：需要重启才生效的参数（指标桶粒度）如果没人装载，就只是
    一个存在库里的摆设——那正是"假开关"。这里先保存，再把进程内状态复位成
    默认值（模拟重启），然后断言装载把它拉回库里的值。
    """
    ok = await client.put(
        CONFIG_URL,
        json={"short_window_min": 15, "window_capacity": 3000,
              "list_cache_ttl_sec": 45, "decision_timeout_ms": 700},
        headers=ADMIN,
    )
    assert ok.status_code == 200, ok.text

    config_service.reset_runtime_state()
    from app.services.feature_service import get_feature_service

    window = get_feature_service().window
    assert window.short_window_min == 60, "复位后应是代码默认值"

    result = await config_service.load_and_apply()
    assert result["loaded"] is True
    assert result["config_version"] == "w2"
    assert window.short_window_min == 15 and window.capacity == 3000
    assert window.window_config()["config_version"] == "w2"
    assert list_filter.LIST_CACHE.ttl_sec == 45.0
    from app.services import event_service

    assert event_service.TIMEOUT_SEC == 0.7


async def test_startup_loader_survives_unavailable_db(monkeypatch):
    """启动期读库失败**不得**让进程起不来（按默认值运行 + 告警）。"""
    async def _boom(self):  # noqa: ANN001
        raise ConfigSaveFailedError("库不可达")

    monkeypatch.setattr(ConfigRepo, "find", _boom)
    result = await config_service.load_and_apply()
    assert result["loaded"] is False
    from app.services.feature_service import get_feature_service

    assert get_feature_service().window.short_window_min == 60


async def test_non_admin_cannot_read_config_after_denial_audit(client):
    """越权被拒之后，配置本身**没有任何变化**（拒绝是纯粹的拒绝）。"""
    await client.put(CONFIG_URL, json={"short_window_min": 40}, headers=ADMIN)
    r = await client.put(CONFIG_URL, json={"short_window_min": 41}, headers=READER)
    assert r.status_code == 403
    data = (await client.get(CONFIG_URL, headers=ADMIN)).json()["data"]
    assert data["short_window_min"] == 40
    assert data["config_version"] == "w2"
