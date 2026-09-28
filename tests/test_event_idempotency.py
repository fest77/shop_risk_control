# -*- coding: utf-8 -*-
"""模块 03 幂等验收测试（V-03-04 / V-03-05，BR-03-09 ~ 14，决策 D6）。

三条要证明的事：

1. **同编号同载荷连发两次不重复计分**（V-03-04）——判据不只是 `duplicate=true`，
   而是"下游只被调用了一次"。否则"返回了首次响应"可能只是恰好算出了同样的
   结果，实现其实重复算了。
2. **同编号不同载荷 → 409 `EVT-4009`**（V-03-05）——不能静默返回首次结果。
3. **并发重复提交不穿透**（BR-03-14）——这是 `asyncio` 下最容易写错的一条：
   只在"返回前写缓存"仍然会漏，必须配合在途占位（single-flight）。

缓存的**纯逻辑**断言（TTL / LRU / 载荷哈希 / 占位释放）在
`tests/test_event_gateway.py`：本文件用的是 async autouse 夹具，混入 sync 用例
会触发 pytest 的夹具终结器断言（详见 `tests/event_testlib.py` 的说明）。
"""
from __future__ import annotations

import asyncio

import pytest

from app import db
from app.constants import COLL_RISK_EVENTS
from app.core.event_simulator import get_simulator
from app.protocols import configure_components
from app.schemas.event_schema import DECISION_FIELDS
from app.services import event_service
from tests.conftest import WRITER
from tests.event_testlib import (
    FIXED_DECISION,
    CountingDecisionProvider,
    component_reset,
    coupon_payload,
    install_fakes,
    login_payload,
    reset_runtime_state,
    unique_event_id,
)
from tests.test_event_gateway import post_event

pytestmark = pytest.mark.anyio

EVENTS_URL = "/api/v1/events"


@pytest.fixture(autouse=True)
async def event_state():
    """逐用例复位进程内状态（理由与实现见 `tests/event_testlib.py`）。"""
    component_reset()
    reset_runtime_state()
    await db.get_db()[COLL_RISK_EVENTS].delete_many({})
    yield
    await event_service.flush()
    sim = get_simulator()
    if sim.running:
        await sim.stop()
    reset_runtime_state()


# ============================================================ V-03-04 同编号同载荷
async def test_same_id_same_payload_is_duplicate_and_not_recounted(client):
    """V-03-04：同编号同载荷连发 2 次 → `duplicate=true`、决策块逐字段相等、
    **下游只被调用一次**（不重复计分、不重复落库）。"""
    feature, decision = install_fakes()
    event_id = unique_event_id()
    payload = coupon_payload(event_id=event_id)

    first = await post_event(client, payload)
    assert first.status_code == 200, first.text
    second = await post_event(client, payload)
    assert second.status_code == 200, second.text

    d1, d2 = first.json()["data"], second.json()["data"]
    assert d1["duplicate"] is False
    assert d2["duplicate"] is True, "同编号同载荷的第二次必须标记为幂等命中"
    assert d1["event_id"] == d2["event_id"] == event_id

    # 决策块逐字段相等（这是 V-03-04 的字面判据）
    for field in DECISION_FIELDS:
        assert d1[field] == d2[field], f"决策块字段 {field} 两次返回不一致"
    assert d1["decision"] == d2["decision"] == "review"
    assert d1["rule_score"] == d2["rule_score"] == 55

    # 真正的判据：下游只被调用了一次
    assert feature.calls == 1, f"特征计算被重复执行 {feature.calls} 次"
    assert decision.calls == 1, f"规则决策被重复执行 {decision.calls} 次"

    # 库里只有一条（BR-03-11：不重复落库）
    await event_service.flush()
    assert await db.get_db()[COLL_RISK_EVENTS].count_documents({"_id": event_id}) == 1


async def test_duplicate_replay_keeps_the_original_decision_even_if_rules_change(
    client,
):
    """**幂等的语义是"重放首次响应"，不是"重算一次"**。

    把 05 换成返回不同分值的实现后再重发同编号：响应必须仍是首次那一份。
    若实现改成"重算"，这条会失败——而那种实现会让同一个编号在库里对应两个
    不同结论，审计上不可接受。
    """
    install_fakes()
    event_id = unique_event_id()
    payload = coupon_payload(event_id=event_id)
    first = await post_event(client, payload)
    assert first.json()["data"]["rule_score"] == 55

    changed = CountingDecisionProvider({**FIXED_DECISION, "rule_score": 99,
                                        "final_score": 99}, delay=0)
    configure_components(decision_provider=changed)
    second = await post_event(client, payload)
    assert second.status_code == 200
    data = second.json()["data"]
    assert data["duplicate"] is True
    assert data["rule_score"] == 55, "幂等命中必须重放首次响应，而不是重新决策"
    assert changed.calls == 0, "幂等命中不得再调用 05"


# ============================================================ V-03-05 同编号不同载荷
async def test_same_id_different_payload_returns_409(client):
    """V-03-05 / BR-03-12：改 `amount` 后重发同编号 → 409 `EVT-4009`。"""
    install_fakes()
    event_id = unique_event_id()
    ok = await post_event(client, coupon_payload(event_id=event_id, amount=2000))
    assert ok.status_code == 200

    changed = coupon_payload(event_id=event_id, amount=9900)
    changed["scene_extra"]["face_value"] = 9900
    r = await post_event(client, changed)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "EVT-4009"
    assert event_id in str(body)


async def test_conflict_does_not_overwrite_the_first_result(client):
    """冲突之后首次响应仍可被重放（冲突不能把缓存项破坏掉）。"""
    install_fakes()
    event_id = unique_event_id()
    first = await post_event(client, coupon_payload(event_id=event_id, amount=2000))
    assert first.status_code == 200

    changed = coupon_payload(event_id=event_id, amount=9900)
    changed["scene_extra"]["face_value"] = 9900
    assert (await post_event(client, changed)).status_code == 409

    again = await post_event(client, coupon_payload(event_id=event_id, amount=2000))
    assert again.status_code == 200
    assert again.json()["data"]["duplicate"] is True
    assert again.json()["data"]["rule_score"] == 55


async def test_payload_key_order_does_not_cause_a_false_conflict(client):
    """载荷哈希对**键顺序不敏感**：换个字段顺序重发不该被误判成编号复用。

    否则调用方（或另一种语言的序列化实现）按自己的顺序重排字段，就会稳定地
    收到 409，表现为"幂等只对同一种序列化方式有效"。
    """
    install_fakes()
    event_id = unique_event_id()
    assert (await post_event(client, coupon_payload(event_id=event_id))).status_code == 200

    full = coupon_payload(event_id=event_id)
    reordered = {k: full[k] for k in
                 ("scene_extra", "amount", "ip", "device_id", "user_id",
                  "event_type", "event_id")}
    r = await post_event(client, reordered)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["duplicate"] is True


# ============================================================ BR-03-14 并发穿透
async def test_concurrent_same_id_runs_the_decision_only_once(client):
    """BR-03-14 / V-03-04 的并发版：`asyncio.gather` 发 5 次同编号同载荷。

    断言"只应有一次真实决策"——用 **05 的调用次数**判定，而不是用响应内容
    （内容相同可能是算了 5 次恰好结果一致）。
    """
    feature, decision = install_fakes()
    event_id = unique_event_id()
    payload = coupon_payload(event_id=event_id)

    responses = await asyncio.gather(*[post_event(client, payload) for _ in range(5)])
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]

    datas = [r.json()["data"] for r in responses]
    assert all(d["event_id"] == event_id for d in datas)
    assert decision.calls == 1, f"并发重复提交穿透了幂等缓存：05 被调用 {decision.calls} 次"
    assert feature.calls == 1, f"04 被调用 {feature.calls} 次"

    # 恰好一次 `duplicate=false`（首领），其余全部是命中
    assert sum(1 for d in datas if d["duplicate"] is False) == 1
    assert sum(1 for d in datas if d["duplicate"] is True) == 4

    # 落库也只有一条
    await event_service.flush()
    assert await db.get_db()[COLL_RISK_EVENTS].count_documents({"_id": event_id}) == 1


async def test_concurrent_same_id_yields_one_decision_block(client):
    """并发下所有响应必须携带**同一份**决策块（不能各算一份）。"""
    install_fakes()
    event_id = unique_event_id()
    payload = login_payload(event_id=event_id)
    responses = await asyncio.gather(*[post_event(client, payload) for _ in range(4)])
    blocks = [{f: r.json()["data"][f] for f in DECISION_FIELDS} for r in responses]
    assert all(b == blocks[0] for b in blocks), "并发响应携带了不同的决策块"


# ============================================================ 校验失败不进缓存
async def test_validation_failure_is_not_cached(client):
    """校验失败的请求**不得**占用幂等编号。

    否则调用方修正报文后重发会拿到 409（"编号已存在且内容不同"），而正确行为
    是"那次提交根本没被受理"。
    """
    install_fakes()
    event_id = unique_event_id()
    bad = coupon_payload(event_id=event_id)
    del bad["device_id"]
    assert (await post_event(client, bad)).status_code == 422

    good = coupon_payload(event_id=event_id)
    second = await post_event(client, good)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["duplicate"] is False


async def test_no_event_id_means_no_idempotency(client):
    """未传编号时每次都是新事件（服务端生成新编号），不存在"幂等命中"。"""
    install_fakes()
    payload = coupon_payload()
    r1 = await post_event(client, payload, headers=WRITER)
    r2 = await post_event(client, payload, headers=WRITER)
    assert r1.json()["data"]["duplicate"] is False
    assert r2.json()["data"]["duplicate"] is False
    assert r1.json()["data"]["event_id"] != r2.json()["data"]["event_id"]
