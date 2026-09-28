# -*- coding: utf-8 -*-
"""名单降级状态（fail-closed 的可见化载体）。

对齐模块 06 `BR-06-24` / §5.3：名单写入失败时**绝不静默吞掉**，必须
① 返回 `CFG-5002` ② 置降级标记并记录 `degraded_since` ③ 失效缓存。

本切片按概要设计（单进程 uvicorn 单 worker）用**进程内状态**承载；
模块 12/13 落地后可换成带持久化的实现，接口 `snapshot()` 不变。
"""
from __future__ import annotations

from typing import Optional

from app.utils.timeutil import now_ms


class DegradedState:
    """名单服务降级标记。**只由写失败设置**（读失败不改它，避免留下粘性状态）。"""

    def __init__(self) -> None:
        self.degraded: bool = False
        self.degraded_since: Optional[int] = None
        self.last_error: Optional[str] = None
        self.last_flush_at: int = now_ms()

    def mark(self, error: str) -> None:
        if not self.degraded:
            self.degraded = True
            self.degraded_since = now_ms()
        self.last_error = error
        # 主动失效：本切片无决策侧缓存（属模块 05），此处记录失效时刻，
        # 保证 05 接入后能据此判断"缓存是否已清"
        self.last_flush_at = now_ms()

    def clear(self) -> None:
        self.degraded = False
        self.degraded_since = None
        self.last_error = None
        self.last_flush_at = now_ms()

    def snapshot(self) -> dict:
        return {
            "degraded": self.degraded,
            "degraded_since": self.degraded_since,
            "last_flush_at": self.last_flush_at,
            "last_error": self.last_error,
        }


DEGRADED = DegradedState()
