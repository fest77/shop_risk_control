# -*- coding: utf-8 -*-
"""`POST /api/v1/engine/evaluate` 的请求/响应模型（模块 05 §3.1）。

## 为什么 `event` 是 `dict` 而不是一个强类型模型

Spec §3.1 的请求表把它写成"完整事件体，结构同 `POST /api/v1/events` 的 `event`"。
事件的强类型模型是 `app/schemas/event_schema.EventIn`，但**这里刻意不再声明一遍**：

1. 03 的校验（`event_service.validate_event`）已经承担了全部字段级判定，而且
   它的错误码是 `EVT-4001~4007`——若这里用 Pydantic 再声明一次，同一个字段
   类型错误会先被 Pydantic 拦成通用 `COM-4001`，调用方（尤其模块 10）拿到的
   错误码就与事件接口不一致了；
2. 事件模型会随 E01 演进，两处各写一遍必然有一处先漂移。

因此本模块只做"是不是对象、有没有 `event_type` / `user_id`"这两步
（`RUL-4001`，Spec §5 对它的定义就是这两个字段），其余交给 03 的校验器。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class EngineEvaluateIn(BaseModel):
    """`POST /engine/evaluate` 请求体（§3.1）。"""

    model_config = ConfigDict(extra="forbid")

    event: dict[str, Any] = Field(description="完整事件体，结构同 POST /api/v1/events 的 event")
    dry_run: bool = Field(
        default=True,
        description="默认 true：**不写** decisions / decision_hits（仿真/调试用）",
    )
    trace: bool = Field(
        default=False,
        description="true 时返回逐规则求值过程（trace），否则为 null",
    )


class EngineEvaluateOut(BaseModel):
    """`POST /engine/evaluate` 响应体（§3.1 的字段，**逐项对齐**）。

    `warnings` 是对 §3.1 表的**一处追加**，用于承载 Spec §5 的 `RUL-5003`
    （单条规则求值异常，页面要在命中明细下方提示「有 N 条规则求值失败」）与
    `RUL-5004`（耗时超阈值）。这两个码的 HTTP 是 200，页面又必须看到它们，
    而冻结的 12 字段决策块里没有承载位——因此只能放在响应层。
    **它不进决策块**（`DecisionProvider` 的契约仍然是那 12 项，多一个都不行）。
    """

    model_config = ConfigDict(extra="allow")

    event_id: str
    snapshot_id: Optional[str] = None
    list_hit: dict[str, Any] = Field(default_factory=dict)
    rule_score: int = 0
    model_score: Optional[float] = None
    final_score: int = 0
    risk_level: str = "low"
    decision: str = "review"
    hit_rule_count: int = 0
    hits: list[dict[str, Any]] = Field(default_factory=list)
    rule_versions: dict[str, Any] = Field(default_factory=dict)
    engine_version: str = ""
    elapsed_ms: int = 0
    degraded: bool = False
    trace: Optional[list[dict[str, Any]]] = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)


__all__ = ["EngineEvaluateIn", "EngineEvaluateOut"]
