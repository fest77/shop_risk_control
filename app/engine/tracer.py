# -*- coding: utf-8 -*-
"""链路追踪器：五步记录的收集、耗时统计与 payload 归集（模块 10 §6 / BR-10-10~13）。

## 为什么单独一个文件、而不是在 `SimService` 里push 几个 dict

BR-10-10 要求每一步**独立**记录五个属性（名称 / 状态 / 结论摘要 / 耗时 /
结构化 payload），BR-10-12 还要求"各步耗时之和与总耗时的差值可解释"。
这两条都是**关于记录本身**的规则，与"怎么跑特征、怎么跑规则"无关。
把它们抽出来之后：

1. `SimService` 只负责"按顺序调 03/04/05"，步骤记录的正确性可以脱离 Mongo
   与引擎单独单测（本文件不 import 任何业务模块）；
2. `orchestration_ms`（编排开销）与 `over_threshold`（差值 > 50% 的告警）
   只有一处实现——它们若散在服务里，"总和对不对"会变成每次都要重算一遍的事。

## 耗时为什么用 `time.perf_counter()`

`perf_counter` 是单调时钟：它不会因为系统时间被 NTP 回拨而给出负耗时。
`now_ms()`（墙上时钟）仍然是**事件时间**的基准，两者不能混用——
把墙上时钟的差值当耗时，会在时钟回拨时算出负数，而负数耗时会被前端
渲染成"耗时 -3ms"这种没人能解释的东西。

## 差值 > 50% 时"记录告警"而不是"报错"（BR-10-12）

差值的来源是**编排本身**（组装响应、写 `sim_runs`、审计、序列取号），
它天然存在且大小与 Mongo 的响应速度有关。把它做成硬失败会让仿真在
Mongo 稍慢时整体不可用；做成静默忽略又违背"需可解释"。因此：
如实回传 `orchestration_ms`（前端可以显示"其余为编排开销 Nms"），
并在超过 50% 时记一条 `WARNING` 日志 + 在 payload 里置 `over_threshold`。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.logging import get_logger

log = get_logger("shop_risk_control.sim_tracer")

#: 步骤状态（Spec BR-10-10）
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_PENDING = "pending"

#: "编排开销占总数"的告警线（BR-10-12：差值 > 50% 时记录告警）
ORCHESTRATION_WARN_RATIO = 0.5

#: 未执行步骤的结论行文案（BR-10-15：失败步骤之后的步骤显示"未执行"）
NOT_EXECUTED_DETAIL = "未执行（前序步骤未通过）"


@dataclass
class TraceStep:
    """五步链路里的单步记录（Spec §3.3 的 `steps[]` 元素）。"""

    seq: int
    name: str
    label: str = ""
    status: str = STATUS_PENDING
    detail: str = ""
    elapsed_ms: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": int(self.elapsed_ms),
            "payload": dict(self.payload),
        }


class SimTracer:
    """一次仿真执行的五步记录器。

    用法固定为"按顺序 `start(seq)` → 干活 → `finish(seq, ...)`"，
    `start` 之前**不预建**未执行步骤：五步是按顺序点亮的前端契约
    （BR-10-14），后端若一次性把五步都塞进去，"哪些没跑"就看不出来了。
    失败时调用 `fail(seq, ...)` 再由 `mark_not_executed(after_seq)` 补齐
    后续步骤——那一步是**显式**的，因为"从来没创建"与"标记为未执行"
    在前端是两种渲染（骨架 vs 红/灰块）。
    """

    def __init__(self, names: tuple[str, ...], labels: Optional[dict[str, str]] = None):
        self._names = tuple(names)
        self._labels = dict(labels or {})
        self._steps: dict[int, TraceStep] = {}
        self._started: dict[int, float] = {}
        self._started_at: float = time.perf_counter()
        self.finished_ms: int = 0

    # ---------------- 生命周期 ----------------
    @property
    def started_at(self) -> float:
        return self._started_at

    def start(self, seq: int) -> None:
        """开始计时。幂等：重复调用以**最后一次**为准（重试场景）。"""
        self._started[int(seq)] = time.perf_counter()

    def _elapsed(self, seq: int) -> int:
        began = self._started.get(int(seq))
        if began is None:
            return 0
        return max(0, int((time.perf_counter() - began) * 1000))

    def elapsed_since(self, seq: int) -> int:
        """取某步**已经过去**的毫秒数（**不落定**该步）。

        与 `finish` 的区别：`finish` 会把这一步的状态与结论钉死，而本方法只回答
        "从 `start(seq)` 到现在过了多久"。用途是**拼结论行**——例如步骤 1 的文案
        是「通过 · event_id=EVT…218 · 耗时 1ms」，那 1ms 必须在 `finish` **之前**
        就取到（`finish` 的 `detail` 是它的入参）。已经 `finish` 过的步骤返回它
        落定的耗时。
        """
        step = self._steps.get(int(seq))
        if step is not None:
            return int(step.elapsed_ms)
        return self._elapsed(seq)

    def finish(
        self,
        seq: int,
        detail: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        status: str = STATUS_OK,
        elapsed_ms: Optional[int] = None,
    ) -> TraceStep:
        """收尾一步并落定耗时。`elapsed_ms` 给了就用它（跨 `await` 的实测值）。"""
        step = TraceStep(
            seq=int(seq),
            name=self._names[int(seq) - 1],
            label=self._labels.get(self._names[int(seq) - 1], ""),
            status=status,
            detail=detail,
            elapsed_ms=self._elapsed(seq) if elapsed_ms is None else max(0, int(elapsed_ms)),
            payload=dict(payload or {}),
        )
        self._steps[int(seq)] = step
        return step

    def fail(
        self,
        seq: int,
        detail: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        elapsed_ms: Optional[int] = None,
    ) -> TraceStep:
        """标记一步失败（HTTP 侧仍按各自的错误码返回，见 `sim_service`）。"""
        return self.finish(seq, detail, payload=payload, status=STATUS_FAILED,
                           elapsed_ms=elapsed_ms)

    def skip(self, seq: int, detail: str = NOT_EXECUTED_DETAIL,
             *, payload: Optional[dict[str, Any]] = None) -> TraceStep:
        """标记一步未执行（BR-10-11：名单直通时步骤 4 是 `skipped` 而不是 `ok`）。"""
        return self.finish(seq, detail, payload=payload, status=STATUS_SKIPPED,
                           elapsed_ms=0)

    def mark_not_executed(self, after_seq: int) -> None:
        """把 `after_seq` 之后的步骤全部标成未执行（BR-10-15）。

        **不覆盖已有记录**：某一步若已经因为别的原因被显式记为 `skipped`
        （例如名单直通导致的步骤 4），这里不能再把它改写成"未执行"——
        两者的原因不同，改写会把"因为名单命中所以没求值规则"这条**有效信息**
        抹成一句笼统的"前序失败"。
        """
        for seq in range(int(after_seq) + 1, len(self._names) + 1):
            if seq not in self._steps:
                self.skip(seq)

    def finish_all(self) -> int:
        """记录总耗时（wall clock，覆盖整次编排）。返回毫秒。"""
        self.finished_ms = max(0, int((time.perf_counter() - self._started_at) * 1000))
        return self.finished_ms

    # ---------------- 结果 ----------------
    def steps(self) -> list[dict[str, Any]]:
        """按 `seq` 升序返回已记录的步骤。"""
        return [self._steps[k].to_dict() for k in sorted(self._steps)]

    def step(self, seq: int) -> Optional[TraceStep]:
        return self._steps.get(int(seq))

    @property
    def step_sum_ms(self) -> int:
        """各步耗时之和（BR-10-12 的被减数）。"""
        return sum(int(s.elapsed_ms) for s in self._steps.values())

    def orchestration_ms(self, total_ms: int) -> int:
        """编排开销 = 总耗时 − 各步之和（**可以为 0**，但绝不为负）。"""
        return max(0, int(total_ms) - self.step_sum_ms)

    def orchestration_ratio(self, total_ms: int) -> float:
        """编排开销占比。总耗时为 0 时返回 0.0（不做除零）。"""
        total = int(total_ms)
        if total <= 0:
            return 0.0
        return self.orchestration_ms(total) / float(total)

    def summary(self, total_ms: Optional[int] = None) -> dict[str, Any]:
        """BR-10-12 的结论块：总耗时 / 各步之和 / 编排开销 / 是否超线。"""
        total = self.finished_ms if total_ms is None else int(total_ms)
        ratio = self.orchestration_ratio(total)
        over = ratio > ORCHESTRATION_WARN_RATIO
        if over:
            # 只告警不失败：差值来自真实的编排开销（组装响应、写 sim_runs、审计），
            # 把它做成硬失败会让仿真在 Mongo 稍慢时整体不可用（见模块 docstring）。
            log.warning(
                "[SIM-5002] 链路编排开销占比过高：总 %dms / 各步之和 %dms / "
                "开销 %dms（%.0f%%）steps=%s",
                total, self.step_sum_ms, self.orchestration_ms(total), ratio * 100,
                [s.name for s in self._steps.values()],
            )
        return {
            "total_ms": total,
            "steps_sum_ms": self.step_sum_ms,
            "orchestration_ms": self.orchestration_ms(total),
            "orchestration_ratio": round(ratio, 3),
            "over_threshold": over,
            "threshold_ratio": ORCHESTRATION_WARN_RATIO,
        }


__all__ = [
    "NOT_EXECUTED_DETAIL",
    "ORCHESTRATION_WARN_RATIO",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_PENDING",
    "STATUS_SKIPPED",
    "SimTracer",
    "TraceStep",
]
