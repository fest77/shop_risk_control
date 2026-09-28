# -*- coding: utf-8 -*-
"""进程内 SSE 广播总线（模块 11 §3.7）：订阅者队列、环形缓冲、心跳与背压。

## 为什么是"进程内"而不是 Redis / Kafka

演示与验收都在单进程 uvicorn（概要设计 AD-04 的部署形态）下进行，多引入一个
中间件只会增加"演示当天起不来"的风险。本模块把这个局限**写在类型上**：
`seq` 只在进程内单调，进程重启后从 1 重新开始，因此 `Last-Event-ID` 补偿
只对"同一次进程生命周期内"的断线有效（重启后客户端带旧 id 会被判定为
"缺口超出缓冲"，收到 `gap` 帧后转为纯实时推送——这恰好是正确的兜底行为）。

## 三条硬约束

1. **绝不阻塞发布方**（MET-5005 / V-11-15）：`publish()` 内部**没有任何 await**，
   只做 `put_nowait`。队列满时丢弃**最旧**事件（保留最新的才有观测价值：
   页面看的是"刚刚发生了什么"），并把丢弃条数累加到该连接后续事件的
   `dropped_cnt` 上；决策链路永远不会因为"没人看大盘"而变慢。
2. **每订阅者独立队列**：慢客户端不影响快客户端。若共用一个队列，一个卡住的
   浏览器就能拖垮所有人的实时流。
3. **缺口要说话**：补偿不齐时发 `gap` 帧而不是"假装连续"。`seq` 不连续却
   不报缺口，会让看盘的人以为中间没有风险事件——这是数据侧的 fail-closed
   （§5：宁可显示"存在缺口"，也不得用连续的外观冒充完整数据）。
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from app.engine.metric_agg import DECISION_TO_LEVEL, scene_of
from app.errors import MET_CODES, AppError
from app.logging import get_logger
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.metric.stream")

# §3.7：订阅上限 50、队列 500、环形缓冲 200、心跳 15s、重连 3s
MAX_SUBSCRIBERS = 50
SUBSCRIBER_QUEUE_SIZE = 500
RING_BUFFER_SIZE = 200
HEARTBEAT_SEC = 15.0
RETRY_MS = 3000

EVENT_RISK = "risk_event"
EVENT_HEARTBEAT = "heartbeat"
EVENT_GAP = "gap"
EVENT_SHUTDOWN = "shutdown"

CONTROL_SHUTDOWN = "shutdown"


def sse_frame(
    data: Optional[dict] = None,
    *,
    event: Optional[str] = None,
    event_id: Optional[int] = None,
    retry: Optional[int] = None,
) -> str:
    """按 SSE 规范拼一帧（事件之间以**空行**分隔）。

    独立的纯函数：帧格式是本模块与模块 02 的接口契约，单测可以直接断言字符串，
    不必去驱动一个真实的流。
    """
    lines: list[str] = []
    if retry is not None:
        lines.append(f"retry: {retry}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    if data is not None:
        # ensure_ascii=False + 紧凑分隔符：帧会原样进浏览器，中文别被转成 \uXXXX
        lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


@dataclass(frozen=True)
class _Control:
    """控制帧（不参与 `Last-Event-ID` 补偿，也不消耗 `seq`）。"""

    kind: str


@dataclass
class Subscriber:
    """一个 SSE 订阅者。

    `queue` 与 `dropped` 公开：单测可以直接操作队列来构造"慢消费者"场景，
    不必真的sleep 到队列满。
    """

    sid: int
    queue: asyncio.Queue = field(repr=False)
    scene: Optional[str] = None
    level: Optional[str] = None
    dropped: int = 0

    def matches(self, payload: dict) -> bool:
        """服务端过滤（§3.7 的 `scene` / `level` 参数）。

        `scene` 由**事件类型**推导（复用 `metric_agg.scene_of`），`level` 优先取
        决策文档里的 `risk_level`，取不到时按 BR-05-17 的 1:1 映射从 `decision` 反推——
        这样"过滤口径"与"统计口径"永远是同一套，不会出现"榜上有、流里没有"。
        """
        if self.scene:
            if scene_of(str(payload.get("event_type") or "")) != self.scene:
                return False
        if self.level:
            level = payload.get("risk_level") or DECISION_TO_LEVEL.get(
                str(payload.get("decision") or "")
            )
            if level != self.level:
                return False
        return True


class MetricStreamBus:
    """进程内发布订阅总线（单例由 `get_bus()` 提供）。"""

    def __init__(
        self,
        *,
        max_subscribers: int = MAX_SUBSCRIBERS,
        queue_size: int = SUBSCRIBER_QUEUE_SIZE,
        buffer_size: int = RING_BUFFER_SIZE,
        heartbeat_sec: float = HEARTBEAT_SEC,
        retry_ms: int = RETRY_MS,
    ):
        # 参数全部可注入：单测需要"上限 1 个订阅者""心跳 0.05s""缓冲 3 条"这类
        # 极端配置，否则验证 MET-5004 与 gap 帧都得等 15 秒或灌 201 条事件
        self.max_subscribers = max_subscribers
        self.queue_size = queue_size
        self.heartbeat_sec = heartbeat_sec
        self.retry_ms = retry_ms
        self._seq = 0
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        self._subs: dict[int, Subscriber] = {}
        self._next_sid = 1
        self.stats: dict[str, int] = {
            "published": 0, "dropped": 0, "rejected": 0, "peak_subscribers": 0,
        }

    # ---------------- 状态查询（供 /health 与测试） ----------------
    @property
    def seq(self) -> int:
        """已发布的最大 `seq`（单调递增，从 1 起）。"""
        return self._seq

    @property
    def buffer_len(self) -> int:
        """环形缓冲当前条数。"""
        return len(self._buffer)

    def subscriber_count(self) -> int:
        """当前订阅者数量（心跳帧里的 `subscribers` 也用它）。"""
        return len(self._subs)

    # ---------------- 订阅 ----------------
    def subscribe(self, *, scene: Optional[str] = None, level: Optional[str] = None) -> Subscriber:
        """登记一个订阅者；超出上限抛 `MET-5004`（503，不踢既有连接）。

        **同步方法**：内部只操作进程内字典与队列（无 IO、无 await）。做成同步的
        直接好处是"订阅名额"在 HTTP 响应开始之前就能判定——若在流生成器里才
        校验，客户端拿到的会是一个已经 200 开头的响应再突然断流，错误码无处安放。
        """
        if len(self._subs) >= self.max_subscribers:
            self.stats["rejected"] += 1
            log.warning("[MET-5004] SSE 订阅数已达上限 %d，拒绝新连接", self.max_subscribers)
            raise AppError("MET-5004", MET_CODES["MET-5004"][1],
                           MET_CODES["MET-5004"][0] or 503,
                           {"max_subscribers": self.max_subscribers})
        sub = Subscriber(sid=self._next_sid, queue=asyncio.Queue(maxsize=self.queue_size),
                         scene=scene, level=level)
        self._next_sid += 1
        self._subs[sub.sid] = sub
        self.stats["peak_subscribers"] = max(self.stats["peak_subscribers"], len(self._subs))
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        """注销订阅者（幂等：重复注销不报错，流被重复关闭时不会二次抛异常）。"""
        self._subs.pop(sub.sid, None)

    # ---------------- 发布 ----------------
    async def publish(self, event: dict) -> int:
        """广播一条风险事件，返回它的 `seq`。

        **函数体内没有 await**，因此它不可能阻塞调用方（决策链路只在内存里
        推一次就返回）。保留 `async` 只是为了与 §3.7 约定的调用形态一致。
        """
        self._seq += 1
        payload = dict(event)
        payload["seq"] = self._seq
        payload.setdefault("ts", now_ms())
        self._buffer.append(payload)
        self.stats["published"] += 1
        for sub in list(self._subs.values()):
            if sub.matches(payload):
                self._offer(sub, payload)
        return self._seq

    def _offer(self, sub: Subscriber, item: Any) -> None:
        """向订阅者队列投递一项；满则丢最旧（MET-5005 的背压策略）。"""
        try:
            sub.queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        # 丢最旧：读盘的人关心"刚刚发生了什么"，丢新的等于让画面永久停在过去
        try:
            sub.queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - 满与空不可能同时成立
            pass
        sub.dropped += 1
        self.stats["dropped"] += 1
        try:
            sub.queue.put_nowait(item)
        except asyncio.QueueFull:  # pragma: no cover - 已腾出一格，理论不可达
            log.error("[MET-5005] 订阅者 %d 投递失败（丢旧后仍满）", sub.sid)
            return
        log.warning("[MET-5005] 订阅者 %d 队列溢出，已丢弃最旧事件（累计 %d 条）",
                    sub.sid, sub.dropped)

    async def shutdown(self) -> None:
        """优雅停机：向所有连接广播 `shutdown` 帧（§3.7）。

        控制帧同样要"占位成功"：队列已满时也腾一格出来，否则停机通知会排在被
        丢弃的那一端——而它的意义恰恰是"别再等了"。
        """
        subs = list(self._subs.values())
        for sub in subs:
            self._offer(sub, _Control(CONTROL_SHUTDOWN))
        if subs:
            log.info("SSE 广播停机通知：%d 个连接", len(subs))

    def reset(self) -> None:
        """清空订阅者、缓冲与 `seq`（测试逐用例复位；停机后重启进程内状态同理）。"""
        self._subs.clear()
        self._buffer.clear()
        self._seq = 0
        self.stats.update({"published": 0, "dropped": 0, "rejected": 0, "peak_subscribers": 0})

    # ---------------- 帧生成 ----------------
    def _event_frame(self, payload: dict, dropped: int) -> str:
        """风险事件帧：`id` = `seq`，`dropped_cnt` = **本连接**的累计丢弃条数。"""
        return sse_frame(
            {**payload, "dropped_cnt": dropped},
            event=EVENT_RISK, event_id=int(payload["seq"]),
        )

    def _heartbeat_frame(self) -> str:
        """心跳帧（无 `id`）：不带 `id` 是刻意的——它不该覆盖 `Last-Event-ID`，
        否则断线重连时补偿起点会变成"最后一次心跳"，中间的事件全部被跳过。"""
        return sse_frame({"ts": now_ms(), "subscribers": len(self._subs)},
                         event=EVENT_HEARTBEAT)

    @staticmethod
    def _gap_frame() -> str:
        return sse_frame({"missed": True, "reason": "buffer_exhausted"}, event=EVENT_GAP)

    @staticmethod
    def _shutdown_frame() -> str:
        return sse_frame({"reason": "server_shutdown"}, event=EVENT_SHUTDOWN)

    def _gap_needed(self, last_event_id: int) -> bool:
        """`Last-Event-ID` 之后的事件是否已超出环形缓冲。"""
        first_missed = last_event_id + 1
        if first_missed > self._seq:
            return False  # 客户端比服务端还新（例如服务端刚重启），没有缺口
        if not self._buffer:
            return True   # 没有任何可补发的事件，但 seq 说明确实发生过
        return int(self._buffer[0]["seq"]) > first_missed

    async def frames(
        self, sub: Subscriber, last_event_id: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """该订阅者的 SSE 帧流（首帧 `retry:`，随后补偿 + 实时 + 心跳）。

        生成器退出（客户端断开 / 抛错 / 收到停机控制帧）时**一定**注销订阅者：
        漏掉这一步，连接数上限会被"已经断开的连接"慢慢吃满，表现为"用一会儿就
        再也连不上实时流"。
        """
        try:
            # 首帧只有 retry：浏览器据此在断线后 3s 重连（§3.7）
            yield sse_frame(retry=self.retry_ms)
            if last_event_id is not None:
                if self._gap_needed(last_event_id):
                    # 缺口超出缓冲：发一次 gap 后转为纯实时推送（不补发半截，
                    # 半截数据会让"断点"看起来比实际更晚，误导排查）
                    yield self._gap_frame()
                else:
                    for payload in list(self._buffer):
                        if int(payload["seq"]) > last_event_id:
                            yield self._event_frame(payload, sub.dropped)

            while True:
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=self.heartbeat_sec)
                except asyncio.TimeoutError:
                    # 无事件也发心跳：代理/网关的空闲断链会表现为"页面数据不再更新"
                    # 但连接看着还在，没有心跳时这类问题极难定位
                    yield self._heartbeat_frame()
                    continue
                if isinstance(item, _Control):
                    if item.kind == CONTROL_SHUTDOWN:
                        yield self._shutdown_frame()
                        return
                    continue
                yield self._event_frame(item, sub.dropped)
        finally:
            self.unsubscribe(sub)


# ============================================================
# 进程内单例
# ============================================================
_BUS = MetricStreamBus()


def get_bus() -> MetricStreamBus:
    """取进程内总线单例（发布方与 SSE 端点共用同一个实例）。"""
    return _BUS
