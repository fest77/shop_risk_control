# -*- coding: utf-8 -*-
"""条件树（AD-07）：结构定义、形状校验与 `validate_tree()`（模块 05 §6）。

## 谁拥有"条件树长什么样"

Spec §1 的「本模块明确不做的事」里写着：「**不做条件树的结构校验实现**——
`validate_tree()` 由本模块提供，06 只调用」。因此"有哪些键、`op` 取值域、
`exists` 能不能带 `value`"这些**只在这里定义一次**：模块 06 的规则编辑器在
保存前调用 `validate_tree()`，模块 05 的求值器直接吃它的产物。

## 结构（AD-07 / E05.condition）

    非叶节点：{"logic": "and"|"or", "children": [节点, ...]}
    叶节点：  {"field": "<特征键>", "op": "<算子>", "value": <任意>}
    `exists`   **不接受 `value`**

## 为什么用「单模型 + 形状校验」而不是 `Union[Leaf, Branch]`

两种形状的键名完全不重叠（`logic`/`children` vs `field`/`op`/`value`），用 Union
时"两边都不匹配"会变成 pydantic 的联合体错误（`union_tag_invalid` 一类），
而 06 要把它渲染成「条件树第 N 个节点不合法：缺少 field」这种**能直接指导修改**
的文案。单模型自己判形状，错误文案由我们负责，对 06、对测试都更可控。

## 为什么 `extra="forbid"`（拒绝多余键）

多出来的键**一定**是拼写错误或编辑器版本漂移（`feild`、`ope`、`vale`）。
静默忽略的后果是：规则保存成功、页面上看起来配了这条条件，而求值器读不到它
→ 条件恒不成立 → 规则**永不命中**。"看起来配好了却从不生效"的规则在风控里
比一条阈值写错的规则更危险，因为它没有任何报错。因此宁可保存时就报错。

## `validate_tree()` 不校验"特征键是否存在"

`field` 只要求是非空字符串，**不在这里**比对 E02 的 18 项特征键。理由：
① 该比对属于"规则配置质量"而不是"结构合法性"，混进来会让 `RUL-4002` 的
语义失焦（它描述的是"读不懂这棵树"，不是"这棵树引用了不存在的特征"）；
② `rules` 是数据，未来增设特征时旧规则不该突然变成"非法"。
求值侧的表现是"该字段取不到值 → 条件不成立"（BR-05-12），审计侧由 06 的
配置页负责提示。18 项键的权威清单见 `app/engine/feature_compute.py::FEATURE_KEYS`。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.enums import ConditionOp
from app.errors import InvalidConditionTreeError

#: 算子枚举。**复用 `app/enums.ConditionOp`**（BR-00-18：枚举只有一处真源，
#: 且该取值域已由 E05 定稿并下发给前端）。这里只给一个更短的别名。
Op = ConditionOp

#: 逻辑节点允许出现的键 / 叶子节点允许出现的键（错误文案与校验共用同一份常量）
BRANCH_KEYS: tuple[str, ...] = ("logic", "children")
LEAF_KEYS: tuple[str, ...] = ("field", "op", "value")


class Logic(str, Enum):
    """非叶节点的组合逻辑（AD-07：只有 `and` / `or`）。"""

    AND = "and"
    OR = "or"


class ConditionNode(BaseModel):
    """条件树的一个节点（叶或非叶，二者**互斥**）。

    `value` 用 `Any`：E05 没有限定比较值的类型（`in` 的右侧是数组、`eq` 的右侧
    可能是字符串/数值/布尔）。类型语义由**算子**决定（BR-05-11），见
    `app/engine/evaluator.py`。
    """

    model_config = ConfigDict(extra="forbid")

    # --- 非叶节点 ---
    logic: Optional[Logic] = None
    children: Optional[list["ConditionNode"]] = None
    # --- 叶节点 ---
    field: Optional[str] = None
    op: Optional[Op] = None
    value: Any = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ConditionNode":
        """判定"这是叶还是枝"，并拒绝两者混用/都不像的节点。

        这里**必须**用 `model_fields_set` 而不是"`value is not None`"来判
        "调用方有没有给 `value`"：`exists` 的合法写法是**完全没有** `value` 这个键，
        而 `{"value": null}` 是另一个意思（等于拿 `null` 去比较，本身就矛盾）。
        两者的区别只存在于"键在不在"，不存在于"值是什么"。
        """
        has_logic = self.logic is not None
        has_op = self.op is not None

        if has_logic and has_op:
            raise ValueError(
                f"同一节点不能既含 {list(BRANCH_KEYS)} 又含 {list(LEAF_KEYS)}，"
                "请拆成父子两层"
            )
        if not has_logic and not has_op:
            raise ValueError(
                f"节点必须是非叶节点（{list(BRANCH_KEYS)}）或叶节点（{list(LEAF_KEYS)}）之一"
            )

        if has_logic:
            if not self.children:
                # 空 children 的 `and` 恒真、`or` 恒假——两者都是"作者以为写了条件、
                # 实际什么都没判"，必须拒绝而不是各自取数学上的默认值。
                raise ValueError("非叶节点必须含非空 children")
            if self.field is not None or "value" in self.model_fields_set:
                raise ValueError(f"非叶节点不得含 {list(LEAF_KEYS)}")
            return self

        # ---- 叶子节点 ----
        if self.children is not None:
            raise ValueError("叶节点不得含 children")
        if not self.field or not str(self.field).strip():
            raise ValueError("叶节点必须含非空 field（特征键）")
        supplied = "value" in self.model_fields_set
        if self.op is Op.EXISTS:
            if supplied:
                raise ValueError(
                    "exists 不接受 value（它问的是「有没有值」，再给一个比较值自相矛盾）"
                )
        elif not supplied:
            raise ValueError(f"算子 {self.op.value} 必须提供 value")
        return self

    # ---------- 便于求值器与 06 使用的只读属性 ----------
    @property
    def is_branch(self) -> bool:
        return self.logic is not None


def _path_of(location: tuple[Any, ...]) -> str:
    """把 pydantic 的 `loc` 渲染成 `children[0].op` 这种能照着改的路径。

    Spec §5 给 `RUL-4002` 定的用户可见提示是「条件树第 N 个节点不合法」——
    要让那句话有用，就必须说清**是哪一层**的哪个键：只有 `children` 是列表，
    因此下标前用 `[]`、其余用 `.`。
    """
    out = ""
    for part in location:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _describe(error: ValidationError) -> tuple[str, str]:
    """取第一个错误的（可读原因, 节点路径）。

    只取第一个而不是拼接全部：条件树嵌套出错时，同一处笔误会在每一层重复出现，
    列 20 条同类错误反而让人找不到要改的那一行。

    `enum` 类型的错误（算子取值不在 `ConditionOp` 里）会被换成中文前缀——
    pydantic 的原文是一句英文（`Input should be 'eq', ...`），而这条文案会
    经 `RUL-4002` 直接显示在 06 的规则编辑页上。英文里夹杂算子取值域读起来
    像内部报错，中文前缀 + 原始取值域才是可照做的提示。
    """
    first = error.errors()[0]
    location = tuple(first.get("loc") or ())
    message = str(first.get("msg") or "结构不合法")
    if first.get("type") == "enum":
        message = f"算子不支持（{message}）"
    # pydantic 的 ValueError 文案前面会带 "Value error, " 前缀，去掉它才像人话
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            message = message[len(prefix):]
    return message, _path_of(location)


def validate_tree(raw: Any) -> ConditionNode:
    """把 `rules.condition` 的原始 JSON 解析成条件树，非法即抛 `RUL-4002`。

    | 入参 | 行为 |
    |---|---|
    | `ConditionNode` | 原样返回（幂等：求值热路径上不会重复解析） |
    | `dict` | 解析 + 形状校验 |
    | 其他 | `RUL-4002`（条件树必须是 JSON 对象） |

    **这是模块 06 保存规则的唯一校验入口**：06 不得自己实现一份结构校验
    （Spec §1），否则两处规则必然漂移，而漂移的表现是"保存时通过、决策时
    被当成求值失败跳过"——一条配好了却永不生效的规则。
    """
    if isinstance(raw, ConditionNode):
        return raw
    if not isinstance(raw, dict):
        raise InvalidConditionTreeError(
            f"条件树必须是 JSON 对象，收到 {type(raw).__name__}"
        )
    try:
        return ConditionNode.model_validate(raw)
    except ValidationError as e:
        message, path = _describe(e)
        raise InvalidConditionTreeError(message, path) from e


def logic_label(logic: Optional[Logic]) -> str:
    """`and`/`or` 的中文说明（供 `reason` 文案与 06 的编辑器复用）。"""
    return {Logic.AND: "且", Logic.OR: "或"}.get(logic, "且")


__all__ = [
    "BRANCH_KEYS",
    "LEAF_KEYS",
    "ConditionNode",
    "Logic",
    "Op",
    "logic_label",
    "validate_tree",
]
