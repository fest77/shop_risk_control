# -*- coding: utf-8 -*-
"""规则选取 → 逐条求值 → 分值累加 → 失败隔离（BR-05-08 ~ 05-16）。

## 场景映射（BR-05-08，含决策 D12 的修正）

    login → login            coupon_receive → coupon
    order_create → order     **order_pay → pay**      after_sale_apply → aftersale

`order_pay → pay` 是 D12 明确修掉的坑：原实现把它也映射到 `order`，
于是 E06 定义的 `pay` 场景**永远不可达**——数据行存在、页面下拉里有它、
配了规则却一条都不会被求值，而且没有任何报错。映射表复用
`app/engine/metric_agg.EVENT_SCENE_MAP`（模块 11 也已经按 D12 对齐），
**不另写一份**：两份映射必然有一份先漂移，届时大盘的场景分布与规则命中
会对不上同一个事件。

## `common` 场景不是代码特判（BR-05-09 / D10 / D13）

"参与求值的规则 = `scene_code IN (本次场景, 'common')`"里的 `common` 由
`rule_repo.COMMON_SCENE_CODE` 提供，它只是拼查询条件用的**数据键**（E06 里
真实存在的一行），没有任何 `if scene == "common"` 这样的行为分支。详见该常量的注释。

## 为什么必须全量求值（BR-05-14）

`priority` 只用于稳定排序与展示，**不做短路**。加分制的总分是
`Σ(命中规则的 score)`，一旦"高优先级命中就停下"，总分就变成了
"按优先级找到的第一条规则的分值"——`rule_score`、`hit_rule_count`、
`rule_versions` 三处同时失真，而界面上完全看不出异常。这也是本项目里
"照抄通用规则引擎"最容易踩的一个坑：短路是通用引擎的常规优化，但它的
前提是"规则的结论互斥且按优先级取第一个"，与加分制不兼容。

## 失败隔离（BR-05-13）

单条规则求值抛异常（条件树非法、字段类型诡异、比较器意外）时：

- **该规则记为求值失败、不计分**；
- 写告警（`RUL-5003`），并把它带进本次结果供接口层展示"有 N 条规则求值失败"；
- **其余规则照常求值累加**。

为什么不整次决策失败：一条坏配置（例如 06 存了一棵引用不存在算子的树）
不该让**所有**事件的决策消失。整次失败会返回 500 或降级 review，于是
"一条规则写错"被升级成"全站规则失效"——而真正的问题只是那一行数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.engine import evaluator
from app.engine.arbiter import arbitrate
from app.engine.condition import validate_tree
from app.engine.evaluator import LeafFact
from app.engine.metric_agg import EVENT_SCENE_MAP
from app.errors import AppError, RUL_NOTICE

#: 事件类型 -> 场景码。**复用模块 11 已按 D12 对齐的那份表**（见模块 docstring）。
SCENE_BY_EVENT_TYPE: dict[str, str] = dict(EVENT_SCENE_MAP)

#: 无法映射场景时使用的场景码。**刻意不用 `common`**：`common` 是"通用规则
#: 对所有场景生效"，而未知事件类型是"我们不知道它属于哪个场景"。若回落到
#: `common`，一条只该作用于通用场景的规则会被拿去判一个语义不明的事件，
#: 而 `common` 恰恰是所有场景都会取到的集合——那等价于"对所有事件生效"，
#: 比"不判"危险。因此这里用一个取不到任何规则的值，结论是"零条规则命中"。
UNKNOWN_SCENE = "unknown"


@dataclass(frozen=True)
class RuleHit:
    """一条命中规则（E04 `decision_hits` 的待写入内容 + `hits` 数组的元素）。"""

    rule_code: str
    rule_name: str
    rule_version: int
    score: int
    reason: str
    matched_facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Spec §3.1 的 `hits[]` 元素形状（**逐字段**，多一个少一个都不行）。"""
        return {
            "rule_code": self.rule_code,
            "rule_name": self.rule_name,
            "rule_version": self.rule_version,
            "score": self.score,
            "reason": self.reason,
            "matched_facts": self.matched_facts,
        }


@dataclass(frozen=True)
class RuleFailure:
    """一条求值失败的规则（BR-05-13 / `RUL-5003`）。"""

    rule_code: str
    rule_name: str
    message: str

    def to_warning(self) -> dict[str, Any]:
        return {
            "code": "RUL-5003",
            "message": f"{RUL_NOTICE['RUL-5003']}：{self.rule_code}"
                       f"（{self.rule_name}）——{self.message}",
            "rule_code": self.rule_code,
        }


@dataclass
class RuleEvaluation:
    """一次规则集求值的完整结果。"""

    hits: list[RuleHit] = field(default_factory=list)
    failures: list[RuleFailure] = field(default_factory=list)
    #: 逐规则求值过程（**仅 `trace=true` 时填充**，Spec §3.1）
    trace: Optional[list[dict]] = None
    #: 本次参与求值的规则条数（含未命中的），供诊断与测试
    evaluated_rules: int = 0
    #: 命中的规则编码 -> 版本（BR-05-20 的 `rule_versions` 快照）
    rule_versions: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """命中规则分值之和（**未截断**；截断由 `arbiter` 负责，只有一处）。"""
        return sum(hit.score for hit in self.hits)


def scene_for(event_type: str) -> str:
    """事件类型 -> 场景码（BR-05-08 / D12）。无法映射时返回 `UNKNOWN_SCENE`。"""
    return SCENE_BY_EVENT_TYPE.get(str(event_type or ""), UNKNOWN_SCENE)


def _rule_field(rule: dict, key: str, default: Any = None) -> Any:
    """读规则字段。

    `_id` 就是规则编码（E05 的主键是 `R{场景码}{3位序号}`），因此 `rule_code`
    一律从 `_id` 取——若允许另存一个 `rule_code` 字段，两处迟早不一致，
    而 `decision_hits.rule_code` 与 `rule_versions` 的键都以它为准。
    """
    if key == "rule_code":
        return rule.get("_id") or rule.get("rule_code")
    return rule.get(key, default)


def _as_int(value: Any, default: int = 0) -> int:
    """把 `score`/`version` 这类字段安全地取成 int。

    脏数据（`score: "40"`、`version: null`）不抛异常：一条字段格式不对的规则
    不应该让整次决策失败，按 `default` 取并继续——真正的兜底是"这条规则的
    分值可能是错的"，而它由**配置校验**（06）负责，不是决策链路的职责。
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def evaluate_rules(
    rules: Iterable[dict],
    features: dict[str, Any],
    *,
    trace: bool = False,
) -> RuleEvaluation:
    """对一批规则逐条求值并累加（**纯函数**，无 IO，可直接单测）。

    `rules` 的**顺序即输出顺序**（`rule_repo` 已按 `priority, _id` 稳定排序），
    本函数不再排序：排序是取数层的职责，混进来会让"顺序为什么是这样"出现两个解释。
    """
    result = RuleEvaluation(trace=[] if trace else None)

    for rule in rules:
        rule_code = str(_rule_field(rule, "rule_code") or "")
        rule_name = str(rule.get("name") or rule_code)
        rule_version = _as_int(rule.get("version"), 0)
        score = _as_int(rule.get("score"), 0)
        result.evaluated_rules += 1
        # BR-05-20：`rule_versions` 记录的是**本次生效规则集的版本快照**——
        # 包括未命中的、以及求值失败的那些。只记命中项无法真正重放：重放时
        # 必须先知道"当时有哪些规则参与"，否则一条后来被停用（或条件被改坏）
        # 的规则会让重放结果与历史分数对不上，而"对不上"正是重放要发现的东西。
        if rule_code:
            result.rule_versions[rule_code] = rule_version

        try:
            tree = validate_tree(rule.get("condition"))
            facts: list[LeafFact] = evaluator.collect(tree, features)
            matched = evaluator.evaluate_tree(tree, features)
            reason = evaluator.describe(facts) if matched else "条件不成立"
            facts_map = evaluator.matched_facts(facts) if matched else {}
        except AppError as e:
            # 条件树非法（RUL-4002）在这里被**降级为该条规则失败**而不是整次失败，
            # 见模块 docstring 的失败隔离一节。
            result.failures.append(RuleFailure(rule_code, rule_name, e.message))
            if result.trace is not None:
                result.trace.append({
                    "rule_code": rule_code,
                    "evaluated": False,
                    "matched": False,
                    "reason": f"求值失败：{e.message}",
                })
            continue
        except Exception as e:  # noqa: BLE001 - BR-05-13：任何异常都只影响这一条规则
            result.failures.append(
                RuleFailure(rule_code, rule_name, f"{type(e).__name__}: {e}")
            )
            if result.trace is not None:
                result.trace.append({
                    "rule_code": rule_code,
                    "evaluated": False,
                    "matched": False,
                    "reason": f"求值失败：{type(e).__name__}: {e}",
                })
            continue

        if result.trace is not None:
            result.trace.append({
                "rule_code": rule_code,
                "evaluated": True,
                "matched": bool(matched),
                "reason": reason,
            })

        if not matched:
            continue

        # BR-05-21 的两项冗余快照（rule_name / score）在 `RuleHit` 里已经带上了，
        # 落库时原样写进 decision_hits——**不联查 rules**，否则规则改名改分后
        # 历史明细会跟着变，等于篡改历史证据。
        result.hits.append(RuleHit(
            rule_code=rule_code,
            rule_name=rule_name,
            rule_version=rule_version,
            score=score,
            reason=reason,
            matched_facts=facts_map,
        ))

    return result


def finalize(evaluation: RuleEvaluation) -> tuple[int, str, str]:
    """分值 → `(rule_score, risk_level, decision)`（截断与分档都在 `arbiter`）。

    单独一个薄封装是为了让"累加"与"仲裁"的边界只有一处：调用方拿到的分值
    一定已经截断（BR-05-16），不会出现"响应里是 100、库里是 120"这种两处
    各存一个分值的情况。
    """
    return arbitrate(evaluation.total)


__all__ = [
    "SCENE_BY_EVENT_TYPE",
    "UNKNOWN_SCENE",
    "RuleEvaluation",
    "RuleFailure",
    "RuleHit",
    "evaluate_rules",
    "finalize",
    "scene_for",
]
