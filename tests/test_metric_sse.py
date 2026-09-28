# -*- coding: utf-8 -*-
"""SSE 实时事件流验收（§3.7）：心跳、`retry`、`Last-Event-ID` 补偿、背压与订阅上限。

**为什么 HTTP 层的用例要"手动驱动"**：httpx 的 `ASGITransport`（0.28）会把响应体
一次性缓冲完再返回，因此 `client.stream(...)` 对**无限** SSE 流会一直挂住。
这里改成"后台任务发起请求 + 测试侧灌事件 + 广播停机帧收尾"，既真实走了
鉴权/中间件/响应头，又不会把测试挂死。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import metric_stream_bus
from app.core.metric_stream_bus import (
    EVENT_GAP,
    EVENT_HEARTBEAT,
    EVENT_RISK,
    EVENT_SHUTDOWN,
    MetricStreamBus,
)
from app.errors import AppError
from app.utils.timeutil import now_ms

pytestmark = pytest.mark.anyio

BASE = "/api/v1/metrics"
NOW = 1758600271208


def _frame_data(frame: str) -> str:
    for line in frame.splitlines():
        if line.startswith("data: "):
            return line[6:]
    return ""


def _split_frames(text: str) -> list[str]:
    return [f for f in text.split("\n\n") if f.strip()]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """等某个条件成立（订阅建立、任务开始），避免用固定 sleep 造成的偶发抖动。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("等待条件超时")


def _event(seq_hint: str = "E1") -> dict:
    return {"event_id": seq_hint, "event_type": "coupon_receive", "user_id": "U1",
            "ts": NOW, "final_score": 86, "risk_level": "high", "decision": "reject"}


# ============================================================ 首帧 / 心跳
async def test_first_frame_is_retry_and_heartbeat_keeps_connection_alive():
    """V-11-13 / §3.7：首帧含 `retry: 3000`；无事件时也发心跳帧。"""
    bus = MetricStreamBus(heartbeat_sec=0.05)
    sub = bus.subscribe()
    frames = bus.frames(sub)
    try:
        assert await anext(frames) == "retry: 3000\n\n"
        heartbeat = await asyncio.wait_for(anext(frames), timeout=1)
        assert f"event: {EVENT_HEARTBEAT}" in heartbeat
        assert '"subscribers":1' in _frame_data(heartbeat)
        # 心跳不带 id：否则会覆盖浏览器的 Last-Event-ID，重连时中间事件会被跳过
        assert "id:" not in heartbeat
    finally:
        await frames.aclose()
    assert bus.subscriber_count() == 0, "生成器结束时必须注销订阅者"


async def test_event_frame_carries_seq_and_dropped_cnt():
    """§3.7：`id` = 单调 `seq`，`dropped_cnt` 为本连接累计丢弃数（正常时为 0）。"""
    bus = MetricStreamBus(heartbeat_sec=5)
    sub = bus.subscribe()
    frames = bus.frames(sub)
    try:
        await anext(frames)
        seq = await bus.publish(_event())
        assert seq == 1
        frame = await asyncio.wait_for(anext(frames), timeout=1)
        assert f"event: {EVENT_RISK}" in frame
        assert "id: 1" in frame
        assert '"seq":1' in _frame_data(frame)
        assert '"dropped_cnt":0' in _frame_data(frame)
        assert '"user_id":"U1"' in _frame_data(frame)
    finally:
        await frames.aclose()


async def test_scene_and_level_filters_are_applied_server_side():
    """§3.7：`scene`/`level` 由服务端过滤（口径与统计一致）。"""
    bus = MetricStreamBus(heartbeat_sec=5)
    coupon_high = bus.subscribe(scene="coupon", level="high")
    login_any = bus.subscribe(scene="login")
    await bus.publish(_event())                       # coupon_receive + high
    await bus.publish({**_event("E2"), "event_type": "login", "risk_level": "low",
                       "decision": "pass"})
    assert coupon_high.queue.qsize() == 1
    assert login_any.queue.qsize() == 1
    bus.unsubscribe(coupon_high)
    bus.unsubscribe(login_any)


# ============================================================ Last-Event-ID 补偿
async def test_last_event_id_replays_missed_events_in_order():
    """V-11-14 / §3.7：重连带 `Last-Event-ID`，补发其后的事件且 `seq` 连续。"""
    bus = MetricStreamBus(heartbeat_sec=5)
    for i in range(1, 6):
        await bus.publish(_event(f"E{i}"))

    sub = bus.subscribe()
    frames = bus.frames(sub, last_event_id=3)
    try:
        assert await anext(frames) == "retry: 3000\n\n"
        replayed = [await anext(frames), await anext(frames)]
        assert ["id: 4" in replayed[0], "id: 5" in replayed[1]] == [True, True]
        assert [f'"seq":{i}' in _frame_data(replayed[i - 4]) for i in (4, 5)] == [True, True]
    finally:
        await frames.aclose()


async def test_gap_frame_when_gap_exceeds_ring_buffer():
    """§3.7：缺口超出环形缓冲（200 条）时发一次 `gap` 帧并转为纯实时推送。"""
    bus = MetricStreamBus(heartbeat_sec=5, buffer_size=3)
    for i in range(1, 6):
        await bus.publish(_event(f"E{i}"))            # 缓冲只留 seq 3/4/5

    sub = bus.subscribe()
    frames = bus.frames(sub, last_event_id=1)          # 缺 seq 2，已被挤出缓冲
    try:
        assert await anext(frames) == "retry: 3000\n\n"
        gap = await anext(frames)
        assert f"event: {EVENT_GAP}" in gap
        data = _frame_data(gap)
        assert '"missed":true' in data and "buffer_exhausted" in data
        # 不补发"半截"：下一条帧应该是新事件（seq 6），而不是 seq 3
        await bus.publish(_event("E6"))
        assert "id: 6" in await asyncio.wait_for(anext(frames), timeout=1)
    finally:
        await frames.aclose()


async def test_no_gap_when_client_is_up_to_date():
    """新鲜连接（不带 `Last-Event-ID`）不应收到 `gap`：它没有"缺口"可言。"""
    bus = MetricStreamBus(heartbeat_sec=0.05)
    await bus.publish(_event())
    sub = bus.subscribe()
    frames = bus.frames(sub)
    try:
        assert await anext(frames) == "retry: 3000\n\n"
        frame = await asyncio.wait_for(anext(frames), timeout=1)
        assert f"event: {EVENT_HEARTBEAT}" in frame     # 没有补发、没有 gap
    finally:
        await frames.aclose()


# ============================================================ 背压（MET-5005）
async def test_backpressure_drops_oldest_and_accumulates_dropped_cnt():
    """V-11-15 / MET-5005：队列满时丢**最旧**，并把丢弃数累加到后续事件上。"""
    bus = MetricStreamBus(heartbeat_sec=5, queue_size=5)
    sub = bus.subscribe()
    for i in range(1, 9):
        await bus.publish(_event(f"E{i}"))

    assert sub.queue.qsize() == 5
    assert sub.dropped == 3
    assert bus.stats["dropped"] == 3
    frames = bus.frames(sub)
    try:
        await anext(frames)
        first = await asyncio.wait_for(anext(frames), timeout=1)
        # 队列里现存的是 seq 4~8：最旧的三条（1/2/3）被丢弃以保留最新事件
        assert '"seq":4' in _frame_data(first)
        assert '"dropped_cnt":3' in _frame_data(first)
    finally:
        await frames.aclose()


async def test_publish_never_blocks_even_without_consumer():
    """V-11-15：发布方永不阻塞——灌 1000 条，队列长度停在上限，丢最旧继续推进。"""
    bus = MetricStreamBus(heartbeat_sec=5, queue_size=500)
    sub = bus.subscribe()
    for i in range(1000):
        await bus.publish(_event(f"E{i}"))

    assert bus.seq == 1000
    assert sub.queue.qsize() == 500
    assert sub.dropped == 500
    assert sub.queue.get_nowait()["seq"] == 501


# ============================================================ 订阅上限与停机
async def test_subscriber_limit_raises_met_5004():
    """MET-5004：订阅数超上限 -> 503 拒绝新连接（**不踢**既有连接）。"""
    bus = MetricStreamBus(max_subscribers=1)
    first = bus.subscribe()
    with pytest.raises(AppError) as err:
        bus.subscribe()
    assert err.value.code == "MET-5004" and err.value.http_status == 503
    assert bus.subscriber_count() == 1
    assert bus.stats["rejected"] == 1
    bus.unsubscribe(first)
    assert bus.subscribe() is not None


async def test_shutdown_broadcasts_shutdown_frame_and_closes_streams():
    """§3.7：优雅停机前广播 `shutdown` 帧，随后流结束并注销订阅者。"""
    bus = MetricStreamBus(heartbeat_sec=5)
    sub = bus.subscribe()
    frames = bus.frames(sub)
    assert await anext(frames) == "retry: 3000\n\n"
    await bus.shutdown()
    shutdown = await asyncio.wait_for(anext(frames), timeout=1)
    assert f"event: {EVENT_SHUTDOWN}" in shutdown
    assert "server_shutdown" in _frame_data(shutdown)
    with pytest.raises(StopAsyncIteration):
        await anext(frames)
    assert bus.subscriber_count() == 0


# ============================================================ HTTP 层
@pytest.fixture
def test_bus(monkeypatch) -> MetricStreamBus:
    """把进程内总线换成用例专属实例（心跳调快、上限可调，且不污染其它用例）。"""
    bus = MetricStreamBus(heartbeat_sec=0.05)
    monkeypatch.setattr(metric_stream_bus, "_BUS", bus)
    return bus


async def _drive_stream(client, bus: MetricStreamBus, url: str, **kwargs):
    """发起 SSE 请求，等订阅建立后返回"请求任务"，由调用方决定何时收尾。"""
    task = asyncio.create_task(client.get(url, **kwargs))
    await _wait_until(lambda: bus.subscriber_count() == 1)
    return task


async def test_stream_endpoint_headers_and_frames(client, test_bus):
    """§3.7：响应头与帧格式（`retry` / `risk_event` / `id`）。"""
    task = await _drive_stream(client, test_bus, f"{BASE}/stream",
                               headers={"X-Test-Role": "reviewer"})
    await test_bus.publish(_event())
    await test_bus.shutdown()
    r = await asyncio.wait_for(task, timeout=5)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    frames = _split_frames(r.text)
    assert frames[0] == "retry: 3000"
    kinds = [next((ln.split(": ", 1)[1] for ln in f.splitlines()
                   if ln.startswith("event: ")), "") for f in frames]
    assert EVENT_RISK in kinds and EVENT_SHUTDOWN in kinds
    risk_frame = frames[kinds.index(EVENT_RISK)]
    assert "id: 1" in risk_frame and '"dropped_cnt":0' in _frame_data(risk_frame)
    # 长连接结束后订阅者必须被注销（否则上限会被"已断开的连接"吃满）
    assert test_bus.subscriber_count() == 0


async def test_stream_accepts_token_query_parameter(client, test_bus, bearer):
    """N-11-10：原生 `EventSource` 无法设置请求头，故该端点额外接受 `?token=`。"""
    token = (await bearer("reviewer"))["Authorization"].split(" ", 1)[1]
    task = asyncio.create_task(client.get(f"{BASE}/stream", params={"token": token}))
    await _wait_until(lambda: test_bus.subscriber_count() == 1)
    await test_bus.shutdown()
    r = await asyncio.wait_for(task, timeout=5)
    assert r.status_code == 200
    assert r.text.startswith("retry: 3000")


async def test_stream_requires_login(client, test_bus):
    """未登录 -> 401 统一响应包（错误发生在流开始之前，因此仍能套信封）。"""
    r = await client.get(f"{BASE}/stream")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "AUTH-4002" and body["ok"] is False
    assert test_bus.subscriber_count() == 0


async def test_stream_rejects_when_subscriber_limit_reached(client, monkeypatch):
    """MET-5004：连接数已满 -> 503 统一响应包（**不踢**既有连接）。"""
    bus = MetricStreamBus(max_subscribers=1, heartbeat_sec=0.05)
    monkeypatch.setattr(metric_stream_bus, "_BUS", bus)
    held = bus.subscribe()
    try:
        r = await client.get(f"{BASE}/stream", headers={"X-Test-Role": "admin"})
        assert r.status_code == 503
        body = r.json()
        assert body["code"] == "MET-5004" and body["ok"] is False
        assert bus.subscriber_count() == 1     # 既有连接没被踢掉
    finally:
        bus.unsubscribe(held)


async def test_stream_invalid_filter_returns_met_4004(client, test_bus):
    r = await client.get(f"{BASE}/stream", params={"scene": "nope"},
                         headers={"X-Test-Role": "reviewer"})
    assert r.status_code == 400
    assert r.json()["code"] == "MET-4004"


async def test_record_decision_publishes_to_stream(client, test_bus):
    """§3.7 的写入侧契约：决策落库成功后必须能在大盘实时流里看到。"""
    from app.services.metric_service import MetricService

    service = MetricService()
    await service.record_decision(
        {"event_id": f"EVT-{now_ms()}", "event_type": "order_pay", "user_id": "U9",
         "ts": NOW, "amount": 5000, "biz_no": "PAY-1"},
        {"decision": "reject", "risk_level": "high", "final_score": 91,
         "case_no": "CASE0923001"},
        [{"rule_code": "R1", "score": 40}],
    )
    payload = test_bus._buffer[-1]
    assert payload["seq"] == 1
    assert payload["event_type"] == "order_pay" and payload["decision"] == "reject"
    assert payload["hit_rule_count"] == 1 and payload["case_no"] == "CASE0923001"
    # 只推 user_id：手机号/IP/地址绝不进实时流（脱敏责任在 00/09）
    assert payload["user_id"] == "U9"
    assert not [k for k in payload if k in ("phone", "ip", "address")]
