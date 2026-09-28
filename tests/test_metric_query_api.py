# -*- coding: utf-8 -*-
"""模块 11 查询接口验收（§3.2 ~ §3.6、§3.8）：口径、交叉筛选、错误码、权限与降级。

**时钟为什么被冻结**：趋势/分布/吞吐的窗口边界都依赖"现在"，用例若用真实时钟，
断言会在分钟翻转的那一刻随机失败（几百次跑一次），而这类偶发红点会被当成"环境问题"
忽略掉。这里用 `monkeypatch` 冻结 `metric_query._now_ms`，把边界变成确定的。
"""
from __future__ import annotations

import asyncio

import pytest
from pymongo.errors import PyMongoError

from app import db
from app.constants import COLL_METRIC_BUCKETS, COLL_RISK_CASES, COLL_RULES
from app.core.metric_rollup import rollup
from app.engine.metric_bucket import align, bucket_id, expire_at, range_bounds
from app.errors import AppError
from app.repos.metric_repo import MetricRepo
from app.services import metric_query, metric_service
from app.services.metric_query import MetricQuery
from app.services.metric_service import CACHE_TTL_MS, MetricService
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

BASE = "/api/v1/metrics"
ENVELOPE_KEYS = {"ok", "code", "message", "trace_id", "data"}


@pytest.fixture
def frozen(monkeypatch) -> int:
    """冻结查询时钟到"现在"（相对真实时间，保证桶落在当前窗口内）。"""
    moment = now_ms()
    monkeypatch.setattr(metric_query, "_now_ms", lambda: moment)
    return moment


def _payload(ts: int, *, bucket_type: str = "global", key: str = "all",
             gran: str = "1m", inc: dict | None = None) -> dict:
    return {
        "_id": bucket_id(bucket_type, key, gran, ts),
        "bucket_type": bucket_type,
        "bucket_key": key,
        "granularity": gran,
        "bucket_ts": ts,
        "set_on_insert": {"created_at": ts},
        "expire_at": expire_at(gran, ts),
        "inc": inc or {},
    }


async def _seed(inc_map: dict[tuple[str, str], dict], ts: int, gran: str = "1m") -> None:
    payloads = [
        _payload(ts, bucket_type=bt, key=key, gran=gran, inc=inc)
        for (bt, key), inc in inc_map.items()
    ]
    await MetricRepo(db.get_db()).upsert_many(payloads)


async def _seed_case(status: str, seq: int) -> None:
    """插入一条案件（`_id` 用序号而不是时间戳：同一毫秒内插 3 条会撞主键）。"""
    await db.get_db()[COLL_RISK_CASES].insert_one({"_id": f"CASE-{seq:03d}", "status": status})


async def _clear_cases_and_rules() -> None:
    # 这两个集合不属本模块，也不在 conftest 的清理列表里：用例各自负责自己的痕迹
    await db.get_db()[COLL_RISK_CASES].delete_many({})
    await db.get_db()[COLL_RULES].delete_many({})


# ============================================================ §3.2 卡片
async def test_overview_returns_cards_with_server_side_ratios(client, frozen):
    """V-11-04 / BR-11-07：拦截率由服务端算好（0.047），页面只乘 100 加百分号。"""
    # risk_cases 不在 conftest 的清理列表里（它属模块 07），本模块只读它，
    # 因此凡断言待审计数的用例都必须自己先把痕迹清干净
    await _clear_cases_and_rules()
    ts = align(frozen, "1m")
    await _seed({("global", "all"): {
        "metrics.event_cnt": 1000, "metrics.pass_cnt": 900, "metrics.review_cnt": 53,
        "metrics.reject_cnt": 47, "metrics.score_sum": 47390, "metrics.score_cnt": 1000,
        "metrics.estimated_saved_amount": 3580000,
        "metrics.saved_amount_by_type.coupon_receive": 600000,
        "metrics.saved_amount_by_type.order_create": 1200000,
        "metrics.saved_amount_by_type.order_pay": 1300000,
        "metrics.saved_amount_by_type.after_sale_apply": 480000,
    }}, ts)

    r = await client.get(f"{BASE}/overview", params={"range": "1h"}, headers=READER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == ENVELOPE_KEYS and body["ok"] is True
    data = body["data"]
    assert data["range"] == "1h" and data["granularity"] == "1m"
    cards = data["cards"]
    assert cards["event_cnt"] == 1000
    assert cards["block_rate"] == 0.047
    assert cards["avg_score"] == 47.39
    assert cards["estimated_saved_amount"] == 3580000
    assert cards["saved_amount_by_type"]["order_pay"] == 1300000
    assert cards["pending_case_cnt"] == 0
    assert data["stale"] is False and data["stale_at"] is None
    assert isinstance(data["generated_at"], int)


async def test_overview_empty_range_returns_null_not_zero(client, frozen):
    """V-11-05 / BR-11-08：空范围的派生量必须是 `null`（页面显示「—」）。"""
    r = await client.get(f"{BASE}/overview", params={"range": "1h"}, headers=READER)
    cards = r.json()["data"]["cards"]
    assert cards["event_cnt"] == 0
    assert cards["block_rate"] is None
    assert cards["avg_score"] is None
    # 真实计数为 0 是允许的（它确实是 0），只有派生比值必须为 null
    assert cards["reject_cnt"] == 0


async def test_pending_case_cnt_is_a_gauge_unaffected_by_filters(client, frozen):
    """BR-11-17 / N-11-9：待审数不套 range/scene/level。"""
    await _clear_cases_and_rules()
    for i in range(3):
        await _seed_case("pending", i)
    await _seed_case("closed", 99)

    for params in ({"range": "1h"}, {"range": "30d"},
                   {"range": "1h", "scene": "order"}, {"range": "24h", "level": "high"}):
        r = await client.get(f"{BASE}/overview", params=params, headers=READER)
        assert r.status_code == 200
        assert r.json()["data"]["cards"]["pending_case_cnt"] == 3, params


# ============================================================ §3.3 趋势
async def test_trend_zero_fills_and_marks_only_last_point_partial(client, frozen):
    ts = align(frozen, "1m")
    await _seed({("global", "all"): {
        "metrics.event_cnt": 25, "metrics.pass_cnt": 20, "metrics.review_cnt": 3,
        "metrics.reject_cnt": 2,
    }}, ts)

    r = await client.get(f"{BASE}/trend", params={"range": "1h"}, headers=READER)
    data = r.json()["data"]
    assert data["range"] == "1h" and data["granularity"] == "1m"
    assert data["truncated"] is False
    assert len(data["points"]) == 60
    assert data["points"][0]["event_cnt"] == 0
    assert data["points"][0]["block_rate"] is None
    current = [p for p in data["points"] if p["bucket_ts"] == ts][0]
    assert current["event_cnt"] == 25 and current["block_rate"] == 0.08
    assert current["partial"] is True
    assert [p["partial"] for p in data["points"]].count(True) == 1


async def test_trend_24h_reads_rolled_up_hour_buckets(client, frozen):
    """`range=24h` 的 1h 桶由 rollup 生成（BR-11-03/15），查询侧只读桶。"""
    await _seed({("global", "all"): {"metrics.event_cnt": 12, "metrics.reject_cnt": 3}},
                align(frozen, "1m"))
    from_ts, to_ts, granularity = range_bounds("24h", frozen)
    assert granularity == "1h"
    await rollup(granularity="1h", from_ts=from_ts, to_ts=to_ts)

    data = (await client.get(f"{BASE}/trend", params={"range": "24h"},
                             headers=READER)).json()["data"]
    assert data["granularity"] == "1h"
    assert len(data["points"]) == 24
    last = data["points"][-1]
    assert last["bucket_ts"] == align(frozen, "1h")
    assert last["event_cnt"] == 12 and last["block_rate"] == 0.25


# ============================================================ §3.4 分布
async def test_distribution_level_has_names_ratios_and_fixed_order(client, frozen):
    ts = align(frozen, "1m")
    await _seed({
        ("level", "low"): {"metrics.event_cnt": 60},
        ("level", "medium"): {"metrics.event_cnt": 30},
        ("level", "high"): {"metrics.event_cnt": 10},
    }, ts)

    data = (await client.get(f"{BASE}/distribution", params={"range": "1h"},
                             headers=READER)).json()["data"]
    assert data["dim"] == "level" and data["total"] == 100
    assert [(i["key"], i["name"], i["cnt"]) for i in data["items"]] == [
        ("low", "低风险", 60), ("medium", "中风险", 30), ("high", "高风险", 10),
    ]
    assert [i["ratio"] for i in data["items"]] == [0.6, 0.3, 0.1]


async def test_distribution_scene_dim_uses_event_count(client, frozen):
    ts = align(frozen, "1m")
    await _seed({
        ("scene", "login"): {"metrics.event_cnt": 5},
        ("scene", "order"): {"metrics.event_cnt": 3},
    }, ts)
    data = (await client.get(f"{BASE}/distribution",
                             params={"range": "1h", "dim": "scene"},
                             headers=READER)).json()["data"]
    assert data["dim"] == "scene"
    assert [(i["key"], i["cnt"]) for i in data["items"]] == [("login", 5), ("order", 3)]
    assert data["items"][0]["name"] == "登录"


async def test_cross_filter_scene_and_level(client, frozen):
    """V-11-07 / BR-11-12：`scene=order&level=high` 取场景桶的 `reject_cnt`。"""
    ts = align(frozen, "1m")
    await _seed({("scene", "order"): {
        "metrics.event_cnt": 10, "metrics.pass_cnt": 5, "metrics.review_cnt": 3,
        "metrics.reject_cnt": 2, "metrics.estimated_saved_amount": 66000,
        "metrics.saved_amount_by_type.order_create": 66000,
    }}, ts)

    # ① 等级分布：场景桶的三档计数直接充当等级分布
    dist = (await client.get(f"{BASE}/distribution",
                             params={"range": "1h", "scene": "order"},
                             headers=READER)).json()["data"]
    assert {i["key"]: i["cnt"] for i in dist["items"]} == {"low": 5, "medium": 3, "high": 2}
    assert dist["total"] == 10

    # ② 场景分布 + level 筛选：每个场景只取该等级对应的分档计数
    scene_dist = (await client.get(f"{BASE}/distribution",
                                   params={"range": "1h", "level": "high", "dim": "scene"},
                                   headers=READER)).json()["data"]
    assert {i["key"]: i["cnt"] for i in scene_dist["items"]} == {"order": 2}

    # ③ 卡片：交叉筛选后整片就是该档事件，拦截率为 100%，资损取场景桶的拦截金额
    cards = (await client.get(f"{BASE}/overview",
                              params={"range": "1h", "scene": "order", "level": "high"},
                              headers=READER)).json()["data"]["cards"]
    assert cards["event_cnt"] == 2 and cards["reject_cnt"] == 2
    assert cards["pass_cnt"] == 0 and cards["review_cnt"] == 0
    assert cards["block_rate"] == 1.0
    assert cards["estimated_saved_amount"] == 66000

    # ④ level=medium 时资损恒为 0（BR-11-14），且计数取 review 档
    medium = (await client.get(f"{BASE}/overview",
                               params={"range": "1h", "scene": "order", "level": "medium"},
                               headers=READER)).json()["data"]["cards"]
    assert medium["event_cnt"] == 3 and medium["estimated_saved_amount"] == 0


# ============================================================ §3.5 排行
async def test_rule_ranking_ranks_ties_joins_names_and_switches_metric_field(client, frozen):
    ts = align(frozen, "1m")
    await _seed({
        ("global", "all"): {"metrics.event_cnt": 100},
        ("rule", "R001"): {"metrics.hit_cnt": 9, "metrics.pass_hit_cnt": 4,
                           "metrics.review_hit_cnt": 3, "metrics.reject_hit_cnt": 2,
                           "metrics.hit_score_sum": 90},
        ("rule", "R002"): {"metrics.hit_cnt": 5, "metrics.pass_hit_cnt": 1,
                           "metrics.review_hit_cnt": 4, "metrics.reject_hit_cnt": 0,
                           "metrics.hit_score_sum": 50},
        ("rule", "R003"): {"metrics.hit_cnt": 5, "metrics.pass_hit_cnt": 5,
                           "metrics.review_hit_cnt": 0, "metrics.reject_hit_cnt": 0,
                           "metrics.hit_score_sum": 55},
    }, ts)
    await _clear_cases_and_rules()
    await db.get_db()[COLL_RULES].insert_one(
        {"_id": "R001", "rule_code": "R001", "name": "同设备多账号", "status": "enabled"}
    )

    data = (await client.get(f"{BASE}/rule-ranking", params={"range": "1h"},
                             headers=READER)).json()["data"]
    assert data["metric_field"] == "hit_cnt"
    assert [(i["rule_code"], i["rank"], i["hit_cnt"]) for i in data["items"]] == [
        ("R001", 1, 9), ("R002", 2, 5), ("R003", 2, 5),
    ]
    first = data["items"][0]
    assert first["rule_name"] == "同设备多账号" and first["rule_status"] == "enabled"
    assert first["hit_ratio"] == 0.09
    # 规则不存在 -> 回落编码 + deleted（§2.4）
    missing = data["items"][2]
    assert missing["rule_name"] == "R003" and missing["rule_status"] == "deleted"

    # V-11-06：level=medium 切到 review 口径并回显
    medium = (await client.get(f"{BASE}/rule-ranking",
                               params={"range": "1h", "level": "medium"},
                               headers=READER)).json()["data"]
    assert medium["metric_field"] == "review_hit_cnt"
    assert [(i["rule_code"], i["hit_cnt"]) for i in medium["items"]] == [
        ("R002", 4), ("R001", 3), ("R003", 0),
    ]

    # top 生效
    limited = (await client.get(f"{BASE}/rule-ranking",
                                params={"range": "1h", "top": 2},
                                headers=READER)).json()["data"]
    assert len(limited["items"]) == 2


# ============================================================ §3.6 吞吐
async def test_throughput_reports_qps_and_p95(client, frozen):
    """§3.6：`qps_1m` 取**最近一个已闭合** 1m 桶（当前这一分钟还在累积，不计入）。"""
    window_end = align(frozen, "1m")
    closed_minute = window_end - 60_000
    await _seed({("global", "all"): {
        "metrics.event_cnt": 600,
        "metrics.elapsed_hist.le5": 100, "metrics.elapsed_hist.le10": 200,
        "metrics.elapsed_hist.le20": 300,
    }}, closed_minute)

    r = await client.get(f"{BASE}/throughput", params={"window": "5m"}, headers=READER)
    data = r.json()["data"]
    assert data["window"] == "5m"
    assert data["qps_1m"] == 10.0                     # 600 / 60
    assert data["event_cnt"] == 600
    assert data["window_end"] == window_end
    assert data["window_start"] == window_end - 300_000
    assert data["p95_elapsed_ms"] is not None
    assert data["elapsed_hist"]["le5"] == 100


async def test_throughput_without_hist_returns_null_p95(client, frozen):
    """MET-5003 / BR-11-13：直方图缺失 -> `p95_elapsed_ms = null`。"""
    data = (await client.get(f"{BASE}/throughput", params={"window": "1m"},
                             headers=READER)).json()["data"]
    assert data["p95_elapsed_ms"] is None
    assert data["qps_1m"] == 0.0
    assert set(data["elapsed_hist"]) == {
        "le5", "le10", "le20", "le30", "le50", "le75", "le100", "le200", "gt200",
    }


# ============================================================ §5 参数错误码
@pytest.mark.parametrize("params,code", [
    ({"range": "5m"}, "MET-4001"),
    ({"range": "24h", "granularity": "1m"}, "MET-4002"),
    ({"range": "1h", "granularity": "1d"}, "MET-4002"),
    ({"range": "1h", "granularity": "1h"}, "MET-4002"),
    ({"range": "1h", "scene": "unknown_scene"}, "MET-4004"),
    ({"range": "1h", "level": "critical"}, "MET-4004"),
])
async def test_overview_parameter_errors(client, frozen, params, code):
    r = await client.get(f"{BASE}/overview", params=params, headers=READER)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["ok"] is False and body["code"] == code
    assert body["message"]


@pytest.mark.parametrize("top", ["0", "51", "-3"])
async def test_rule_ranking_top_out_of_range(client, frozen, top):
    """MET-4003：越界拒绝（Spec §5 的提示写"已按 10 返回"，但状态列写 400，以状态列为准）。"""
    r = await client.get(f"{BASE}/rule-ranking", params={"range": "1h", "top": top},
                         headers=READER)
    assert r.status_code == 400 and r.json()["code"] == "MET-4003"


async def test_distribution_invalid_dim(client, frozen):
    r = await client.get(f"{BASE}/distribution", params={"range": "1h", "dim": "risk"},
                         headers=READER)
    assert r.status_code == 400 and r.json()["code"] == "MET-4004"


async def test_throughput_invalid_window(client, frozen):
    r = await client.get(f"{BASE}/throughput", params={"window": "2m"}, headers=READER)
    assert r.status_code == 400 and r.json()["code"] == "MET-4001"


async def test_rollup_endpoint_errors_and_success(client, frozen):
    """§3.8：运维接口的成功响应与 `MET-4005`。"""
    ts = align(frozen, "1m")
    await _seed({("global", "all"): {"metrics.event_cnt": 3}}, ts)
    hour = align(frozen, "1h")

    bad = await client.post(f"{BASE}/rollup", headers=ADMIN, json={
        "granularity": "1h", "from_ts": hour + 3_600_000, "to_ts": hour})
    assert bad.status_code == 400 and bad.json()["code"] == "MET-4005"

    ok = await client.post(f"{BASE}/rollup", headers=ADMIN, json={
        "granularity": "1h", "from_ts": hour, "to_ts": hour + 3_599_999})
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert data["scanned_1m_buckets"] == 1 and data["upserted"] == 1
    assert isinstance(data["elapsed_ms"], int)


# ============================================================ 权限
@pytest.mark.parametrize("path,params", [
    ("/overview", {"range": "1h"}),
    ("/trend", {"range": "1h"}),
    ("/distribution", {"range": "1h"}),
    ("/rule-ranking", {"range": "1h"}),
    ("/throughput", {"window": "5m"}),
])
async def test_read_endpoints_open_to_all_three_roles(client, frozen, path, params):
    """G-11 / E19：大盘四个查询接口对三角色全部可读。"""
    for headers in (READER, WRITER, ADMIN):
        r = await client.get(f"{BASE}{path}", params=params, headers=headers)
        assert r.status_code == 200, f"{path} {headers}: {r.status_code} {r.text}"


async def test_read_endpoints_require_login(client, frozen):
    """未登录 -> 401（鉴权中间件在路由之前）。"""
    r = await client.get(f"{BASE}/overview", params={"range": "1h"})
    assert r.status_code == 401 and r.json()["code"] == "AUTH-4002"


@pytest.mark.parametrize("headers", [READER, WRITER])
async def test_rollup_requires_admin(client, frozen, headers):
    """`sys:config` 只属于 admin；其余角色必须是 AUTH-4020。"""
    r = await client.post(f"{BASE}/rollup", headers=headers, json={
        "granularity": "1h", "from_ts": 0, "to_ts": 3_599_999})
    assert r.status_code == 403
    assert r.json()["code"] == "AUTH-4020"


# ============================================================ AD-03 / BR-11-18
async def test_queries_never_touch_detail_collections(frozen):
    """V-11-02 / BR-11-18：查询只读 `metric_buckets`（+ 排行联查 rules、卡片联查 risk_cases）。"""
    touched: set[str] = set()

    class SpyDb:
        def __init__(self, real):
            self._real = real

        def __getitem__(self, name):
            touched.add(name)
            return self._real[name]

    repo = MetricRepo(SpyDb(db.get_db()))
    query = MetricQuery(repo, now_provider=lambda: frozen)
    await query.overview(range_="1h")
    await query.trend(range_="1h")
    await query.distribution(range_="1h")
    await query.distribution(range_="1h", dim="scene")
    await query.rule_ranking(range_="24h")
    await query.throughput(window="5m")

    assert "metric_buckets" in touched, "正向对照：确实读了指标桶"
    assert touched <= {COLL_METRIC_BUCKETS, COLL_RULES, COLL_RISK_CASES}, touched
    assert "decisions" not in touched and "decision_hits" not in touched


# ============================================================ BR-11-19 缓存与合并
class _CountingRepo:
    """只数调用次数、并故意慢一点的假仓储（用于验证缓存与 single-flight）。"""

    def __init__(self, delay: float = 0.05):
        self.calls = 0
        self.delay = delay

    async def sum_range(self, *, fields, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {f: 0 for f in fields}

    async def count_pending_cases(self):
        return 0


async def test_cache_serves_repeat_query_within_ttl():
    """BR-11-19：3s 内重复请求只查一次库。"""
    repo = _CountingRepo(delay=0)
    service = MetricService(repo_factory=lambda: repo)
    await service.overview(range_="1h")
    await service.overview(range_="1h")
    await service.overview(range_="1h")
    assert repo.calls == 1
    assert service.stats["cache_hit"] == 2


async def test_single_flight_merges_concurrent_identical_queries():
    """BR-11-19：相同查询键的并发请求只查一次库（其余等待同一个结果）。"""
    repo = _CountingRepo()
    service = MetricService(repo_factory=lambda: repo, cache_ttl_ms=0)
    results = await asyncio.gather(*[service.overview(range_="1h") for _ in range(5)])
    assert repo.calls == 1
    assert service.stats["coalesced"] == 4
    assert all(r["cards"]["event_cnt"] == 0 for r in results)
    # 不同查询键不会被错误合并
    await service.overview(range_="24h")
    assert repo.calls == 2


# ============================================================ BR-11-20 / MET-5001
async def test_mongo_unavailable_without_snapshot_returns_503(client, monkeypatch, frozen):
    """MET-5001：从未成功过 -> 503（而不是返回 0 值图表）。"""
    service = MetricService()          # 新实例：确保没有任何历史快照

    async def boom(self, **kwargs):
        raise PyMongoError("simulated mongo down")

    monkeypatch.setattr(MetricRepo, "sum_range", boom)

    with pytest.raises(AppError) as err:
        await service.overview(range_="1h")
    assert err.value.code == "MET-5001" and err.value.http_status == 503

    r = await client.get(f"{BASE}/overview", params={"range": "1h"}, headers=READER)
    assert r.status_code == 503
    assert r.json()["code"] == "MET-5001"


async def test_mongo_unavailable_returns_last_successful_snapshot(client, monkeypatch, frozen):
    """V-11-16 / BR-11-20：降级返回最近一次成功快照 + `stale=true`，**不用 0 冒充**。"""
    ts = align(frozen, "1m")
    await _seed({("global", "all"): {
        "metrics.event_cnt": 40, "metrics.reject_cnt": 4,
    }}, ts)

    service = metric_service.get_metric_service()
    service.clear_cache()
    first = await client.get(f"{BASE}/overview", params={"range": "1h"}, headers=READER)
    assert first.json()["data"]["cards"]["event_cnt"] == 40
    assert first.json()["data"]["stale"] is False

    async def boom(self, **kwargs):
        raise PyMongoError("simulated mongo down")

    monkeypatch.setattr(MetricRepo, "sum_range", boom)
    service.clear_cache()
    service.cache_ttl_ms = 0
    try:
        degraded = await client.get(f"{BASE}/overview", params={"range": "1h"}, headers=READER)
    finally:
        service.cache_ttl_ms = CACHE_TTL_MS

    assert degraded.status_code == 200
    data = degraded.json()["data"]
    assert data["stale"] is True
    assert isinstance(data["stale_at"], int)
    # 关键：返回的是快照真值，不是 0（0 会被读成"没有任何事件"）
    assert data["cards"]["event_cnt"] == 40
    assert data["cards"]["block_rate"] == 0.1
    assert service.stats["stale"] >= 1
