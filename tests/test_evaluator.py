# -*- coding: utf-8 -*-
"""条件树结构与求值语义（BR-05-10 ~ 05-12，V-05-06 的单测面）。

## 这个文件要抓的三类真实缺陷

1. **把缺失当 0**：`device_user_cnt gte 5` 在字段缺失时若命中，就等于把
   "我们不知道这台设备关联了几个账号"读成"这台设备很干净"；
2. **`ne` 在缺失时为真**：`ip_is_proxy ne true` 会对所有没画像的用户命中并加分，
   方向与风控目标正好相反；
3. **布尔/字符串参与数值比较**：Python 里 `True == 1`、`"5" != 5`，
   不做区分就会出现"规则看似配好了、实际永远不命中或永远命中"。

三类都是**静默**的：没有异常、没有日志，只有结论悄悄错掉。因此这里的断言
逐算子、逐取值类型写全，而不是抽查两个。
"""
from __future__ import annotations

import pytest

from app.engine.condition import ConditionNode, Logic, Op, validate_tree
from app.engine.evaluator import collect, describe, evaluate, matched_facts
from app.errors import InvalidConditionTreeError

pytestmark = pytest.mark.anyio


# ============================================================
# validate_tree：合法结构
# ============================================================
async def test_validate_tree_accepts_leaf_branch_and_nesting():
    """叶 / 枝 / 三层嵌套都必须是合法结构（AD-07）。"""
    tree = validate_tree({
        "logic": "and",
        "children": [
            {"field": "device_user_cnt", "op": "gte", "value": 5},
            {"logic": "or", "children": [
                {"field": "ip_is_proxy", "op": "eq", "value": True},
                {"field": "user_level", "op": "in", "value": ["normal", "silver"]},
            ]},
        ],
    })
    assert tree.is_branch and tree.logic is Logic.AND
    assert len(tree.children or []) == 2
    inner = (tree.children or [])[1]
    assert inner.logic is Logic.OR and len(inner.children or []) == 2


async def test_validate_tree_accepts_exists_without_value():
    """`exists` 的合法写法是**完全没有 `value` 这个键**（Spec §15）。"""
    node = validate_tree({"field": "device_user_cnt", "op": "exists"})
    assert node.op is Op.EXISTS
    assert node.value is None


async def test_validate_tree_is_idempotent():
    """已解析的节点原样返回（求值热路径上不重复解析）。"""
    node = validate_tree({"field": "order_cnt_1h", "op": "gte", "value": 3})
    assert isinstance(node, ConditionNode)
    assert validate_tree(node) is node


# ============================================================
# validate_tree：非法结构必须**真拒绝**（RUL-4002 / 422）
# ============================================================
BAD_TREES: tuple[tuple[str, object], ...] = (
    ("不是对象", ["field", "op"]),
    ("空对象（既不是叶也不是枝）", {}),
    ("缺 field", {"op": "gte", "value": 5}),
    ("field 为空串", {"field": "  ", "op": "gte", "value": 5}),
    ("缺 op 与 logic", {"field": "x"}),
    ("不支持的算子", {"field": "x", "op": "between", "value": 5}),
    ("非 exists 缺 value", {"field": "x", "op": "gte"}),
    ("exists 带了 value", {"field": "x", "op": "exists", "value": None}),
    ("空的 children", {"logic": "and", "children": []}),
    ("缺 children", {"logic": "or"}),
    ("逻辑取值非法", {"logic": "xor", "children": [{"field": "x", "op": "gte", "value": 1}]}),
    ("枝节点带 field", {"logic": "and", "children": [{"field": "x", "op": "gte", "value": 1}],
                        "field": "y"}),
    ("叶节点带 children", {"field": "x", "op": "gte", "value": 1,
                           "children": [{"field": "y", "op": "gte", "value": 1}]}),
    ("叶节点既带 op 又带 logic", {"field": "x", "op": "gte", "value": 1, "logic": "and",
                                  "children": [{"field": "y", "op": "gte", "value": 1}]}),
    ("多余键（拼写错误）", {"field": "x", "op": "gte", "vale": 1, "value": 1}),
    ("嵌套层非法", {"logic": "and", "children": [{"field": "x", "op": "nope", "value": 1}]}),
)


@pytest.mark.parametrize("label,tree", BAD_TREES, ids=[c[0] for c in BAD_TREES])
async def test_validate_tree_rejects_invalid_structures(label, tree):
    """非法结构一律抛 `RUL-4002`，且错误码/状态码正确（Spec §5）。

    为什么必须逐条拒绝而不是"宽容解析"：一棵读不懂的树若被放行，规则会
    静默变成"恒不命中"，而配置页上看起来一切正常——这类缺陷在生产上要靠
    对比命中数才能发现。
    """
    with pytest.raises(InvalidConditionTreeError) as exc:
        validate_tree(tree)
    assert exc.value.code == "RUL-4002"
    assert exc.value.http_status == 422
    assert "条件树结构非法" in exc.value.message
    assert label  # 用例 id 已由 parametrize 给出，这里只为让参数被使用


async def test_validate_tree_reports_node_path_for_nested_error():
    """错误信息必须指出**是哪一层的哪个键**（Spec §5 的「第 N 个节点不合法」）。"""
    with pytest.raises(InvalidConditionTreeError) as exc:
        validate_tree({"logic": "and", "children": [
            {"field": "ok", "op": "gte", "value": 1},
            {"field": "bad", "op": "gte"},
        ]})
    assert "children[1]" in exc.value.message, exc.value.message
    assert exc.value.data["node_path"] == "children[1]"


# ============================================================
# BR-05-11：算子语义
# ============================================================
CASES: tuple[tuple[str, dict, dict, bool], ...] = (
    # --- eq / ne：跨类型不静默相等 ---
    ("eq 整数命中", {"field": "n", "op": "eq", "value": 3}, {"n": 3}, True),
    ("eq 整数不命中", {"field": "n", "op": "eq", "value": 3}, {"n": 4}, False),
    ("eq 字符串命中", {"field": "s", "op": "eq", "value": "normal"}, {"s": "normal"}, True),
    ("eq 布尔命中", {"field": "b", "op": "eq", "value": True}, {"b": True}, True),
    ("eq 布尔不因 1==True 而命中", {"field": "b", "op": "eq", "value": 1}, {"b": True}, False),
    ("eq 数字串不等于数字", {"field": "s", "op": "eq", "value": 5}, {"s": "5"}, False),
    ("ne 命中", {"field": "n", "op": "ne", "value": 3}, {"n": 4}, True),
    ("ne 不命中", {"field": "n", "op": "ne", "value": 3}, {"n": 3}, False),
    # --- 数值比较：仅数值 ---
    ("gt", {"field": "n", "op": "gt", "value": 3}, {"n": 4}, True),
    ("gt 边界不命中", {"field": "n", "op": "gt", "value": 3}, {"n": 3}, False),
    ("gte 边界命中", {"field": "n", "op": "gte", "value": 3}, {"n": 3}, True),
    ("lt 浮点", {"field": "f", "op": "lt", "value": 24}, {"f": 23.5}, True),
    ("lte 边界", {"field": "f", "op": "lte", "value": 0.5}, {"f": 0.5}, True),
    ("数值算子对字符串返回 False", {"field": "s", "op": "gte", "value": 1}, {"s": "5"}, False),
    ("数值算子对布尔返回 False", {"field": "b", "op": "gte", "value": 1}, {"b": True}, False),
    # --- in / not_in：右侧数组 ---
    ("in 命中", {"field": "s", "op": "in", "value": ["normal", "silver"]}, {"s": "silver"}, True),
    ("in 不命中", {"field": "s", "op": "in", "value": ["vip"]}, {"s": "normal"}, False),
    ("not_in 命中", {"field": "s", "op": "not_in", "value": ["vip", "gold"]},
     {"s": "normal"}, True),
    ("右侧不是数组一律不命中", {"field": "s", "op": "in", "value": "normal"},
     {"s": "normal"}, False),
    # --- exists：唯一能回答"缺没缺"的算子 ---
    ("exists 有值", {"field": "n", "op": "exists"}, {"n": 0}, True),
    ("exists 无值", {"field": "n", "op": "exists"}, {}, False),
    # --- contains：仅字符串 ---
    ("contains 命中", {"field": "s", "op": "contains", "value": "il"}, {"s": "silver"}, True),
    ("contains 不命中", {"field": "s", "op": "contains", "value": "zz"}, {"s": "silver"}, False),
    ("contains 对数字返回 False", {"field": "n", "op": "contains", "value": "1"},
     {"n": 123}, False),
)


@pytest.mark.parametrize("label,tree,features,expected", CASES, ids=[c[0] for c in CASES])
async def test_operator_semantics(label, tree, features, expected):
    """BR-05-11 的逐算子语义（含跨类型不相等这两条容易写错的分支）。"""
    assert evaluate(tree, features) is expected, label


NULL_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains")


@pytest.mark.parametrize("op", NULL_OPS)
async def test_missing_feature_returns_false_for_every_operator_except_exists(op):
    """BR-05-12：特征缺失时除 `exists` 外**一律 false**，不抛异常、不计分。

    `ne` / `not_in` 也返回 false 是刻意的（见 `evaluator` 的模块说明）：
    若它们在缺失时为真，`ip_is_proxy ne true` 这类规则会对所有没画像的用户
    命中并加分——把"不知道他是不是代理"当成"他肯定不是代理"。
    """
    tree = {"field": "ip_is_proxy", "op": op, "value": True if op in ("eq", "ne") else [1]}
    assert evaluate(tree, {}) is False
    # 键存在但值为 null 与"键不存在"等价（04 只把算得出来的项放进 features）
    assert evaluate(tree, {"ip_is_proxy": None}) is False


async def test_null_value_and_missing_key_are_equivalent():
    """`features` 里显式的 `None` 与键缺失必须同义（都表示"没算出来"）。"""
    tree = {"field": "device_user_cnt", "op": "gte", "value": 5}
    assert evaluate(tree, {}) is evaluate(tree, {"device_user_cnt": None}) is False


# ============================================================
# and / or 递归（BR-05-10）
# ============================================================
async def test_and_or_nesting_is_evaluated_recursively():
    tree = {"logic": "and", "children": [
        {"field": "a", "op": "gte", "value": 1},
        {"logic": "or", "children": [
            {"field": "b", "op": "eq", "value": True},
            {"field": "c", "op": "gte", "value": 10},
        ]},
    ]}
    assert evaluate(tree, {"a": 1, "b": True, "c": 0}) is True
    assert evaluate(tree, {"a": 1, "b": False, "c": 10}) is True
    assert evaluate(tree, {"a": 1, "b": False, "c": 9}) is False
    assert evaluate(tree, {"a": 0, "b": True, "c": 99}) is False


# ============================================================
# BR-05-22：reason 必须含实际特征值
# ============================================================
async def test_describe_reports_actual_feature_values():
    """`reason` 要含**具体数值**（V-05-11 的纯函数面）。"""
    tree = {"logic": "and", "children": [
        {"field": "device_user_cnt", "op": "gte", "value": 5},
        {"field": "ip_is_proxy", "op": "eq", "value": True},
    ]}
    facts = collect(tree, {"device_user_cnt": 12, "ip_is_proxy": True})
    text = describe(facts)
    assert "12" in text and "5" in text, text
    assert "true" in text, "布尔按 true/false 渲染（与 JSON、与规则写法一致）"
    facts_map = matched_facts(facts)
    assert facts_map == {"device_user_cnt": 12, "ip_is_proxy": True}


async def test_matched_facts_excludes_unmatched_leaves():
    """`matched_facts` 只收命中项：它是"凭什么扣分"的证据，不是全部入参。"""
    tree = {"logic": "or", "children": [
        {"field": "a", "op": "gte", "value": 1},
        {"field": "b", "op": "gte", "value": 1},
    ]}
    facts = collect(tree, {"a": 1, "b": 0})
    assert matched_facts(facts) == {"a": 1}


async def test_collect_visits_every_leaf_without_short_circuit():
    """`collect` 不做短路：解释"为什么没命中"需要全部叶子的事实。"""
    tree = {"logic": "and", "children": [
        {"field": "a", "op": "gte", "value": 9},
        {"field": "b", "op": "gte", "value": 1},
    ]}
    facts = collect(tree, {"a": 1, "b": 1})
    assert len(facts) == 2
    assert [f.matched for f in facts] == [False, True]
    assert facts[0].missing is False        # 有值但不满足，与"缺失"是两回事


async def test_evaluate_rejects_invalid_tree_at_evaluation_time():
    """求值入口对非法树同样抛 `RUL-4002`（由规则层降级为该条规则失败）。"""
    with pytest.raises(InvalidConditionTreeError):
        evaluate({"field": "x", "op": "nope", "value": 1}, {})
