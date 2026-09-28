# -*- coding: utf-8 -*-
"""模块 03 幂等缓存的**纯逻辑**断言（BR-03-11 / 12 / 14，决策 D6）。

TTL / LRU / 载荷哈希 / 在途占位 / 占位释放都是不依赖数据库的纯逻辑，但仍然写成
**异步用例**：`tests/conftest.py` 的 `prep_db` 是 async 的 autouse 夹具，
而 pytest 不允许同步用例依赖 async 夹具（会对每个同步用例报
"'xxx' requested an async fixture 'prep_db' with autouse=True ... pytest does
not natively support it"）。本项目全部既有用例都是 async 的，这里遵循同一约定。

放在单独文件而不是并进 `test_event_gateway.py`：这几个断言与接口无关，
单独一个文件读起来边界更清楚（且共用同一套 conftest 夹具，没有额外成本）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.idempotency import (
    DEFAULT_CAPACITY,
    DEFAULT_TTL_MS,
    IdempotencyStore,
    payload_digest,
)
from app.utils.timeutil import now_ms

pytestmark = pytest.mark.anyio


async def test_store_ttl_expires_entries():
    """BR-03-11 / D6：TTL 到点后条目失效（时钟可注入，因此可确定性验证）。"""
    clock = {"now": 1_000_000}
    store = IdempotencyStore(ttl_ms=1000, capacity=10, clock=lambda: clock["now"])
    digest = payload_digest({"a": 1})
    assert store.reserve("EVT1", digest)[0] == "reserved"
    store.finish("EVT1", {"event_id": "EVT1"})

    clock["now"] += 999
    assert store.check("EVT1", digest)[0] == "hit"

    clock["now"] += 2          # 累计超过 1000ms
    assert store.check("EVT1", digest)[0] == "miss"
    assert store.size() == 0, "check 命中过期分支时应顺手删掉该条目"


async def test_store_default_ttl_and_capacity_match_decision_d6():
    """D6 的两个取值必须真的是 24h / 20000（写成 24 分钟不会自己暴露）。"""
    assert DEFAULT_TTL_MS == 24 * 60 * 60 * 1000
    assert DEFAULT_CAPACITY == 20_000


async def test_store_evicts_least_recently_used_beyond_capacity():
    """BR-03-11 的 LRU 淘汰（容量缩到 2 以便验证语义）。"""
    store = IdempotencyStore(ttl_ms=10 ** 9, capacity=2, clock=now_ms)
    for key in ("EVT1", "EVT2"):
        store.reserve(key, payload_digest({"k": key}))
        store.finish(key, {"event_id": key})

    # 访问 EVT1，使 EVT2 成为最久未使用
    assert store.check("EVT1", payload_digest({"k": "EVT1"}))[0] == "hit"
    store.reserve("EVT3", payload_digest({"k": "EVT3"}))
    assert store.size() == 2
    assert store.check("EVT2", payload_digest({"k": "EVT2"}))[0] == "miss", "EVT2 应已被淘汰"
    assert store.check("EVT1", payload_digest({"k": "EVT1"}))[0] == "hit", (
        "最近使用过的不该被淘汰"
    )


async def test_store_distinguishes_payload_by_digest():
    """BR-03-12 的底层判据：同一 `event_id` 不同载荷必须能被区分。"""
    store = IdempotencyStore(clock=now_ms)
    store.reserve("EVT1", payload_digest({"amount": 100}))
    store.finish("EVT1", {"event_id": "EVT1"})
    assert store.check("EVT1", payload_digest({"amount": 100}))[0] == "hit"
    assert store.check("EVT1", payload_digest({"amount": 200}))[0] == "conflict"


async def test_digest_ignores_key_order_but_not_values():
    """载荷哈希必须对键顺序不敏感、对取值敏感。

    "对键顺序不敏感"是必要的：否则调用方换个字段顺序重发就会被误判成
    `EVT-4009`（编号复用），表现为"幂等只对同一种序列化方式有效"。
    """
    assert payload_digest({"a": 1, "b": 2}) == payload_digest({"b": 2, "a": 1})
    assert payload_digest({"a": 1}) != payload_digest({"a": "1"})
    assert payload_digest({"a": None}) != payload_digest({})


async def test_store_release_frees_the_slot():
    """首领失败必须释放占位，否则该编号会被永久占住（后来者一直等一个不会来的结果）。"""
    store = IdempotencyStore(clock=now_ms)
    digest = payload_digest({"x": 1})
    assert store.reserve("EVT1", digest)[0] == "reserved"
    assert store.check("EVT1", digest)[0] == "inflight"
    store.release("EVT1")
    assert store.check("EVT1", digest)[0] == "miss"


async def test_reserve_is_idempotent_for_the_same_leader():
    """`reserve` 只在"确实没有有效项"时登记，不会覆盖在途占位。

    覆盖在途项会让两个首领并行产出两份决策（V-03-04 的并发版会红）。
    """
    store = IdempotencyStore(clock=now_ms)
    digest = payload_digest({"x": 1})
    state, entry = store.reserve("EVT1", digest)
    assert state == "reserved" and entry is not None
    state2, entry2 = store.reserve("EVT1", digest)
    assert state2 == "inflight", "同一编号的第二位不得再登记为首领"
    assert entry2 is None
    assert store.size() == 1


async def test_reserve_reports_conflict_for_different_digest():
    """不同载荷不得抢占同一编号的在途占位（BR-03-12）。"""
    store = IdempotencyStore(clock=now_ms)
    store.reserve("EVT1", payload_digest({"x": 1}))
    assert store.reserve("EVT1", payload_digest({"x": 2}))[0] == "conflict"


async def test_store_expires_before_evicting_when_both_limits_apply():
    """容量与 TTL 必须**同时**生效：过期项优先于 LRU 被清理。

    只有 TTL 时，一次 20 万条的压测会在 24h 内把内存吃光；只有容量时，
    低频的长尾编号会一直占着位置。
    """
    clock = {"now": 0}
    store = IdempotencyStore(ttl_ms=100, capacity=2, clock=lambda: clock["now"])
    store.reserve("EVT1", payload_digest({"k": 1}))
    store.finish("EVT1", {"event_id": "EVT1"})
    clock["now"] = 1000     # EVT1 已过期
    store.reserve("EVT2", payload_digest({"k": 2}))
    assert store.check("EVT1", payload_digest({"k": 1}))[0] == "miss"
    assert store.check("EVT2", payload_digest({"k": 2}))[0] == "inflight"
    assert store.size() == 1


async def test_inflight_follower_waits_instead_of_recomputing():
    """single-flight 的单元级验证：跟随者不会自己再算一遍（BR-03-14 的延伸）。"""
    store = IdempotencyStore(clock=now_ms)
    digest = payload_digest({"x": 1})
    assert store.reserve("EVT1", digest)[0] == "reserved"

    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    assert store.attach_waiter("EVT1", waiter) is True
    assert store.check("EVT1", digest)[0] == "inflight"

    body = {"event_id": "EVT1", "decision": "review"}
    store.finish("EVT1", body)
    assert await asyncio.wait_for(waiter, timeout=1) is body
    assert store.check("EVT1", digest)[0] == "hit"


async def test_attach_waiter_fails_when_entry_is_already_ready():
    """已完成/不存在的项不能被挂等待者：否则跟随者会永远等一个不会到来的结果。"""
    store = IdempotencyStore(clock=now_ms)
    digest = payload_digest({"x": 1})
    loop = asyncio.get_running_loop()
    assert store.attach_waiter("EVT_NONE", loop.create_future()) is False
    store.reserve("EVT1", digest)
    store.finish("EVT1", {"event_id": "EVT1"})
    assert store.attach_waiter("EVT1", loop.create_future()) is False


async def test_finish_wakes_waiters_and_updates_the_canonical_body():
    """`finish` 必须把响应体写进缓存项并唤醒等待者（BR-03-14 的落点）。"""
    store = IdempotencyStore(clock=now_ms)
    digest = payload_digest({"x": 1})
    store.reserve("EVT1", digest)
    body = {"event_id": "EVT1", "decision": "review", "persisted": False}
    store.finish("EVT1", body, doc={"_id": "EVT1"})
    state, cached = store.check("EVT1", digest)
    assert state == "hit"
    assert cached is body, "缓存里应是同一个对象（落库完成后就地改 persisted）"
    assert store.take_doc("EVT1") == {"_id": "EVT1"}
