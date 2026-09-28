# -*- coding: utf-8 -*-
"""模块 05 对模块 03 的装配入口：`RuleDecisionProvider`（决策 D45）。

## 它为什么存在

`app/protocols.py` 冻结了 03 → 05 的调用契约：

    class DecisionProvider(Protocol):
        async def evaluate(self, event: dict, features: dict) -> dict: ...

在模块 05 落地之前，`Components.decision_provider` 装的是
`UnavailableDecisionProvider`（**明确不可用**，调用即抛异常），因此每个事件都
停在 `degrade.stage="rule"`。本类把那一格换成真实实现，链路才第一次真正跑通：
名单过滤 → 条件树求值 → 分值累加 → 仲裁分级 → 产出 `pass/review/reject`。

**方法签名一个字都不能改**：03 直接依赖它，改了就是跨模块破坏。本类只做一件事
——把 03 给的特征转交给 `decision.decide()`，并原样返回那个决策块。

## 为什么这里**不抛异常**（即使依赖挂了）

与 `UnavailableDecisionProvider` 的行为正好相反，这是**刻意的**：

- 占位实现必须抛异常，因为"05 根本没跑"与"05 判了 review"是两件不同的事，
  抛异常才能让 03 标出 `stage=rule`（结论 vs 故障）。
- 真实实现**不能**抛异常：决策 D5 要求"降级也必须写一条 `decisions`
  （`decision=review`、`degraded=true`），08 据此建案"。若 05 抛异常，控制权
  就交回 03 的降级路径，而**那条路径不写 decisions**——于是这次降级请求
  永远不会出现在 07 的案件列表里，无人处理，等于变相丢弃，正是 D5 要避免的。

因此 `decide()` 把名单/规则依赖故障消化成 `review + degraded=true` 的决策块，
本类把它交给 03 透传（Spec §3.2「契约冻结：03 不得自行拼装该结构」）。

## 返回值字段

严格按 `app/protocols.py` 冻结的 12 项（`list_hit` 到 `elapsed_ms`）。
唯一的例外是 `snapshot_id`：03 的调用签名里**没有**快照编号（只有 03 手里有），
因此不知道时**不写这个键**，由 03 的 `normalize_decision` 用真实快照编号补上
（`setdefault`）。写 `None` 会让 `setdefault` 失效，响应里的 `snapshot_id`
会永久为 null——那比"少一个键"糟得多。详见 `app/engine/decision.py` 的模块说明。
"""
from __future__ import annotations

from app.engine import decision as decision_engine
from app.engine.decision import DecisionOutcome


class RuleDecisionProvider:
    """规则决策引擎的真实实现（模块 05）。"""

    #: 供 `/health` 与启动日志一眼看出"这个组件是真的还是占位"
    available = True

    async def evaluate(self, event: dict, features: dict) -> dict:
        """对一条事件求值并给出决策块（**契约冻结**，见模块 docstring）。

        `features` 由 03 从 04 的快照里取好（`snapshot["features"]`），因此本方法
        **不再访问 04**：04→05 的先后依赖由 03 串行保证（BR-03-15），05 若自己
        再算一次特征，轻则白花一次窗口计算，重则与决策依据不是同一份快照。
        """
        outcome = await decision_engine.decide(event, features)
        return outcome.block

    async def evaluate_outcome(self, event: dict, features: dict) -> DecisionOutcome:
        """与 `evaluate` 同源，但返回带 `warnings`/`trace` 的完整结果。

        `/engine/evaluate` 需要那些附加信息（`degraded`、`warnings`、`trace`），
        而 03 的契约只有 12 个字段。**两条路走同一个 `decide()`**，因此
        事件链路与仿真链路的结果不可能不一致（V-05-13 的复用要求由此成立）。
        """
        return await decision_engine.decide(event, features)


__all__ = ["RuleDecisionProvider"]
