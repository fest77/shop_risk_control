# -*- coding: utf-8 -*-
"""登录失败锁定（BR-01-05，模块 01 §8 登记为"PRD 未要求、可关闭"）。

**为什么需要**：没有锁定时，攻击者可以对同一账号无限次尝试口令。bcrypt 只是
把单次尝试变慢，挡不住"慢而持续"的爆破。

**实现选择**：进程内滑动窗口计数（与模块 00 的 `COM-4290` 限流同思路）。
演示环境是单 worker，用 Redis 属过度设计；**已知局限**：多 worker 部署时
每个 worker 各自计数，实际允许次数为 `N × max_failures`，生产需换集中式计数。

**为什么按账号而不是按 IP 计数**：按 IP 会让同一出口 NAT 后的正常用户互相牵连；
按账号计数恰好对应"保护这个账号不被爆破"这一目标。两者都做才是完整方案，
但那需要引入 IP 信誉，超出本项目范围（已在 §8 登记）。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class LoginGuard:
    """按账号记录连续失败次数，超限后锁定一段时间。"""

    def __init__(self, max_failures: int, lock_seconds: int):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        # username -> 失败时间（单调时钟，避免系统时间被改动影响判定）
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}

    def is_locked(self, username: str, now: float | None = None) -> int:
        """返回剩余锁定秒数；未锁定返回 0。"""
        ts = time.monotonic() if now is None else now
        until = self._locked_until.get(username)
        if until is None:
            return 0
        remain = int(until - ts)
        if remain <= 0:
            # 锁定到期即整体释放，连同失败计数一起清掉——
            # 否则用户解锁后再失败一次就被立刻重新锁上，体验极差且不合理
            self._locked_until.pop(username, None)
            self._failures.pop(username, None)
            return 0
        return remain

    def record_failure(self, username: str, now: float | None = None) -> int:
        """记一次失败，返回剩余锁定秒数（0 表示尚未锁定）。"""
        ts = time.monotonic() if now is None else now
        bucket = self._failures[username]
        bucket.append(ts)
        # 只保留锁定窗口内的失败：失败次数应当随时间自然衰减
        deadline = ts - self.lock_seconds
        while bucket and bucket[0] <= deadline:
            bucket.popleft()
        if len(bucket) >= self.max_failures:
            self._locked_until[username] = ts + self.lock_seconds
            return self.lock_seconds
        return 0

    def clear(self, username: str) -> None:
        """登录成功后清零（BR-01-05 只针对"连续"失败）。"""
        self._failures.pop(username, None)
        self._locked_until.pop(username, None)

    def reset_all(self) -> None:
        """清空全部计数（测试用）。"""
        self._failures.clear()
        self._locked_until.clear()

    def snapshot(self) -> dict:
        """当前锁定情况（供系统设置/health 观测）。"""
        return {
            "locked_accounts": len(self._locked_until),
            "tracked_accounts": len(self._failures),
            "max_failures": self.max_failures,
            "lock_seconds": self.lock_seconds,
        }
