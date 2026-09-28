# -*- coding: utf-8 -*-
"""条件树求值（BR-05-10 ~ 05-12）——**纯函数、无 IO、无全局状态**。

## 为什么必须纯

它是整个决策链路上唯一"只靠输入就能重放"的一环：给定同一棵树与同一份特征，
结果永远一致。因此它能在不碰 Mongo、不起 HTTP 的情况下被精确单测——而算子
语义一旦错了（例如把缺失当成 0、把 `ne` 当成"不等于即命中"），后果是**静默**
的：没有任何报错，只是某些规则永远不命中、或某些规则对所有用户都命中。

## 算子语义（BR-05-11，逐条钉住）

| 算子 | 语义 | 对 `null`（特征缺失） |
|---|---|---|
| `eq` / `ne` | 恒等比较（数值按数值比、字符串按字符串比、布尔按布尔比） | `false` |
| `gt` / `gte` / `lt` / `lte` | **仅数值**；非数值（含布尔、字符串）一律 `false` | `false` |
| `in` / `not_in` | 右侧必须是数组；逐个元素做恒等比较 | `false` |
| `exists` | 判该字段是否**有值**（`features.get(field) is not None`） | 它是唯一的例外 |
| `contains` | **仅字符串**：右侧是左串的子串 | `false` |

## 为什么特征缺失返回 `false` 而不是抛异常（BR-05-12，本模块最容易被改错的一条）

冷启动是**常态**而不是异常：新用户没有历史、新设备没有画像、E21 基线还没算。
若把缺失当异常抛出，一次正常的新用户请求就会变成 500（或整条决策失败）；
若把缺失当 `0`，那么 `device_user_cnt gte 5`（同设备 ≥5 个账号）会被算成
"这台设备只关联 0 个账号"——**把"我不知道"读成了"他很干净"**，于是最该拦的
团伙新账号恰好被放行。`0` 是一个断言，`null` 是一个未知；两者必须分开。

返回 `false` 的代价是"这条规则这次不计分"，这在风控里是**保守方向**（少加一次分
= 更容易落到 `pass`）……所以它必须配合另一条约束才安全：真正会因缺失而漏判的
特征（当前是 `ip_is_proxy`）由 04 登记进 `degrade_suggested`，由 03 在调 05 之前
就 fail-closed 短路（D46 / D44）。**05 不做这个判断，也不该做**：它只负责
"按给定特征如实求值"，缺失的严重性由拥有该特征口径的模块评估。

### `ne` 对缺失为什么也返回 `false`

直觉上"这个字段不等于 X"在字段缺失时应当为真，但这里刻意返回 `false`：
若 `ne` 在缺失时为真，则 `ip_is_proxy ne true` 这类规则会对**所有没画像的用户**
命中并加分——等于把"我们不知道他是不是代理"当成"他肯定不是代理"，方向恰好相反。
缺失的唯一出口是显式的 `exists`，规则作者想表达"没有值也算命中"时必须写明它。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.engine.condition import ConditionNode, Op, validate_tree
from app.engine.feature_compute import FEATURE_LABELS

#: `reason` 文案里每个算子对应的数学符号（Spec §2.2 的「≥ 阈值 5」形态）
_OP_SYMBOL: dict[Op, str] = {
    Op.EQ: "=",
    Op.NE: "≠",
    Op.GT: ">",
    Op.GTE: "≥",
    Op.LT: "<",
    Op.LTE: "≤",
    Op.IN: "∈",
    Op.NOT_IN: "∉",
    Op.EXISTS: "存在",
    Op.CONTAINS: "包含",
}

#: `reason` 最多列几条命中事实（超长文案在命中明细表里会被截断，反而看不清）
_MAX_REASON_TERMS = 4


# ============================================================
# 比较原语
# ============================================================
def _eq(actual: Any, expected: Any) -> bool:
    """恒等比较，但**不跨类型静默相等**。

    两处刻意的严格：

    1. **布尔必须与布尔比**。Python 里 `True == 1`，若不拦住，
       `ip_is_proxy eq 1` 会被判命中——而 §15 明确要求布尔特征写 `eq true`。
       这种"能跑通但口径不明"的规则迟早会被误改（有人以为是数值特征，改成
       `gte 1`，于是布尔与数值两套语义在同一字段上打架）。
    2. **数值才与数值比**。`"5" == 5` 在 JSON 里是两种类型，把它们当相等会让
       `user_level eq 5` 这类笔误静默生效或静默失效；直接返回不相等更可预测。
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _num(value: Any) -> Optional[float]:
    """把值取成数值；**布尔不算数值**，取不到返回 `None`。

    布尔排除掉是必需的：`True` 在 Python 里就是 `1`，若允许它参与 `gt/gte`，
    `ip_is_proxy gte 1` 会命中——一条把布尔特征当计数用的规则，作者大概率
    想写的是别的东西，静默命中比报错危险。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def compare(op: Op, actual: Any, expected: Any) -> bool:
    """单个叶节点的比较（BR-05-11 + BR-05-12 的全部语义都在这里）。"""
    if op is Op.EXISTS:
        # 唯一的例外：它问的就是"有没有值"，因此缺失是它**能回答**的问题
        return actual is not None

    if actual is None:
        # BR-05-12：除 exists 外，缺失一律 false，不抛异常、不计分
        return False

    if op is Op.EQ:
        return _eq(actual, expected)
    if op is Op.NE:
        return not _eq(actual, expected)

    if op in (Op.GT, Op.GTE, Op.LT, Op.LTE):
        left, right = _num(actual), _num(expected)
        if left is None or right is None:
            return False
        if op is Op.GT:
            return left > right
        if op is Op.GTE:
            return left >= right
        if op is Op.LT:
            return left < right
        return left <= right

    if op in (Op.IN, Op.NOT_IN):
        if not isinstance(expected, (list, tuple, set)):
            # 右侧不是数组 → 这棵树本身有问题。求值期**不抛异常**（BR-05-13 的
            # 失败隔离在规则层做），只判不命中；结构层由 06 保存时拦。
            return False
        contained = any(_eq(actual, item) for item in expected)
        return contained if op is Op.IN else not contained

    if op is Op.CONTAINS:
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        return expected in actual

    # 到达这里说明 `op` 取值域已变（枚举新增成员却没补语义）——绝不静默放过
    raise ValueError(f"未知算子：{op!r}")


# ============================================================
# 逐叶求值明细（供 `reason` 与 `matched_facts` 使用）
# ============================================================
@dataclass(frozen=True)
class LeafFact:
    """一个叶节点本次的求值事实。"""

    field: str
    op: Op
    expected: Any
    actual: Any
    matched: bool

    @property
    def missing(self) -> bool:
        """该字段本次**没有值**（与"值为 0/False"严格区分）。"""
        return self.actual is None


def collect(tree: Any, features: dict[str, Any]) -> list[LeafFact]:
    """按**深度优先**顺序对每个叶节点做一次比较（不做短路）。

    为什么不短路：本函数的产物要用于生成 `reason` 与 `matched_facts`，
    而"为什么这条规则命中"的解释必须来自**真正成立的那些叶子**。
    若按 `and` 短路，一旦第一个叶子不成立就停止，我们就无法解释
    "这条规则为什么没命中"（BR-05-13 的告警文案与 trace 都要用到）。
    `evaluate_tree()` 自己会短路，性能不受影响。

    `tree` 可以是已解析的 `ConditionNode` 或原始 JSON（`dict`）——
    与 `evaluate()` 一样接受两种形态，非法结构抛 `RUL-4002`。
    """
    root = validate_tree(tree)
    facts: list[LeafFact] = []

    def walk(node: ConditionNode) -> None:
        if node.is_branch:
            for child in node.children or ():
                walk(child)
            return
        assert node.op is not None and node.field is not None  # 由 validate_tree 保证
        actual = features.get(node.field)
        facts.append(LeafFact(
            field=node.field,
            op=node.op,
            expected=node.value,
            actual=actual,
            matched=compare(node.op, actual, node.value),
        ))

    walk(root)
    return facts


def evaluate_tree(tree: ConditionNode, features: dict[str, Any]) -> bool:
    """递归求值（BR-05-10），`and`/`or` 短路。

    | 节点 | 语义 |
    |---|---|
    | 叶 | `compare(op, features.get(field), value)` |
    | `and` | 全部子节点为真 |
    | `or` | 任一子节点为真 |

    `features` 用 `dict.get` 取：**缺失与"键不存在"等价**。04 的快照只放
    算得出来的项（算不出来的进 `missing_features`），因此"键不存在"就是
    "这次没有这个特征"，不该补默认值。
    """
    if tree.is_branch:
        children = tree.children or ()
        if tree.logic is not None and tree.logic.value == "or":
            return any(evaluate_tree(child, features) for child in children)
        return all(evaluate_tree(child, features) for child in children)
    assert tree.op is not None and tree.field is not None  # 由 validate_tree 保证
    return compare(tree.op, features.get(tree.field), tree.value)


def evaluate(tree: Any, features: dict[str, Any]) -> bool:
    """Spec §6 冻结的入口：`evaluate(tree, features) -> bool`。

    接受 `dict`（库里读出来的原始 JSON）或已解析的 `ConditionNode`。
    **非法结构在这里抛 `RUL-4002`**，调用方（`rule_engine`）负责按 BR-05-13
    把它降级成"该规则求值失败、其余规则继续"。
    """
    return evaluate_tree(validate_tree(tree), features)


# ============================================================
# 命中解释（BR-05-22：reason 必须含实际特征值）
# ============================================================
def matched_facts(facts: list[LeafFact]) -> dict[str, Any]:
    """命中叶子涉及的**实际特征值**（E04 的 `matched_facts`）。

    只收命中项而不是全部叶子：这张表是"这条规则凭什么扣了分"的证据，
    把未命中的叶子也写进去会让复核者以为它们也是判据。
    """
    out: dict[str, Any] = {}
    for fact in facts:
        if fact.matched:
            out[fact.field] = fact.actual
    return out


def _format_term(fact: LeafFact) -> str:
    """一条命中事实的人话（Spec §2.2：`近1h同设备下单 12 次 ≥ 阈值 5`）。"""
    label = FEATURE_LABELS.get(fact.field, fact.field)
    if fact.op is Op.EXISTS:
        return f"{label} 有值（实际 {_render(fact.actual)}）"
    return (
        f"{label} {_render(fact.actual)} {_OP_SYMBOL.get(fact.op, fact.op.value)}"
        f" 阈值 {_render(fact.expected)}"
    )


def _render(value: Any) -> str:
    """把特征值渲染成人话：布尔按 `true`/`false`（与 JSON、与规则写法一致）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (list, tuple)):
        return "[" + "、".join(_render(v) for v in value) + "]"
    return str(value)


def describe(facts: list[LeafFact]) -> str:
    """由命中事实拼出 `reason`（**必须带实际数值**，BR-05-22）。

    为什么从"命中的叶子"生成而不是渲染整棵树：审核员要看的是**为什么扣分**，
    把未命中的分支一起写进 `reason` 会让"哪条才是判据"变得含糊；而整棵树的
    文本在嵌套三层时会长到无法在明细表里读完。
    """
    hits = [f for f in facts if f.matched]
    if not hits:
        return "命中（无叶子事实可说明）"
    terms = [_format_term(f) for f in hits[:_MAX_REASON_TERMS]]
    text = "；".join(terms)
    if len(hits) > _MAX_REASON_TERMS:
        text += f"；等共 {len(hits)} 项条件同时成立"
    return text


__all__ = [
    "LeafFact",
    "collect",
    "compare",
    "describe",
    "evaluate",
    "evaluate_tree",
    "matched_facts",
]
