# -*- coding: utf-8 -*-
"""限流（悬空点「限流策略」，模块 00 §8）。

**为什么需要**：`COM-4290` 在错误码表里，就必须有真实触发路径，否则该码永远
不可达、也无法被测试覆盖——"定义了但走不到"的错误码是假实现。

**实现选择**：单进程内存滑动窗口。PRD 没有性能指标，演示是单 worker，
用 Redis 属于过度设计；但**必须只依赖 `allow()` 这一个契约**，将来换 Redis
时中间件不改。

**已知局限（如实记录，不掩盖）**：多 worker 部署时每个 worker 各有计数，
实际额度是 `N × max_requests`。生产环境需换成集中式计数。
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """按 key（用户/IP）的滑动窗口计数限流。"""

    def __init__(self, max_requests: int, window_sec: int):
        self.max_requests = max_requests
        self.window_sec = window_sec
        # key -> 该 key 在窗口内的请求时间戳（单调递增，故可用 deque 左端过期）
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        """记录一次请求并判断是否放行。"""
        ts = time.monotonic() if now is None else now
        bucket = self._hits[key]
        self._evict(bucket, ts)
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(ts)
        return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        """还要等多少秒才能再次请求（用于 `Retry-After` 与提示文案）。"""
        ts = time.monotonic() if now is None else now
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        self._evict(bucket, ts)
        if not bucket:
            return 0
        # 最早一次请求滑出窗口即可再试
        return max(1, int(self.window_sec - (ts - bucket[0])) + 1)

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        self._hits.clear()

    def _evict(self, bucket: deque[float], now: float) -> None:
        deadline = now - self.window_sec
        while bucket and bucket[0] <= deadline:
            bucket.popleft()


# 进程内单例。额度可用环境变量覆盖，便于压测与演示时放宽。
LIMITER = SlidingWindowLimiter(
    max_requests=int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "60")),
    window_sec=int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60")),
)
