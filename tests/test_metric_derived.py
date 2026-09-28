# -*- coding: utf-8 -*-
"""BR-11-07 / 08 / 09 / 12 / 13：派生量口径、`null` 语义与四舍五入。

**为什么用假仓储而不是真库**：这一层要验证的是"给定累计量，算出来的百分比对不对"。
真库只会引入噪声（聚合是否正确是另一组用例的事），而固定时钟 + 可编程仓储能让
每个边界（分母为 0、正好 4 位、并列名次）都被精确断言。
"""
from __future__ import annotations

import pytest

from app.engine.metric_agg import HIST_KEYS, hist_bucket
from app.engine.metric_bucket import align, trend_points
from app.engine.metric_percentile import percentile
from app.services.metric_query import WINDOW_MS, MetricQuery, ratio, round_half_up

pytestmark = pytest.mark.anyio

NOW = 1758600271208          # 固定时刻：趋势/吞吐的边界断言不能随执行时间漂移
MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000


class FakeRepo:
    """可编程假仓储：只回答"这个维度这些字段应该返回什么"。

    `calls` 记录调用过的 `(方法, 维度, key)`，因此还能顺带断言"口径真的读了
    `scene` 桶而不是 `level` 桶"这类实现细节。
    """

    def __init__(self, *, sums=None, series=None, by_key=None, bucket=None,
                 hist=None, pending=0, rules=None):
        self.sums = sums or {}
        self.series_map = series or {}
        self.by_key_map = by_key or {}
        self.buckets = bucket or {}
        self.hist = hist or {}
        self.pending = pending
        self.rules = rules or {}
        self.calls: list[tuple] = []

    async def sum_range(self, *, bucket_type, bucket_key=None, granularity,
                        from_ts, to_ts, fields):
        self.calls.append(("sum_range", bucket_type, bucket_key))
        src = self.sums.get((bucket_type, bucket_key), {})
        return {f: int(src.get(f) or 0) for f in fields}

    async def series(self, *, bucket_type, bucket_key=None, granularity,
                     from_ts, to_ts, fields):
        self.calls.append(("series", bucket_type, bucket_key))
        src = self.series_map.get((bucket_type, bucket_key), {})
        return {ts: {f: int(row.get(f) or 0) for f in fields} for ts, row in src.items()}

    async def sum_by_key(self, *, bucket_type, granularity, from_ts, to_ts, fields,
                         sort_field=None, descending=True, limit=None):
        self.calls.append(("sum_by_key", bucket_type, sort_field))
        rows = [dict(r) for r in self.by_key_map.get(bucket_type, [])]
        if sort_field:
            rows.sort(key=lambda r: (
                -int(r.get(sort_field) or 0) if descending else int(r.get(sort_field) or 0),
                str(r["bucket_key"]),
            ))
        if limit is not None:
            rows = rows[:limit]
        return [
            {"bucket_key": str(r["bucket_key"]),
             **{f: int(r.get(f) or 0) for f in fields}}
            for r in rows
        ]

    async def get_bucket(self, bucket_type, bucket_key, granularity, bucket_ts):
        self.calls.append(("get_bucket", bucket_type, bucket_key))
        return self.buckets.get(bucket_ts)

    async def elapsed_hist(self, *, granularity, from_ts, to_ts,
                           bucket_type="global", bucket_key="all"):
        self.calls.append(("elapsed_hist", bucket_type, bucket_key))
        return {k: int(self.hist.get(k) or 0) for k in HIST_KEYS}

    async def count_pending_cases(self):
        self.calls.append(("count_pending_cases",))
        return self.pending

    async def rules_by_codes(self, codes):
        self.calls.append(("rules_by_codes",))
        return {c: self.rules[c] for c in codes if c in self.rules}


def query(repo: FakeRepo, *, write_granularity: str = "1m") -> MetricQuery:
    return MetricQuery(repo, write_granularity=write_granularity, now_provider=lambda: NOW)


def true_p95(samples: list[int]) -> float:
    """样本真 P95（线性插值，与 numpy 默认 method="linear" 一致）。

    刻意不用 numpy：它是环境里恰好存在的包而非本项目依赖（requirements 里没有），
    在测试里 import 一个未声明依赖，会让"换台机器跑测试"变成一场赌博。
    """
    xs = sorted(samples)
    index = 0.95 * (len(xs) - 1)
    low = int(index)
    high = min(low + 1, len(xs) - 1)
    return xs[low] + (xs[high] - xs[low]) * (index - low)


# ============================================================ BR-11-07/08/09
async def test_overview_derives_block_rate_and_avg_score_server_side():
    """V-11-04 / BR-11-07：1000 事件 47 拦截 -> `block_rate=0.047`（页面只展示）。"""
    repo = FakeRepo(sums={("global", "all"): {
        "event_cnt": 1000, "pass_cnt": 900, "review_cnt": 53, "reject_cnt": 47,
        "score_sum": 47390, "score_cnt": 1000,
    }})
    data = await query(repo).overview(range_="24h")
    cards = data["cards"]
    assert cards["block_rate"] == 0.047
    assert cards["event_cnt"] == 1000
    assert cards["reject_cnt"] == 47
    assert cards["avg_score"] == 47.39
    # 分母必须是 event_cnt（不是"拦截+转人工"），否则拦截率的口径就变成了
    # "拦截占已处置的比例"，与页面上的文案不是一回事
    assert cards["block_rate"] == ratio(cards["reject_cnt"], cards["event_cnt"])
    assert cards["block_rate"] != ratio(cards["reject_cnt"],
                                        cards["reject_cnt"] + cards["review_cnt"])


async def test_overview_zero_denominator_returns_null_not_zero():
    """V-11-05 / BR-11-08：空时间范围 -> `null`，**不是 0**。"""
    repo = FakeRepo()
    cards = (await query(repo).overview(range_="1h"))["cards"]
    assert cards["event_cnt"] == 0
    assert cards["block_rate"] is None and cards["block_rate"] != 0
    assert cards["avg_score"] is None and cards["avg_score"] != 0
    assert cards["estimated_saved_amount"] == 0
    assert cards["saved_amount_by_type"] == {
        "login": 0, "coupon_receive": 0, "order_create": 0, "order_pay": 0,
        "after_sale_apply": 0,
    }


async def test_ratio_uses_half_up_not_bankers_rounding():
    """BR-11-09：派生浮点统一四舍五入到 4 位（不是 Python `round` 的银行家舍入）。"""
    assert ratio(1, 3) == 0.3333
    assert ratio(1, 7) == 0.1429
    assert ratio(0, 0) is None
    assert ratio(5, 0) is None
    # Decimal HALF_UP：0.12345 -> 0.1235；Python 的 round(0.12345, 4) 会给出 0.1234
    assert round_half_up(0.12345) == 0.1235
    assert round_half_up(2.00005) == 2.0001

    repo = FakeRepo(sums={("global", "all"): {
        "event_cnt": 100_000, "score_sum": 12345, "score_cnt": 100_000,
    }})
    cards = (await query(repo).overview(range_="24h"))["cards"]
    assert cards["avg_score"] == 0.1235


async def test_pending_case_cnt_comes_from_gauge_query():
    """BR-11-17：待审数是当前态 gauge，服务层直查 `count(status=pending)`。"""
    repo = FakeRepo(pending=17)
    data = await query(repo).overview(range_="1h")
    assert data["cards"]["pending_case_cnt"] == 17
    assert ("count_pending_cases",) in repo.calls


# ============================================================ BR-11-12 交叉筛选
async def test_cross_filter_reads_scene_bucket_decision_counters():
    """V-11-07 / BR-11-12：`scene=order&level=high` 取 `scene:order` 桶的 `reject_cnt`。"""
    repo = FakeRepo(sums={("scene", "order"): {
        "event_cnt": 10, "pass_cnt": 5, "review_cnt": 3, "reject_cnt": 2,
        "score_sum": 500, "score_cnt": 10, "estimated_saved_amount": 66000,
        "saved_amount_by_type.order_create": 66000,
    }})
    data = await query(repo).overview(range_="24h", scene="order", level="high")
    assert ("sum_range", "scene", "order") in repo.calls     # 读的是场景桶
    assert ("sum_range", "level", "high") not in repo.calls  # 而不是等级桶
    cards = data["cards"]
    # 整片筛选结果按定义只含该档事件：event_cnt 就是这一档的计数
    assert cards["event_cnt"] == 2 and cards["reject_cnt"] == 2
    assert cards["pass_cnt"] == 0 and cards["review_cnt"] == 0
    assert cards["block_rate"] == 1.0
    assert cards["estimated_saved_amount"] == 66000

    # level=medium 时资损恒为 0（BR-11-14）：资损只由 reject 产生
    medium = await query(repo).overview(range_="24h", scene="order", level="medium")
    assert medium["cards"]["event_cnt"] == 3 and medium["cards"]["reject_cnt"] == 0
    assert medium["cards"]["estimated_saved_amount"] == 0
    assert medium["cards"]["saved_amount_by_type"]["order_create"] == 0


async def test_level_only_filter_reads_level_bucket():
    repo = FakeRepo(sums={("level", "medium"): {
        "event_cnt": 12, "review_cnt": 12, "score_sum": 600, "score_cnt": 12,
    }})
    data = await query(repo).overview(range_="24h", level="medium")
    assert ("sum_range", "level", "medium") in repo.calls
    assert data["cards"]["event_cnt"] == 12
    assert data["cards"]["review_cnt"] == 12
    assert data["cards"]["avg_score"] == 50.0


# ============================================================ §3.3 趋势补零
async def test_trend_zero_fills_missing_buckets_and_marks_last_partial():
    """§2.2 / V-11-05：空桶补 0（折线不断裂），只有末点 `partial=true`。"""
    points = trend_points("1h", NOW)
    target_ts = points[-2][0]
    repo = FakeRepo(series={("global", "all"): {
        target_ts: {"event_cnt": 10, "pass_cnt": 8, "review_cnt": 1, "reject_cnt": 1},
    }})
    data = await query(repo).trend(range_="1h")
    assert data["granularity"] == "1m"
    assert data["truncated"] is False
    assert len(data["points"]) == 60
    assert data["points"][0]["event_cnt"] == 0
    assert data["points"][0]["block_rate"] is None      # 0 事件 -> null 而不是 0.0
    filled = [p for p in data["points"] if p["bucket_ts"] == target_ts][0]
    assert filled["event_cnt"] == 10 and filled["block_rate"] == 0.1
    assert filled["partial"] is False
    assert data["points"][-1]["partial"] is True
    assert sum(1 for p in data["points"] if p["partial"]) == 1


async def test_trend_degrades_when_base_granularity_is_hourly():
    """BR-11-16：基础写入粒度已是 `1h` 时，`range=1h` 降级为 1 个点 + `truncated`。"""
    hour_ts = align(NOW, "1h")
    repo = FakeRepo(series={("global", "all"): {
        hour_ts: {"event_cnt": 600, "reject_cnt": 60},
    }})
    data = await query(repo, write_granularity="1h").trend(range_="1h")
    assert data["truncated"] is True
    assert data["granularity"] == "1h"
    assert len(data["points"]) == 1
    assert data["points"][0]["bucket_ts"] == hour_ts
    assert data["points"][0]["event_cnt"] == 600
    assert data["points"][0]["block_rate"] == 0.1

    # 但 24h 档不受影响（它本来就按 1h 取点）
    normal = await query(repo, write_granularity="1h").trend(range_="24h")
    assert normal["truncated"] is False and len(normal["points"]) == 24


# ============================================================ §3.4 分布
async def test_distribution_level_uses_level_buckets_and_fixed_order():
    repo = FakeRepo(by_key={"level": [
        {"bucket_key": "medium", "event_cnt": 30},
        {"bucket_key": "high", "event_cnt": 10},
        {"bucket_key": "low", "event_cnt": 60},
    ]})
    data = await query(repo).distribution(range_="24h", dim="level")
    assert [i["key"] for i in data["items"]] == ["low", "medium", "high"]   # BR-11-22 固定序
    assert [i["name"] for i in data["items"]] == ["低风险", "中风险", "高风险"]
    assert [i["cnt"] for i in data["items"]] == [60, 30, 10]
    assert data["total"] == 100
    assert data["items"][0]["ratio"] == 0.6


async def test_distribution_level_with_scene_filter_maps_decision_counters():
    """§3.4：有 `scene` 筛选时读场景桶的分档计数映射为 low/medium/high。"""
    repo = FakeRepo(sums={("scene", "order"): {
        "event_cnt": 10, "pass_cnt": 5, "review_cnt": 3, "reject_cnt": 2,
    }})
    data = await query(repo).distribution(range_="24h", scene="order", dim="level")
    assert {i["key"]: i["cnt"] for i in data["items"]} == {"low": 5, "medium": 3, "high": 2}
    assert data["total"] == 10


async def test_distribution_scene_uses_decision_counter_when_level_filtered():
    """§3.4：`dim=scene&level=high` -> 每个场景取自己的 `reject_cnt`。"""
    repo = FakeRepo(by_key={"scene": [
        {"bucket_key": "order", "event_cnt": 10, "pass_cnt": 5, "review_cnt": 3, "reject_cnt": 2},
        {"bucket_key": "coupon", "event_cnt": 4, "pass_cnt": 1, "review_cnt": 1, "reject_cnt": 2},
        {"bucket_key": "login", "event_cnt": 9, "pass_cnt": 9, "review_cnt": 0, "reject_cnt": 0},
    ]})
    data = await query(repo).distribution(range_="24h", level="high", dim="scene")
    counts = {i["key"]: i["cnt"] for i in data["items"]}
    assert counts == {"order": 2, "coupon": 2, "login": 0}
    assert data["total"] == 4
    # 固定序：login / coupon / order（SCENE_KEYS 的顺序），未知桶不出现
    assert [i["key"] for i in data["items"]] == ["login", "coupon", "order"]


async def test_distribution_scene_keeps_unknown_only_when_it_has_data():
    """BR-11-06：`unknown` 桶只在确实有脏数据时出现在图例里。"""
    repo = FakeRepo(by_key={"scene": [
        {"bucket_key": "order", "event_cnt": 3},
        {"bucket_key": "unknown", "event_cnt": 0},
    ]})
    empty = await query(repo).distribution(range_="24h", dim="scene")
    assert [i["key"] for i in empty["items"]] == ["order"]

    repo2 = FakeRepo(by_key={"scene": [
        {"bucket_key": "order", "event_cnt": 3},
        {"bucket_key": "unknown", "event_cnt": 2},
    ]})
    with_unknown = await query(repo2).distribution(range_="24h", dim="scene")
    assert [i["key"] for i in with_unknown["items"]] == ["order", "unknown"]
    assert with_unknown["items"][-1]["name"] == "未知场景"


# ============================================================ §3.5 排行
@pytest.mark.parametrize(
    "level,metric_field",
    [(None, "hit_cnt"), ("high", "hit_cnt"), ("medium", "review_hit_cnt"),
     ("low", "pass_hit_cnt")],
)
async def test_rule_ranking_metric_field_switches_with_level(level, metric_field):
    """V-11-06 / BR-11-12：口径随 `level` 切换并**回显** `metric_field`。"""
    repo = FakeRepo(
        by_key={"rule": [
            {"bucket_key": "R001", "hit_cnt": 9, "pass_hit_cnt": 4,
             "review_hit_cnt": 3, "reject_hit_cnt": 2, "hit_score_sum": 90},
        ]},
        sums={("global", "all"): {"event_cnt": 100}},
    )
    data = await query(repo).rule_ranking(range_="24h", level=level)
    assert data["metric_field"] == metric_field
    item = data["items"][0]
    assert item["hit_cnt"] == {"hit_cnt": 9, "review_hit_cnt": 3, "pass_hit_cnt": 4}[metric_field]
    assert item["hit_score_sum"] == 90
    assert item["hit_ratio"] == ratio(item["hit_cnt"], 100)
    assert item["rank"] == 1


async def test_rule_ranking_ties_share_rank_and_sort_by_code():
    """§2.4 / BR-11-22：并列同名次，且并列内部按 `rule_code` 升序。"""
    repo = FakeRepo(
        by_key={"rule": [
            {"bucket_key": "R003", "hit_cnt": 5, "hit_score_sum": 50},
            {"bucket_key": "R001", "hit_cnt": 5, "hit_score_sum": 51},
            {"bucket_key": "R002", "hit_cnt": 9, "hit_score_sum": 90},
            {"bucket_key": "R004", "hit_cnt": 1, "hit_score_sum": 10},
        ]},
        sums={("global", "all"): {"event_cnt": 0}},
    )
    data = await query(repo).rule_ranking(range_="24h")
    assert [(i["rule_code"], i["rank"]) for i in data["items"]] == [
        ("R002", 1), ("R001", 2), ("R003", 2), ("R004", 4),
    ]
    # 分母为 0 -> null（BR-11-08），不得把"没有事件"渲染成 0%
    assert all(i["hit_ratio"] is None for i in data["items"])


async def test_rule_ranking_joins_name_and_falls_back_to_code():
    """§2.4：联查 `rules.name`；规则已删除时回落 `rule_code` 且 `rule_status=deleted`。"""
    repo = FakeRepo(
        by_key={"rule": [
            {"bucket_key": "R001", "hit_cnt": 3, "hit_score_sum": 30},
            {"bucket_key": "R404", "hit_cnt": 1, "hit_score_sum": 5},
        ]},
        sums={("global", "all"): {"event_cnt": 10}},
        rules={"R001": {"name": "同设备多账号", "status": "enabled"}},
    )
    data = await query(repo).rule_ranking(range_="24h", top=10)
    first, second = data["items"]
    assert (first["rule_name"], first["rule_status"]) == ("同设备多账号", "enabled")
    assert (second["rule_name"], second["rule_status"]) == ("R404", "deleted")
    assert first["hit_ratio"] == 0.3


async def test_rule_ranking_applies_top_limit_and_rounds_ratio():
    repo = FakeRepo(
        by_key={"rule": [
            {"bucket_key": f"R{i:03d}", "hit_cnt": 10 - i, "hit_score_sum": i}
            for i in range(1, 8)
        ]},
        sums={("global", "all"): {"event_cnt": 3}},
    )
    data = await query(repo).rule_ranking(range_="24h", top=3)
    assert len(data["items"]) == 3
    assert data["items"][0]["hit_ratio"] == round_half_up(9 / 3)   # 3.0
    assert data["items"][2]["hit_ratio"] == round_half_up(7 / 3)   # 2.3333


# ============================================================ §3.6 吞吐
async def test_throughput_uses_last_closed_minute_and_window_sum():
    """§3.6：`qps_1m` 取最近一个**已闭合**的 1m 桶；窗口只统计闭合分钟。"""
    window_end = align(NOW, "1m")
    last_closed = window_end - MINUTE
    repo = FakeRepo(
        sums={("global", "all"): {"event_cnt": 1200}},
        bucket={last_closed: {"metrics": {"event_cnt": 600}}},
    )
    data = await query(repo).throughput(window="5m")
    assert data["window"] == "5m"
    assert data["qps_1m"] == 10.0                     # 600 / 60，保留 1 位小数
    assert data["qps_window"] == 4.0                  # 1200 / 300s
    assert data["event_cnt"] == 1200
    assert data["window_end"] == window_end
    assert data["window_start"] == window_end - WINDOW_MS["5m"]
    assert ("get_bucket", "global", "all") in repo.calls


async def test_throughput_missing_hist_returns_null_p95():
    """BR-11-13 / MET-5003：直方图无样本 -> `p95_elapsed_ms = null`（不是 0）。"""
    repo = FakeRepo(bucket={})
    data = await query(repo).throughput(window="1m")
    assert data["p95_elapsed_ms"] is None and data["p95_elapsed_ms"] != 0
    assert data["elapsed_hist"] == {k: 0 for k in HIST_KEYS}
    assert data["qps_1m"] == 0.0                       # 桶不存在 = 真的 0 请求，可用 0
    assert data["event_cnt"] == 0


async def test_p95_approximation_error_within_ten_percent():
    """V-11-12 / BR-11-13：1000 条已知样本造桶，与真 P95 比对，误差 ≤10%。"""
    samples = [i % 200 + 1 for i in range(1000)]      # 1~200 均匀分布，各 5 条
    hist = {k: 0 for k in HIST_KEYS}
    for value in samples:
        hist[hist_bucket(value)] += 1
    approx = percentile(hist)
    truth = true_p95(samples)
    assert approx is not None
    assert abs(approx - truth) / truth <= 0.10, f"P95 近似 {approx} 与真值 {truth} 偏差过大"

    # 端到端也走一遍：吞吐接口返回的 p95 与引擎一致
    repo = FakeRepo(hist=hist)
    data = await query(repo).throughput(window="5m")
    assert data["p95_elapsed_ms"] == approx
