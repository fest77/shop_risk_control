# -*- coding: utf-8 -*-
"""BR-11-05 / 10 / 11 / 21：写入路径的幂等、`$inc` upsert 约束、失败不阻断与 TTL。

**这一组用例同时是"实现没走捷径"的证据**：
- 载荷必须是 `UpdateOne + $inc + $setOnInsert + upsert`（BR-11-05），
  用代理集合把实际下发的操作拦下来逐条断言，而不是只信任代码注释；
- 同一 `event_id` 重放 5 次只 +1（BR-11-10 / V-11-11）；
- 写库失败必须被吞掉并计数（MET-5002），决策链路不受影响（BR-11-11）。
"""
from __future__ import annotations

import pytest
from pymongo import UpdateOne
from pymongo.errors import PyMongoError

from app import config, db
from app.constants import COLL_METRIC_BUCKETS
from app.engine.metric_agg import build_payloads
from app.engine.metric_bucket import align
from app.repos.metric_repo import MetricRepo
from app.services.metric_service import MetricService

pytestmark = pytest.mark.anyio

NOW = 1758600271208
# 读改写方法族：BR-11-05 明令禁止（并发下必然丢更新）
_READ_MODIFY_WRITE = (
    "find_one_and_update", "find_one_and_replace", "find_one_and_delete",
    "find_and_modify", "findAndModify",
)


def _event(event_id: str = "EVT-IDEM-1", **over) -> dict:
    event = {"event_id": event_id, "event_type": "order_create", "user_id": "U1",
             "ts": NOW, "amount": 20000}
    event.update(over)
    return event


_DECISION = {"event_id": "EVT-IDEM-1", "scene_code": "order", "risk_level": "high",
             "decision": "reject", "final_score": 86, "elapsed_ms": 18}
_HITS = [{"rule_code": "R1", "score": 30}, {"rule_code": "R2", "score": 25},
         {"rule_code": "R3", "score": 20}]


class _SpyCollection:
    """代理真实集合：记录下发的写操作，并对读改写方法立刻报错。"""

    def __init__(self, real):
        self._real = real
        self.ops: list = []

    async def bulk_write(self, requests, ordered=False):
        self.ops.extend(requests)
        return await self._real.bulk_write(requests, ordered=ordered)

    def __getattr__(self, name):
        if name in _READ_MODIFY_WRITE:
            raise AssertionError(f"指标写入不得使用读改写方法 {name}（BR-11-05）")
        return getattr(self._real, name)


class _SpyDb:
    def __init__(self, real, spy):
        self._real = real
        self._spy = spy

    def __getitem__(self, name):
        return self._spy if name == COLL_METRIC_BUCKETS else self._real[name]


async def _metric_value(field: str, ts: int, key: str = "all",
                        bucket_type: str = "global") -> int:
    repo = MetricRepo(db.get_db())
    sums = await repo.sum_range(
        bucket_type=bucket_type, bucket_key=key, granularity="1m",
        from_ts=ts, to_ts=ts, fields=(field,),
    )
    return sums[field]


# ============================================================ BR-11-10 幂等
async def test_same_event_id_replayed_five_times_counts_once():
    """V-11-11 / BR-11-10：同一 `event_id` 重放 5 次，`event_cnt` 只 +1。"""
    service = MetricService()
    ts = align(NOW, "1m")
    for _ in range(5):
        await service.record_decision(_event(), _DECISION, _HITS)

    assert await _metric_value("event_cnt", ts) == 1
    assert await _metric_value("reject_cnt", ts) == 1
    assert await _metric_value("estimated_saved_amount", ts) == 20000
    assert service.stats["replay_skipped"] == 4
    assert service.stats["write_ok"] == 1
    # 一次决策 6 个桶（3 + 3 条命中），重放不再新增
    assert await MetricRepo(db.get_db()).count() == 6


async def test_distinct_event_ids_are_counted_separately():
    """去重只对重复 `event_id` 生效：不同事件必须各记一次（防"去重过头"）。"""
    service = MetricService()
    ts = align(NOW, "1m")
    await service.record_decision(_event("EVT-A"), _DECISION, _HITS)
    await service.record_decision(_event("EVT-B", amount=5000), _DECISION, _HITS)

    assert await _metric_value("event_cnt", ts) == 2
    assert await _metric_value("estimated_saved_amount", ts) == 25000
    assert service.stats["replay_skipped"] == 0
    # 同一规则被两次命中 -> 规则桶 hit_cnt = 2
    assert await _metric_value("hit_cnt", ts, key="R1", bucket_type="rule") == 2


# ============================================================ BR-11-05 写路径
async def test_write_path_is_inc_upsert_with_set_on_insert():
    """BR-11-05：每个载荷翻译成 `UpdateOne(_id, {$inc, $setOnInsert}, upsert=True)`。"""
    spy = _SpyCollection(db.get_db()[COLL_METRIC_BUCKETS])
    repo = MetricRepo(_SpyDb(db.get_db(), spy))
    payloads = build_payloads(event=_event(), decision=_DECISION, hits=_HITS, now_ms=NOW)
    written = await repo.upsert_many(payloads)

    assert written == 6
    assert len(spy.ops) == 6
    payload_ids = {p["_id"] for p in payloads}
    for op in spy.ops:
        assert isinstance(op, UpdateOne)
        assert op._upsert is True, "必须 upsert：桶不存在时要新建"
        assert set(op._doc) == {"$inc", "$setOnInsert"}
        assert op._doc["$inc"], "必须有增量指令，否则写入等于空操作"
        assert list(op._filter) == ["_id"]
        assert op._filter["_id"] in payload_ids
        static = op._doc["$setOnInsert"]
        assert set(static) >= {"bucket_type", "bucket_key", "granularity", "bucket_ts",
                               "created_at"}
        # 1m 桶必须带 expire_at（TTL 生效的前提）；1d 桶则**不带**该字段
        assert isinstance(static.get("expire_at"), int)


async def test_duplicate_ids_in_one_batch_are_merged():
    """同一批里重复的 `_id` 先合并再下发：无序 bulk 对同一文档两次更新结果未定义。"""
    spy = _SpyCollection(db.get_db()[COLL_METRIC_BUCKETS])
    repo = MetricRepo(_SpyDb(db.get_db(), spy))
    payloads = build_payloads(
        event=_event(), decision=_DECISION,
        hits=[{"rule_code": "R1", "score": 30}, {"rule_code": "R1", "score": 30}],
        now_ms=NOW,
    )
    assert len(payloads) == 5
    assert len({p["_id"] for p in payloads}) == 4
    await repo.upsert_many(payloads)
    assert len(spy.ops) == 4
    rule_op = [op for op in spy.ops if op._filter["_id"].startswith("rule:R1")][0]
    assert rule_op._doc["$inc"]["metrics.hit_cnt"] == 2       # 合并成一次 +2


async def test_repo_source_never_uses_read_modify_write():
    """源码级守卫：仓储里不得出现读改写方法（BR-11-05 / AD-03）。"""
    source = (config.ROOT / "app" / "repos" / "metric_repo.py").read_text(encoding="utf-8")
    assert '"$inc"' in source and "$setOnInsert" in source
    for banned in _READ_MODIFY_WRITE:
        assert banned not in source, f"metric_repo 不得出现 {banned}"


# ============================================================ BR-11-11 失败不阻断
async def test_write_failure_is_swallowed_and_counted(monkeypatch):
    """MET-5002 / BR-11-11：写库失败只记 WARN + 计数，**绝不抛出**。"""

    async def boom(self, payloads):
        raise PyMongoError("simulated metric db down")

    monkeypatch.setattr(MetricRepo, "upsert_many", boom)
    service = MetricService()
    # 不抛异常本身就是断言：指标写失败不能把决策链路一起拖垮
    await service.record_decision(_event(), _DECISION, _HITS)
    assert service.stats["write_fail"] == 1
    assert service.stats["write_ok"] == 0
    assert await MetricRepo(db.get_db()).count() == 0


async def test_record_decision_swallows_malformed_input():
    """入参缺字段（05 尚未落地时可能发生）不得抛异常，只记 WARN + 计数。

    这里刻意构造一次**真实的写库失败**：`event_type` 为空串时，引擎会写出
    `metrics.saved_amount_by_type.` 这种"空字段名"路径（它按 E01 枚举设计，不做空值
    兜底），Mongo 会拒绝整批 upsert。要点是——失败被吞掉并计数，决策链路毫无感知。
    """
    service = MetricService()
    await service.record_decision({}, {}, [])          # 关键断言：不抛异常
    assert service.stats["write_fail"] == 1
    assert await MetricRepo(db.get_db()).count() == 0

    # 但"未知事件类型"必须能写进去（BR-11-06 的容错路径不能被上面的失败牵连）
    await service.record_decision(
        {"event_id": "EVT-UNKNOWN", "event_type": "mystery_event", "user_id": "U1",
         "ts": NOW}, _DECISION, [],
    )
    ts = align(NOW, "1m")
    assert await _metric_value("event_cnt", ts) == 1
    assert await _metric_value("event_cnt", ts, key="unknown", bucket_type="scene") == 1


async def test_record_decision_swallows_bad_ts_but_still_pushes_stream():
    """脏 `ts`（字符串）会在载荷构造阶段抛异常——同样必须被吞掉，且不影响实时流。"""
    from app.core.metric_stream_bus import get_bus

    service = MetricService()
    before = get_bus().seq
    await service.record_decision(
        {"event_id": "EVT-BAD-TS", "event_type": "order_create", "user_id": "U1",
         "ts": "not-a-timestamp", "amount": 100},
        _DECISION, [],
    )
    assert service.stats["build_fail"] == 1
    assert service.stats["write_fail"] == 0
    assert get_bus().seq == before + 1, "实时流与桶写入是两条独立通道，前者不该被牵连"


async def test_write_granularity_refuses_day_granularity():
    """BR-11-03：写入粒度不允许 `1d`（会让 24h 趋势只剩一个点）。"""
    from app.services import metric_service

    service = MetricService()
    await service.record_decision(_event(), _DECISION, _HITS, granularity="1d")
    # 回落为配置的基础粒度（1m），因此日粒度桶里不会有数据
    assert await _metric_value("event_cnt", align(NOW, "1m")) == 1
    assert await db.get_db()[COLL_METRIC_BUCKETS].count_documents({"granularity": "1d"}) == 0
    assert metric_service.write_granularity() == "1m"

    # 运行参数入口同样拒绝非法值并保持原值
    assert metric_service.set_write_granularity("1d") == "1m"
    assert metric_service.set_write_granularity("1h") == "1h"
    try:
        assert metric_service.write_granularity() == "1h"
    finally:
        metric_service.set_write_granularity("1m")


# ============================================================ BR-11-21 TTL 索引
async def test_ttl_index_targets_expire_at_field():
    """V-11-18 / BR-11-21：TTL 索引建在 `expire_at` 上且 `expireAfterSeconds=0`。"""
    info = await db.get_db()[COLL_METRIC_BUCKETS].index_information()
    assert "ttl_expire" in info, "缺少 TTL 索引声明（constants.INDEX_SPECS）"
    assert info["ttl_expire"]["key"] == [("expire_at", 1)]
    assert info["ttl_expire"]["expireAfterSeconds"] == 0
    # 维度+时间与粒度+时间两条读路径也必须在（否则趋势/分布会全表扫描）
    assert "ix_dim_ts" in info and "ix_gran_ts" in info
