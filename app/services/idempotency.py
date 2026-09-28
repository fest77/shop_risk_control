# -*- coding: utf-8 -*-
"""事件幂等内存缓存：`event_id -> 首次响应体`（BR-03-11 / 12 / 13 / 14，决策 D6）。

## 为什么是「返回响应之前写入 + 临界区内零 `await`」

BR-03-14 是本节的全部理由。`asyncio` 只在 `await` 处切换任务，因此**只要在
"查缓存 → 判定 → 登记占位"这一段里没有 `await`，它相对于其它协程就是原子的**。
若把写入放到返回响应之后（或中间插一个 `await`），两个并发的同编号请求会
双双"查不到"、双双跑完整条决策链路——重复计分、重复落库、`decision_hits`
翻倍（V-03-04 要防的正是这个）。

## 为什么需要「在途占位」（single-flight）

只做"返回前写入"还不够：真下决策链路要 `await` 若干次（取号、调 04、调 05），
这期间第二个同编号请求仍然查不到结果。因此这里在**进入链路之前**就先登记一个
不带响应体的占位项，后来者拿到占位项后等待首领（leader）的结果，复用同一份
响应体并标记 `duplicate=true`。这样"只应有一次真实决策"在并发下也成立。

## 为什么同时存载荷哈希

BR-03-12：同一编号承载不同载荷属于数据事故，必须报 `409 EVT-4009` 而不是
静默返回首次结果——否则调用方会以为自己的新事实被受理了，而系统里留下的是
另一件事的记录。因此命中时要比较 `sha256(规范化载荷)`。

## 已知局限（如实登记）

缓存是**进程内**的：多 worker 部署时各自持有一份，跨进程的同编号并发只能靠
`risk_events._id` 唯一索引兜底（BR-03-13，且那种路径不再返回决策块）。
TTL 到期后同理。这与 AD-02 的"进程内缓存 + 唯一索引兜底"设计一致。
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.logging import get_logger
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.event.idempotency")

# BR-03-11 / D6：TTL 24 小时、容量 20000、LRU 淘汰。
# 24h 是对齐 Stripe Idempotency-Key 的业界通行窗口（D6 的依据）。
DEFAULT_TTL_MS = 24 * 60 * 60 * 1000
DEFAULT_CAPACITY = 20_000


def payload_digest(payload: dict) -> str:
    """规范化载荷哈希（BR-03-12 的比较依据）。

    用 `sort_keys=True` + 紧凑分隔符，使"字段顺序不同但内容相同"的两次提交
    得到同一个哈希——否则调用方换个字段顺序重发就会被误判成 `EVT-4009`。
    `default=str` 兜住 datetime 等不可直接序列化的值，避免哈希计算本身抛异常
    把一次正常接入变成 500。
    """
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass
class Entry:
    """一个幂等项。`body is None` 表示首领（leader）仍在计算中。"""

    digest: str
    body: Optional[dict] = None
    created_ms: int = 0
    # 首领完成后写入的"已落库文档"，供异步落库任务在响应返回后使用
    doc: Optional[dict] = None
    # 跟随者等待首领结果的载体（由首领在完成时 set_result）
    waiters: list[Any] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.body is not None


class IdempotencyStore:
    """`event_id -> Entry` 的有界 TTL + LRU 缓存，带 single-flight 占位。

    时钟可注入（`clock`）：TTL 与 LRU 的行为必须能在测试里被确定性地推进，
    否则"24h 后过期"这条只能靠改系统时间验证，等于没有测试。
    """

    def __init__(
        self,
        ttl_ms: int = DEFAULT_TTL_MS,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.ttl_ms = int(ttl_ms)
        self.capacity = int(capacity)
        self._clock = clock
        # OrderedDict 的插入顺序即 LRU 顺序：命中时 move_to_end，淘汰时 popitem(False)
        self._items: "OrderedDict[str, Entry]" = OrderedDict()
        self.stats = {"hits": 0, "conflicts": 0, "waits": 0, "evicted": 0, "expired": 0}

    # ---------------- 内部 ----------------
    def _now(self) -> int:
        return int(self._clock())

    def _is_expired(self, entry: Entry, now: int) -> bool:
        return now - entry.created_ms > self.ttl_ms

    def prune(self, now: Optional[int] = None, *, reserve_slots: int = 0) -> int:
        """清掉过期项并按 LRU 淘汰到容量以内，返回移除条数。

        容量与 TTL 必须都生效：只有 TTL 时，一次 20 万条的压测会在 24h 内
        把内存吃光；只有容量时，低频的长尾编号会一直占着位置。

        `reserve_slots` 是"本次调用之后马上要插入的条数"。**必须预留**：
        若先按 `capacity` 修剪、再插入新项，缓存会稳定地保持在
        `capacity + 1` 条（每次多一条），容量上限就形同虚设。
        """
        moment = self._now() if now is None else int(now)
        removed = 0
        # 过期扫描：按插入顺序，遇到第一条未过期的即可停止（后插入的更不可能过期）
        for key in list(self._items.keys()):
            entry = self._items[key]
            if self._is_expired(entry, moment):
                del self._items[key]
                removed += 1
            else:
                break
        self.stats["expired"] += removed
        limit = max(0, self.capacity - max(0, reserve_slots))
        while len(self._items) > limit:
            self._items.popitem(last=False)
            self.stats["evicted"] += 1
            removed += 1
        return removed

    def size(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        """清空缓存（测试逐用例复位用）。

        生产**没有**调用点：清空等于放弃幂等窗口，那会让 24h 内的重发
        变成新的一次决策与落库。
        """
        self._items.clear()

    # ---------------- 对外：查 / 登记 / 完成 ----------------
    def check(self, event_id: str, digest: str) -> tuple[str, Optional[dict]]:
        """查幂等缓存。返回 `(状态, 响应体)`。

        状态取值：
        - `miss`：无有效项（不存在 / 已过期），调用方可以登记为首领
        - `hit`：同编号同载荷且首领已完成 → 返回首次响应体
        - `inflight`：同编号同载荷但首领仍在算 → 返回 `None`，调用方应等待
        - `conflict`：同编号不同载荷（BR-03-12 → `EVT-4009`）

        **本方法与 `reserve` 一样不含 `await`**，因此"查—判—登记"这一串
        相对于其它协程是原子的（BR-03-14）。
        """
        now = self._now()
        entry = self._items.get(event_id)
        if entry is None:
            return "miss", None
        if self._is_expired(entry, now):
            del self._items[event_id]
            self.stats["expired"] += 1
            return "miss", None
        if entry.digest != digest:
            self.stats["conflicts"] += 1
            return "conflict", None
        self._items.move_to_end(event_id)     # LRU：命中即刷新
        if entry.ready:
            self.stats["hits"] += 1
            return "hit", entry.body
        self.stats["waits"] += 1
        return "inflight", None

    def reserve(self, event_id: str, digest: str) -> tuple[str, Optional[Entry]]:
        """登记首领占位。返回 `(状态, 占位项)`。

        状态：`reserved`（登记成功，调用方成为首领）/ `hit` / `inflight` /
        `conflict`（语义同 `check`）。

        **为什么登记前要再查一次**：调用方在 `check` 之后可能已经 `await` 过
        （例如取号取库），期间另一个请求可能已经登记。重复登记会覆盖在途项，
        使两个首领并行产出两份决策。因此这里把 check 与 insert 合并为一个
        没有 `await` 的临界区，由它做唯一的权威判定。
        """
        state, body = self.check(event_id, digest)
        if state != "miss":
            return state, None
        # 预留一个槽位给马上就要插入的新项，否则缓存会稳定在 capacity+1 条
        self.prune(reserve_slots=1)
        entry = Entry(digest=digest, created_ms=self._now())
        self._items[event_id] = entry
        return "reserved", entry

    def attach_waiter(self, event_id: str, waiter: Any) -> bool:
        """给在途项挂一个等待者。返回是否挂上。

        找不到项（极端情况：首领刚失败并放弃了占位）时返回 False，调用方
        应回退为直接重算——**绝不返回空决策**。
        """
        entry = self._items.get(event_id)
        if entry is None or entry.ready:
            return False
        entry.waiters.append(waiter)
        return True

    def finish(self, event_id: str, body: dict, doc: Optional[dict] = None) -> None:
        """首领完成：写入响应体与落库文档，并唤醒全部等待者。

        **这是"返回响应之前写入"的落点**：`ingest()` 在 `return` 之前一定
        先调它，且它不含 `await`。
        """
        entry = self._items.get(event_id)
        if entry is None:
            return
        entry.body = body
        entry.doc = doc
        waiters, entry.waiters = entry.waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(body)
        self._items.move_to_end(event_id)

    def release(self, event_id: str) -> None:
        """首领失败（降级之外的异常）：撤掉占位，让后来的请求能重算。

        如果不撤销，一条抛异常的请求会把该编号永久占住（占位项没有 `body`，
        会一直让后来者等一个永远不会来的结果）。
        """
        entry = self._items.pop(event_id, None)
        if entry is None:
            return
        for waiter in entry.waiters:
            if not waiter.done():
                # 让等待者立刻拿到一个明确的失败，而不是挂到超时：
                # 首领失败往往意味着下游不可用，等待者应当自行重试而非等待
                waiter.set_exception(RuntimeError("幂等占位已被释放，请重试"))

    def take_doc(self, event_id: str) -> Optional[dict]:
        """取该编号在 `finish` 时登记、待落库的文档（查不到返回 None）。

        注意：异步落库路径**不**依赖它——`EventService` 在发起落库任务时就把
        文档作为参数捕获了，而不是事后再回缓存里取。原因是缓存有 LRU 淘汰：
        压测下这条项可能在落库任务真正执行前就被挤掉，那时再查就会拿到 None，
        落库静默丢失。这个方法保留下来是为了能直接断言"缓存项里到底存了什么"。
        """
        entry = self._items.get(event_id)
        return None if entry is None else entry.doc


# ============================================================
# 进程内单例（事件编号是全局唯一键，缓存必须跨请求共享）
# ============================================================
_STORE = IdempotencyStore()


def get_store() -> IdempotencyStore:
    return _STORE


def reset_store() -> None:
    """复位缓存（测试夹具使用；生产无调用点，理由见 `clear`）。"""
    _STORE.clear()
    _STORE.stats = {"hits": 0, "conflicts": 0, "waits": 0, "evicted": 0, "expired": 0}


__all__ = [
    "DEFAULT_CAPACITY", "DEFAULT_TTL_MS", "Entry", "IdempotencyStore",
    "get_store", "payload_digest", "reset_store",
]
