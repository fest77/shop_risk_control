# -*- coding: utf-8 -*-
"""BR-11-15 / 16：rollup 的幂等重算、字段口径与定时任务。

幂等是本模块最容易被"看起来对"掩盖的一条：`$inc` 写法的第一次执行完全正确，
第二次才翻倍，而验收时往往只跑一次。因此这里对同一小时**连跑 3 次**并逐字段比对，
还额外注入一个脏字段来确认走的是 `$set` 覆盖（脏字段必须消失）而不是 `$inc`。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import db
from app.constants import COLL_METRIC_BUCKETS
from app.core import metric_rollup
from app.core.metric_rollup import RollupScheduler, rollup, seconds_until, target_bucket_starts
from app.engine.metric_bucket import align, bucket_id, expire_at
from app.errors import AppError
from app.repos.metric_repo import MetricRepo
from app.utils.timeutil import CN_TZ, to_dt

pytestmark = pytest.mark.anyio

MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000
NOW = 1758600271208


def cst(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=CN_TZ).timestamp() * 1000)


def _minute_payload(ts: int, *, bucket_type: str = "global", key: str = "all",
                    inc: dict | None = None) -> dict:
    return {
        "_id": bucket_id(bucket_type, key, "1m", ts),
        "bucket_type": bucket_type,
        "bucket_key": key,
        "granularity": "1m",
        "bucket_ts": ts,
        "set_on_insert": {"created_at": ts},
        "expire_at": expire_at("1m", ts),
        "inc": inc or {},
    }


async def _seed_minutes(minutes: int, *, bucket_type: str = "global", key: str = "all",
                        start_ts: int, per_minute: int = 10) -> None:
    """按分钟灌入可预测的计数（每分钟 `per_minute` 事件、1 条拦截）。"""
    payloads = [
        _minute_payload(
            start_ts + i * MINUTE, bucket_type=bucket_type, key=key,
            inc={
                "metrics.event_cnt": per_minute,
                "metrics.pass_cnt": per_minute - 2,
                "metrics.review_cnt": 1,
                "metrics.reject_cnt": 1,
                "metrics.score_sum": 86 * per_minute,
                "metrics.score_cnt": per_minute,
                "metrics.estimated_saved_amount": 2000,
                "metrics.saved_amount_by_type.order_create": 2000,
                "metrics.elapsed_sum": 18 * per_minute,
                "metrics.elapsed_cnt": per_minute,
                "metrics.elapsed_hist.le20": per_minute,
            },
        )
        for i in range(minutes)
    ]
    await MetricRepo(db.get_db()).upsert_many(payloads)


async def _bucket(bucket_type: str, key: str, granularity: str, ts: int) -> dict | None:
    return await db.get_db()[COLL_METRIC_BUCKETS].find_one(
        {"_id": bucket_id(bucket_type, key, granularity, ts)}
    )


# ============================================================ BR-11-15 幂等
async def test_rollup_is_idempotent_over_three_runs():
    """V-11-09 / BR-11-15：同一小时连跑 3 次，`1h` 桶取值完全不变。"""
    hour = align(NOW, "1h") - 2 * HOUR
    await _seed_minutes(60, start_ts=hour)

    snapshots = []
    for _ in range(3):
        result = await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1)
        assert result["scanned_1m_buckets"] == 60
        assert result["upserted"] == 1
        assert isinstance(result["elapsed_ms"], int)
        doc = await _bucket("global", "all", "1h", hour)
        snapshots.append(doc["metrics"])

    assert snapshots[0] == snapshots[1] == snapshots[2], "rollup 不幂等（疑似用了 $inc）"
    metrics = snapshots[0]
    # V-11-10：1h 桶必须等于该小时 60 个 1m 桶之和
    assert metrics["event_cnt"] == 600
    assert metrics["pass_cnt"] == 480 and metrics["review_cnt"] == 60
    assert metrics["reject_cnt"] == 60
    assert metrics["score_sum"] == 86 * 600 and metrics["score_cnt"] == 600
    assert metrics["elapsed_sum"] == 18 * 600 and metrics["elapsed_cnt"] == 600
    assert metrics["elapsed_hist"]["le20"] == 600
    assert metrics["estimated_saved_amount"] == 120000
    assert metrics["saved_amount_by_type"]["order_create"] == 120000
    # 源桶不被改动（rollup 只读 1m、只写目标粒度）
    assert await MetricRepo(db.get_db()).count() == 61


async def test_rollup_overwrites_stale_metric_fields():
    """BR-11-15：用 `$set` 覆盖整个 `metrics`，因此历史脏字段会被清掉（不是 `$inc`）。"""
    hour = align(NOW, "1h") - 2 * HOUR
    await _seed_minutes(60, start_ts=hour)
    await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1)

    await db.get_db()[COLL_METRIC_BUCKETS].update_one(
        {"_id": bucket_id("global", "all", "1h", hour)},
        {"$set": {"metrics.hit_cnt": 999, "metrics.bogus": 1}},
    )
    await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1)

    metrics = (await _bucket("global", "all", "1h", hour))["metrics"]
    assert "hit_cnt" not in metrics and "bogus" not in metrics
    assert metrics["event_cnt"] == 600
    # TTL 字段：1h 桶带 90 天保留期
    doc = await _bucket("global", "all", "1h", hour)
    assert doc["expire_at"] == hour + 90 * DAY


async def test_rollup_creates_all_dimensions_and_trims_fields_by_type():
    """字段按维度裁剪：`rule` 桶不得混入 `event_cnt`（§3.10 的结构约束）。"""
    hour = align(NOW, "1h") - 2 * HOUR
    await _seed_minutes(60, start_ts=hour)
    await _seed_minutes(60, start_ts=hour, bucket_type="level", key="high")
    await _seed_minutes(60, start_ts=hour, bucket_type="scene", key="order")
    await MetricRepo(db.get_db()).upsert_many([
        _minute_payload(hour + i * MINUTE, bucket_type="rule", key="R001",
                        inc={"metrics.hit_cnt": 2, "metrics.reject_hit_cnt": 1,
                             "metrics.hit_score_sum": 40})
        for i in range(60)
    ])

    result = await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1)
    assert result["scanned_1m_buckets"] == 240

    global_metrics = (await _bucket("global", "all", "1h", hour))["metrics"]
    level_metrics = (await _bucket("level", "high", "1h", hour))["metrics"]
    scene_metrics = (await _bucket("scene", "order", "1h", hour))["metrics"]
    rule_metrics = (await _bucket("rule", "R001", "1h", hour))["metrics"]

    assert global_metrics["event_cnt"] == 600 and "elapsed_hist" in global_metrics
    assert level_metrics["reject_cnt"] == 60 and "elapsed_sum" not in level_metrics
    assert scene_metrics["event_cnt"] == 600
    assert rule_metrics["hit_cnt"] == 120 and rule_metrics["hit_score_sum"] == 2400
    assert "event_cnt" not in rule_metrics, "rule 桶不得携带事件计数（E15 结构）"


async def test_rollup_dimension_filter_limits_written_buckets():
    """`dimension` 限定只重算某一维度，其余维度不产生桶。"""
    hour = align(NOW, "1h") - 2 * HOUR
    await _seed_minutes(60, start_ts=hour)
    await _seed_minutes(60, start_ts=hour, bucket_type="level", key="high")

    result = await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1,
                          dimension="level")
    assert result["scanned_1m_buckets"] == 60
    assert await _bucket("level", "high", "1h", hour) is not None
    assert await _bucket("global", "all", "1h", hour) is None


async def test_rollup_skips_empty_windows_without_zeroing_history():
    """空窗口不写桶：否则 7 天后 `1m` 桶过期，重算会把正确的小时桶覆盖成全 0。"""
    hour = align(NOW, "1h") - 2 * HOUR
    await _seed_minutes(60, start_ts=hour)
    await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1)

    # 模拟 1m 桶已被 TTL 清理
    await db.get_db()[COLL_METRIC_BUCKETS].delete_many({"granularity": "1m"})
    result = await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1)

    assert result["scanned_1m_buckets"] == 0 and result["upserted"] == 0
    metrics = (await _bucket("global", "all", "1h", hour))["metrics"]
    assert metrics["event_cnt"] == 600, "历史小时桶被空窗口覆盖（数据被静默抹掉）"


# ============================================================ 参数校验 MET-4002/4004/4005
async def test_rollup_rejects_invalid_arguments():
    hour = align(NOW, "1h")
    with pytest.raises(AppError) as e1:
        await rollup(granularity="1h", from_ts=hour + HOUR, to_ts=hour)
    assert e1.value.code == "MET-4005" and e1.value.http_status == 400

    with pytest.raises(AppError) as e2:
        await rollup(granularity="1m", from_ts=hour, to_ts=hour + HOUR - 1)
    assert e2.value.code == "MET-4002"

    with pytest.raises(AppError) as e3:
        await rollup(granularity="1h", from_ts=hour, to_ts=hour + HOUR - 1,
                     dimension="bogus")
    assert e3.value.code == "MET-4004"

    # 单次补算的目标桶有上限：误传"一年 + 1h"会被拒绝而不是把服务挂死
    with pytest.raises(AppError) as e4:
        await rollup(granularity="1h", from_ts=hour - 1001 * HOUR, to_ts=hour)
    assert e4.value.code == "MET-4005"
    assert "expected_buckets" in (e4.value.data or {})

    assert target_bucket_starts(hour, hour + HOUR - 1, "1h", NOW) == [hour]


async def test_rollup_accepts_spec_positional_signature():
    """§3.8 的签名写成 `rollup(granularity, from_ts, to_ts, dimension)`，运维脚本会那样调。"""
    result = await rollup("1h", 0, 3_599_999)
    assert result["upserted"] == 0 and result["scanned_1m_buckets"] == 0
    assert isinstance(result["elapsed_ms"], int)
    assert set(result) == {"upserted", "scanned_1m_buckets", "elapsed_ms"}


# ============================================================ 定时任务
async def test_scheduled_hourly_and_daily_recompute_closed_buckets():
    """BR-11-15：整点后重算**上一个已闭合**小时；每日重算**前一日**日桶。"""
    scheduler = RollupScheduler(interval_sec=0.05)
    closed_hour = align(NOW, "1h") - HOUR
    await _seed_minutes(60, start_ts=closed_hour)

    await scheduler.run_hourly_once(NOW)
    assert scheduler.stats["hourly_runs"] == 1
    assert (await _bucket("global", "all", "1h", closed_hour))["metrics"]["event_cnt"] == 600
    # 当前这个小时（未闭合）不重算
    assert await _bucket("global", "all", "1h", align(NOW, "1h")) is None

    yesterday = align(NOW, "1d") - DAY
    await _seed_minutes(60, start_ts=yesterday + HOUR)
    await scheduler.run_daily_once(NOW)
    assert scheduler.stats["daily_runs"] == 1
    day_doc = await _bucket("global", "all", "1d", yesterday)
    assert day_doc["metrics"]["event_cnt"] == 600
    # 日桶永久保留：不写 expire_at（BR-11-21）
    assert "expire_at" not in day_doc
    # 日桶起点必须是**东八区 00:00**（BR-11-02），而不是 UTC 整日
    midnight = to_dt(yesterday)
    assert (midnight.hour, midnight.minute, midnight.second) == (0, 0, 0)
    assert yesterday == align(NOW, "1d") - DAY


async def test_start_and_stop_rollup_are_idempotent():
    """生命周期可重入：重复 start/stop 不报错、不留悬挂任务。"""
    await metric_rollup.start_rollup()
    await metric_rollup.start_rollup()
    scheduler = metric_rollup.get_rollup_scheduler()
    assert len([t for t in scheduler._tasks if not t.done()]) == 2
    await metric_rollup.stop_rollup()
    await metric_rollup.stop_rollup()
    assert scheduler._tasks == []


async def test_seconds_until_next_daily_run_uses_shanghai_calendar():
    """每日调度按东八区日历算（与 BR-11-02 的日桶时区保持一致）。"""
    assert seconds_until(cst(2026, 9, 23, 10, 0), 0, 5) == 14 * 3600 + 300
    assert seconds_until(cst(2026, 9, 23, 0, 4), 0, 5) == 60
    # 已经过了当日 00:05 -> 顺延到次日
    assert seconds_until(cst(2026, 9, 23, 0, 30), 0, 5) == 23 * 3600 + 35 * 60
