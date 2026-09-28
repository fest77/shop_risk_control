# -*- coding: utf-8 -*-
"""BR-11-04 / 06 / 13 / 14：决策 → 桶载荷的维度、计数与口径。

这些用例**不碰数据库**：`metric_agg` 是纯函数，口径一旦算错，大盘上所有数字都会
错，而错误不会以异常的形式暴露（只是数字不对）。因此这里的断言细到每一个 `$inc`
字段——"跑通即通过"的测试对这类缺陷毫无价值。
"""
from __future__ import annotations

import pytest

from app.engine.metric_agg import (
    EVENT_SCENE_MAP,
    HIST_KEYS,
    SAVED_AMOUNT_TYPES,
    SCENE_KEYS,
    build_payloads,
    hist_bucket,
    saved_amount_for,
    scene_of,
)
from app.engine.metric_bucket import align, expire_at

pytestmark = pytest.mark.anyio

# 固定事件时刻（对齐用；UTC 毫秒时间戳，全项目统一约定）
NOW = 1758600271208


def _event(**over) -> dict:
    event = {
        "event_id": "EVT20260923000218",
        "event_type": "coupon_receive",
        "user_id": "U000128",
        "ts": NOW,
        "amount": 20000,
    }
    event.update(over)
    return event


def _decision(**over) -> dict:
    decision = {
        "event_id": "EVT20260923000218",
        "scene_code": "coupon",
        "risk_level": "high",
        "decision": "reject",
        "final_score": 86,
        "elapsed_ms": 18,
    }
    decision.update(over)
    return decision


HITS = [
    {"rule_code": "RCOUPON001", "score": 30},
    {"rule_code": "RCOUPON002", "score": 25},
    {"rule_code": "RDEVICE007", "score": 20},
]


def _payloads(**over) -> list[dict]:
    kwargs = {"event": _event(), "decision": _decision(), "hits": HITS, "now_ms": NOW}
    kwargs.update(over)
    return build_payloads(**kwargs)


def _by_id(payloads: list[dict]) -> dict[str, dict]:
    return {p["_id"]: p for p in payloads}


# ============================================================ BR-11-04 桶数量与维度
async def test_one_decision_writes_three_plus_hits_buckets():
    """BR-11-04 / V-11-01：一次决策写 `3 + N` 个桶（global + level + scene + N 条规则）。"""
    payloads = _payloads()
    assert len(payloads) == 3 + len(HITS) == 6

    types = [p["bucket_type"] for p in payloads]
    assert types.count("global") == 1
    assert types.count("level") == 1
    assert types.count("scene") == 1
    assert types.count("rule") == 3

    # `_id` 必须带 granularity 段（BR-11-01 的修正）：整点时 1m 与 1h 桶的
    # bucket_ts 相同，少了这一段会互相覆盖
    assert set(_by_id(payloads)) == {
        f"global:all:1m:{align(NOW, '1m')}",
        f"level:high:1m:{align(NOW, '1m')}",
        f"scene:coupon:1m:{align(NOW, '1m')}",
        f"rule:RCOUPON001:1m:{align(NOW, '1m')}",
        f"rule:RCOUPON002:1m:{align(NOW, '1m')}",
        f"rule:RDEVICE007:1m:{align(NOW, '1m')}",
    }


async def test_hit_without_rule_code_is_skipped():
    """命中条目缺 `rule_code` 时跳过该条，而不是写入一个 `rule:` 空键的桶。"""
    payloads = _payloads(hits=[{"rule_code": "R1"}, {"score": 10}, {"rule_code": ""}])
    rule_ids = [p["_id"] for p in payloads if p["bucket_type"] == "rule"]
    assert rule_ids == [f"rule:R1:1m:{align(NOW, '1m')}"]


async def test_zero_hits_still_writes_three_dimensions():
    """N=0（无命中规则）时仍写 global/level/scene 三个桶——否则事件量会凭空少算。"""
    payloads = _payloads(hits=[])
    assert [p["bucket_type"] for p in payloads] == ["global", "level", "scene"]


# ============================================================ BR-11-05 静态字段
async def test_static_fields_go_to_set_on_insert():
    """BR-11-05：静态字段只走 `$setOnInsert`，`created_at` 不因重复 upsert 被刷新。"""
    payload = _by_id(_payloads())[f"global:all:1m:{align(NOW, '1m')}"]
    assert payload["set_on_insert"] == {"created_at": NOW}
    assert payload["bucket_type"] == "global"
    assert payload["bucket_key"] == "all"
    assert payload["granularity"] == "1m"
    assert payload["bucket_ts"] == align(NOW, "1m")
    # 桶内只有 inc 与 set_on_insert 两类写入指令，没有任何"读改写"的痕迹
    assert set(payload) == {
        "_id", "bucket_type", "bucket_key", "granularity", "bucket_ts",
        "set_on_insert", "expire_at", "inc",
    }


async def test_expire_at_follows_retention_policy():
    """BR-11-21：1m 桶 +7 天、1d 桶不带 `expire_at`（= 永不过期）。"""
    minute_ts = align(NOW, "1m")
    assert _payloads()[0]["expire_at"] == expire_at("1m", minute_ts) == minute_ts + 7 * 86_400_000

    day_ts = align(NOW, "1d")
    day_payloads = _payloads(granularity="1d")
    assert day_payloads[0]["bucket_ts"] == day_ts
    assert day_payloads[0]["expire_at"] is None


# ============================================================ BR-11-14 计数与资损
async def test_global_bucket_carries_full_metric_set():
    """global 桶承载全部计数、分值、资损与延迟直方图（§3.10）。"""
    inc = _by_id(_payloads())[f"global:all:1m:{align(NOW, '1m')}"]["inc"]
    assert inc["metrics.event_cnt"] == 1
    assert inc["metrics.reject_cnt"] == 1
    assert inc["metrics.pass_cnt"] == 0 and inc["metrics.review_cnt"] == 0
    assert inc["metrics.score_sum"] == 86 and inc["metrics.score_cnt"] == 1
    # BR-11-14：资损按事件类型分项，仅 reject 计入
    assert inc["metrics.estimated_saved_amount"] == 20000
    assert inc["metrics.saved_amount_by_type.coupon_receive"] == 20000
    # BR-11-13：延迟直方图写入时累加（18ms -> le20 档）
    assert inc["metrics.elapsed_sum"] == 18 and inc["metrics.elapsed_cnt"] == 1
    assert inc["metrics.elapsed_hist.le20"] == 1


async def test_level_bucket_uses_one_to_one_decision_mapping():
    """BR-11-12：`reject↔high`，等级桶只让对应那一档计 1。"""
    inc = _by_id(_payloads())[f"level:high:1m:{align(NOW, '1m')}"]["inc"]
    assert inc["metrics.event_cnt"] == 1
    assert inc["metrics.reject_cnt"] == 1
    assert inc["metrics.pass_cnt"] == 0 and inc["metrics.review_cnt"] == 0
    # 等级桶不带延迟统计（§3.10：只有 global 桶有 elapsed_*）
    assert not [k for k in inc if k.startswith("metrics.elapsed")]


async def test_scene_bucket_key_comes_from_event_type_not_rule_scene():
    """N-11-7：`scene` 桶按**事件实际场景**归类，`common` 规则不得造出 `common` 桶。"""
    payloads = _payloads(decision=_decision(scene_code="common"))
    scene_ids = [p["_id"] for p in payloads if p["bucket_type"] == "scene"]
    assert scene_ids == [f"scene:coupon:1m:{align(NOW, '1m')}"]


async def test_rule_bucket_counts_each_hit_once():
    """`rule` 桶**每条命中一次**，并带三档命中数与命中分之和。"""
    by_id = _by_id(_payloads())
    ts = align(NOW, "1m")
    first = by_id[f"rule:RCOUPON001:1m:{ts}"]["inc"]
    assert first["metrics.hit_cnt"] == 1
    assert first["metrics.reject_hit_cnt"] == 1
    assert first["metrics.pass_hit_cnt"] == 0 and first["metrics.review_hit_cnt"] == 0
    assert first["metrics.hit_score_sum"] == 30
    assert by_id[f"rule:RDEVICE007:1m:{ts}"]["inc"]["metrics.hit_score_sum"] == 20


async def test_unknown_event_type_falls_back_to_unknown_scene():
    """BR-11-06：事件类型无法映射场景时写 `unknown` 桶，且不阻断其余维度。"""
    payloads = _payloads(event=_event(event_type="mystery_event"))
    by_type = {p["bucket_type"]: p for p in payloads}
    assert by_type["scene"]["bucket_key"] == "unknown"
    # 其余三维度照常写入（容错设计：脏数据不该让整次统计丢失）
    assert set(by_type) == {"global", "level", "scene", "rule"}
    assert len(payloads) == 6


async def test_saved_amount_only_counts_reject_and_never_login():
    """BR-11-14 / V-11-08：仅 reject 计入；`login` 恒为 0；缺 `amount` 按 0 计。"""
    assert saved_amount_for("order_create", "reject", 20000) == 20000
    assert saved_amount_for("order_pay", "reject", 30000) == 30000
    assert saved_amount_for("login", "reject", 50000) == 0          # login 恒 0
    assert saved_amount_for("order_create", "pass", 20000) == 0
    assert saved_amount_for("order_create", "review", 20000) == 0
    assert saved_amount_for("order_create", "reject", None) == 0     # 缺 amount 按 0
    assert saved_amount_for("not_an_event", "reject", 999) == 0

    # V-11-08 的三条 reject 事件：20000 + 30000 + 0 = 50000
    total = sum(
        saved_amount_for("order_create", "reject", amount)
        for amount in (20000, 30000, 0)
    )
    assert total == 50000

    # 分项：不同事件类型各记各的
    inc_a = _by_id(_payloads(event=_event(event_type="order_pay", amount=30000)))[
        f"global:all:1m:{align(NOW, '1m')}"]["inc"]
    assert inc_a["metrics.saved_amount_by_type.order_pay"] == 30000
    assert inc_a["metrics.estimated_saved_amount"] == 30000
    # login 的 reject 不产生资损（口径 G-09 的明确例外）
    inc_b = _by_id(_payloads(event=_event(event_type="login", amount=50000)))[
        f"global:all:1m:{align(NOW, '1m')}"]["inc"]
    assert inc_b["metrics.estimated_saved_amount"] == 0
    assert inc_b["metrics.saved_amount_by_type.login"] == 0


async def test_saved_amount_types_cover_all_five_event_types():
    """资损分项字段覆盖五类事件（02 的「口径切换与排查」依赖它）。"""
    assert SAVED_AMOUNT_TYPES == (
        "login", "coupon_receive", "order_create", "order_pay", "after_sale_apply",
    )


# ============================================================ BR-11-13 直方图
async def test_hist_bucket_boundaries():
    """BR-11-13：分桶边界为 `[5,10,20,30,50,75,100,200,+∞)`，取闭区间上界。"""
    assert HIST_KEYS == ("le5", "le10", "le20", "le30", "le50", "le75", "le100", "le200", "gt200")
    assert hist_bucket(0) == "le5"
    assert hist_bucket(5) == "le5"
    assert hist_bucket(6) == "le10"
    assert hist_bucket(200) == "le200"
    assert hist_bucket(201) == "gt200"
    assert hist_bucket(None) is None          # 缺 elapsed_ms 不计数
    # 无延迟统计时不写 elapsed_hist 键，避免"看起来有样本"
    inc = _by_id(_payloads(decision=_decision(elapsed_ms=None)))[
        f"global:all:1m:{align(NOW, '1m')}"]["inc"]
    assert inc["metrics.elapsed_sum"] == 0 and inc["metrics.elapsed_cnt"] == 0
    assert not [k for k in inc if k.startswith("metrics.elapsed_hist")]


# ============================================================ 场景映射
async def test_scene_mapping_keeps_pay_reachable():
    """N-11-6 / 决策 D12：`order_pay -> pay`，否则 E06 的 `pay` 场景永远不可达。"""
    assert EVENT_SCENE_MAP["order_pay"] == "pay"
    assert set(EVENT_SCENE_MAP.values()) == set(SCENE_KEYS)
    assert scene_of("order_pay") == "pay"
    assert scene_of("after_sale_apply") == "aftersale"
    assert scene_of("whatever") == "unknown"
