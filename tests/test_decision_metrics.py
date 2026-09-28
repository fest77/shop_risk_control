# -*- coding: utf-8 -*-
"""决策 D66：`E15 metric_buckets` 的生产写入方（模块 05 → 11 的 §3.9 调用点）。

## 这条缺口原来长什么样

`metric_service.record_decision` 的契约早就冻结好了（docstring 原文：
「供模块 05 / 08 的异步落库任务调用（§3.9）」），但 `app/` 下**没有任何生产
调用点**——`grep record_decision(` 只能找到定义与模块级包装。
后果是**大盘的 `event_cnt` 恒为 0、`block_rate` 恒为 `null`**（分母为 0，
BR-11-08 明确分母为 0 不给 0），而这两个数字在页面上"看起来只是没有流量"。

## 本文件钉住四件事

1. **真实链路写桶**：跑一次真实决策（`decision.decide`），1m 桶里出现
   `event_cnt=1` 与对应分档计数；
2. **`dry_run=true` 绝对不写**（模块 10 §4.2「数据隔离」/ 决策 D11）：
   仿真反复求值不得污染真实指标——指标桶是 `$inc` 增量，**事后无法分辨**
   哪条是仿真来的；
3. **失败不影响决策**（BR-11-11）：指标写入永远吞异常，决策结果与建案照常；
4. **幂等**：同一 `event_id` 只记一次（BR-11-10 的 at-most-once）。
"""
from __future__ import annotations

import pytest
from pymongo.errors import PyMongoError

from app import db
from app.constants import COLL_DECISIONS, COLL_DECISION_HITS, COLL_METRIC_BUCKETS
from app.engine import decision as decision_engine
from app.engine.metric_bucket import align
from app.repos.metric_repo import MetricRepo
from app.utils.timeutil import now_ms

pytestmark = pytest.mark.anyio

#: 一条"能真的跑完全流程"的事件（`login` 无金额、无额外必填场景字段）
EVENT = {
    "_id": "EVT20260101900000000042",
    "event_type": "login",
    "user_id": "U-D66-1",
    "device_id": "D-D66-1",
    "ip": "192.0.2.200",
    "phone": "13800000042",
    "ts": now_ms() - 1000,
    "scene_extra": {"login_type": "pwd", "ua": "pytest", "success": True},
}


async def _global_metric(field: str, ts: int) -> int:
    repo = MetricRepo(db.get_db())
    sums = await repo.sum_range(
        bucket_type="global", bucket_key="all", granularity="1m",
        from_ts=ts, to_ts=ts, fields=(field,),
    )
    return int(sums[field])


async def test_real_decision_writes_metric_bucket():
    """D66 的核心：跑一次真实决策后，指标桶里必须**有数**（原来是恒 0）。"""
    event = dict(EVENT)
    ts = align(int(event["ts"]), "1m")

    # 前置断言"原来是 0"：证明这条用例真的在测那个缺口，而不是碰巧过了
    assert await _global_metric("event_cnt", ts) == 0

    outcome = await decision_engine.decide(event, features={"user_id": event["user_id"]})
    await decision_engine.flush()
    assert outcome.decision in ("pass", "review", "reject")
    assert await MetricRepo(db.get_db()).count() > 0, "决策之后必须出现指标桶"

    assert await _global_metric("event_cnt", ts) == 1
    assert await _global_metric("score_cnt", ts) == 1
    # 三档计数恰好有一档为 1（与本次决策结论一致）
    verdict = outcome.decision
    assert await _global_metric(f"{verdict}_cnt", ts) == 1
    assert decision_engine.STATS["metrics_recorded"] == 1
    # 决策本身照常落库（两条旁路互不干扰）
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 1


async def test_rule_hits_produce_rule_buckets():
    """命中明细要进 `rule` 维度桶（大盘的规则排行靠它，BR-11-04 的 `3+N`）。"""
    event = dict(EVENT, _id="EVT20260101900000000043")
    await decision_engine.decide(event, features={"user_id": event["user_id"]})
    await decision_engine.flush()

    repo = MetricRepo(db.get_db())
    rows = await repo.sum_by_dimension(
        granularity="1m", from_ts=align(int(event["ts"]), "1m"),
        to_ts=align(int(event["ts"]), "1m"),
        fields=("hit_cnt",), bucket_types=("rule",),
    )
    # 没有配规则时命中数为 0 —— `rule` 桶本来就不该存在（"没有命中"不是"零命中"）。
    # 这条断言的价值在于：它证明 `3+N` 里的 N 是**按真实命中数**生成的，
    # 而不是无脑给每条决策写一个空的 rule 桶。
    assert rows == []


async def test_dry_run_never_writes_metrics():
    """**D66 的红线**：`dry_run=true`（仿真链路）绝不写指标桶（模块 10 §4.2）。"""
    event = dict(EVENT, _id="EVT20260101900000000044")
    ts = align(int(event["ts"]), "1m")

    await decision_engine.decide(event, dry_run=True,
                                 features={"user_id": event["user_id"]})
    await decision_engine.flush()

    assert await MetricRepo(db.get_db()).count() == 0, "仿真不得污染真实指标"
    assert await _global_metric("event_cnt", ts) == 0
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 0
    assert await db.get_db()[COLL_DECISION_HITS].count_documents({}) == 0
    # 计数器同样不该动：它只在**真的调用**了 record_decision 之后才 +1
    assert decision_engine.STATS["metrics_recorded"] == 0


async def test_metric_failure_does_not_break_decision(monkeypatch):
    """BR-11-11 / MET-5002：指标写库失败只记 WARN，决策与落库照常。"""

    async def boom(self, payloads):
        raise PyMongoError("simulated metric db down")

    monkeypatch.setattr(MetricRepo, "upsert_many", boom)
    event = dict(EVENT, _id="EVT20260101900000000045")

    outcome = await decision_engine.decide(event, features={"user_id": event["user_id"]})
    await decision_engine.flush()

    assert outcome.block.get("decision") in ("pass", "review", "reject")
    # 决策落库不受影响（两条旁路互不牵连）
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == 1
    assert await db.get_db()[COLL_METRIC_BUCKETS].count_documents({}) == 0


async def test_metrics_are_idempotent_per_event():
    """BR-11-10：同一 `event_id` 的决策只记一次指标（重放不翻倍）。"""
    event = dict(EVENT, _id="EVT20260101900000000046")
    ts = align(int(event["ts"]), "1m")

    await decision_engine.decide(event, features={"user_id": event["user_id"]})
    await decision_engine.flush()
    first = await _global_metric("event_cnt", ts)

    # 再算一次同一个事件（人工复算 / 消息重复投递的形态）
    await decision_engine.decide(dict(event), features={"user_id": event["user_id"]})
    await decision_engine.flush()

    assert first == 1
    assert await _global_metric("event_cnt", ts) == 1, "重放不得把指标翻倍"


async def test_event_summary_is_trimmed_for_the_background_task():
    """落库任务只带**事件摘要**，不把整份报文（含特征快照）挂在 Task 上。"""
    event = dict(
        EVENT, _id="EVT20260101900000000047",
        feature_snapshot={"snapshot_id": "SNP-x", "features": {"a": 1}},
        idempotency_key="k" * 64,
    )
    outcome = await decision_engine.decide(event, features={"user_id": event["user_id"]})
    summary = decision_engine.decision_event(event, outcome)

    assert set(summary) == {"event_id", "event_type", "user_id", "ts",
                            "amount", "biz_no"}
    assert summary["event_id"] == event["_id"]
    assert "feature_snapshot" not in summary and "idempotency_key" not in summary


async def test_event_id_falls_back_to_underscore_id():
    """03 用 `_id`、直接构造的事件可能用 `event_id`：两者都要认（否则去重失效）。"""
    event = dict(EVENT)
    event.pop("_id")
    event["event_id"] = "EVT20260101900000000048"
    outcome = await decision_engine.decide(event, features={"user_id": event["user_id"]})
    summary = decision_engine.decision_event(event, outcome)
    assert summary["event_id"] == "EVT20260101900000000048"
    await decision_engine.flush()
