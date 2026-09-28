# -*- coding: utf-8 -*-
"""规则的请求 / 响应模型与展示辅助（模块 06-B，对齐 E05 与 Spec 06 §3.1）。

## 模型层为什么"宽严分明"

| 字段 | 位置 | 理由 |
|---|---|---|
| `name` / `description` 长度、纯空白 | **模型层**（→ `COM-4001`） | 纯格式问题，不属于任何业务规则；与 06-A 的名单请求模型同一取舍 |
| `status` 取值域 | **模型层**（`Literal` → `COM-4001`） | 同上：它是"枚举写错了"，Spec §5.1 也没给它分配 `CFG-` 码 |
| `score` 范围 | **服务层**（→ `CFG-4004`） | `BR-06-06` 明确定义为业务规则且有专属码；若写进 `Field(ge=0, le=100)`，越界会先被 Pydantic 拦成 `COM-4001`，契约要求的 `CFG-4004` 就永远拿不到（V-06-06 会失败） |
| `scene_code` 存在性 | **服务层**（→ `CFG-4013`） | `BR-06-07`：必须查 E06 字典，属业务校验；且 D24 要求数据驱动，不能在模型里写死场景枚举 |
| `condition` 结构 | **服务层转调 05 的 `validate_tree()`**（→ `CFG-4003`） | `BR-06-17`：本模块**不得**实现第二套结构校验。模型层因此把 `condition` 声明为 `Any`，完整地交给 05 |

`condition: Any` 是刻意的：若声明成 `dict`，一棵不是对象的树（如 `[]`）会被
Pydantic 先拦成 `COM-4001`，而 Spec 要求它是条件树问题（`CFG-4003`）。

## 不可变字段（`CFG-4007`）

`_id` / `is_system` / `created_*` / `version` 由服务端独占。**不能用
`extra="forbid"` 来表达**：那会把它们变成 `COM-4001`（422），而 Spec §3.1 的
PUT 状态码栏明确要求"传了 `_id` 直接 `400 CFG-4007`"。因此模型对未知键的处理
是"透传不进模型"（由接口层先读原始 JSON 判不可变字段），见 `rule_api` 的实现。
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: E05 `status` 取值域（BR-06-03/04）
RULE_STATUSES: tuple[str, ...] = ("enabled", "disabled")

#: 规则列表可排序字段白名单（Spec §3.1：字段必须在白名单内，否则 `CFG-4008`）。
#: 默认排序 `priority:asc,_id:asc` 与 05 的稳定排序语义（BR-05-14 / BR-06-11）一致：
#: `priority` 小者在前，且**只用于展示与稳定输出顺序，不用于短路**。
SORTABLE_FIELDS: tuple[str, ...] = (
    "_id", "name", "scene_code", "score", "priority", "version", "status",
    "created_at", "updated_at",
)
DEFAULT_SORT = "priority:asc,_id:asc"

#: 分值边界（BR-06-06）。多条规则累加超 100 的截断不在本模块（归 BR-05-16）。
SCORE_MIN = 0
SCORE_MAX = 100
#: BR-06-12 的场景分值合计提示阈值
SCORE_SUM_HINT_LIMIT = 100

RULE_NAME_MAX = 50
RULE_DESCRIPTION_MAX = 200
RULE_KEYWORD_MAX = 50

#: **客户端不得提交**的字段（`CFG-4007`）。`version` 也在内：乐观锁要求客户端
#: 用 `expected_version` **声明**期望值，而不是直接把版本号写进文档（否则
#: "版本递增"就成了客户端说了算，BR-06-03 与 D60 的重放前提同时失效）。
IMMUTABLE_FIELDS: tuple[str, ...] = (
    "_id", "rule_code", "is_system", "version",
    "created_by", "created_at", "updated_by", "updated_at",
    "deleted", "deleted_at", "deleted_by",
)


def _strip_required(v: str) -> str:
    """去首尾空白并拒绝纯空白（`min_length=1` 挡不住 `"   "`）。"""
    s = (v or "").strip()
    if not s:
        raise ValueError("不能为空白")
    return s


class RuleCreate(BaseModel):
    """`POST /api/v1/rules` 请求体（Spec §3.1）。"""

    name: str = Field(..., min_length=1, max_length=RULE_NAME_MAX)
    scene_code: str = Field(..., min_length=1, max_length=32)
    description: Optional[str] = Field(default=None, max_length=RULE_DESCRIPTION_MAX)
    # 见模块 docstring：类型交给 05 的 validate_tree() 判定，本模块不复刻
    condition: Any = Field(..., description="JSON 条件树（AD-07，禁字符串表达式）")
    score: int = Field(..., description="0~100；越界由服务层判为 CFG-4004")
    priority: int = Field(default=10, description="执行优先级，小者在前")
    status: Literal["enabled", "disabled"] = Field(
        default="disabled", description="BR-06-04：默认停用（上线前先仿真）"
    )

    @field_validator("name", "scene_code")
    @classmethod
    def _clean(cls, v: str) -> str:
        return _strip_required(v)

    @field_validator("description")
    @classmethod
    def _clean_optional(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None

    @field_validator("score", "priority", mode="before")
    @classmethod
    def _reject_bool(cls, v: Any) -> Any:
        """布尔值不是分值/优先级。

        Pydantic 的宽松模式会把 `true` 转成 `1`——若不拦，`score: true` 会被
        当成 1 分**静默写进库**，规则分值变成布尔值来源，重放时可解释性尽失。
        这里按"字段格式错误"处理（`COM-4001` 422）：它不是"越界"
        （`CFG-4004` 的语义是"整数值不在 0~100"），只是类型不对。
        """
        if isinstance(v, bool):
            raise ValueError("必须是整数，不能是布尔值")
        return v


class RuleUpdate(BaseModel):
    """`PUT /api/v1/rules/{rule_code}` 请求体。

    `expected_version` 必填（BR-06-05）；其余字段**留空即保持原值**——抽屉里
    用户可能只改分值，若强制全量提交，前端一旦漏带某个字段就会把它清空
    （"改个分值顺手把条件树抹了"是这类接口最经典的破坏方式）。
    """

    expected_version: int = Field(..., description="乐观锁：必须等于库中当前 version")
    name: Optional[str] = Field(default=None, min_length=1, max_length=RULE_NAME_MAX)
    scene_code: Optional[str] = Field(default=None, min_length=1, max_length=32)
    description: Optional[str] = Field(default=None, max_length=RULE_DESCRIPTION_MAX)
    condition: Any = Field(default=None)
    score: Optional[int] = None
    priority: Optional[int] = None
    status: Optional[Literal["enabled", "disabled"]] = None

    @field_validator("name", "scene_code")
    @classmethod
    def _clean_optional(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _strip_required(v)

    @field_validator("description")
    @classmethod
    def _clean_desc(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip() or None

    @field_validator("score", "priority", mode="before")
    @classmethod
    def _reject_bool(cls, v: Any) -> Any:
        """同 `RuleCreate`：布尔值不是整数分值/优先级（见那里的说明）。"""
        if isinstance(v, bool):
            raise ValueError("必须是整数，不能是布尔值")
        return v


class RuleToggleIn(BaseModel):
    """`POST /api/v1/rules/{rule_code}/toggle` 请求体（Spec §3.1）。"""

    status: Literal["enabled", "disabled"]
    expected_version: int
    #: BR-06-09：从 `disabled` 切到 `enabled` 时，页面会先引导去仿真页；服务端
    #: **不强制拦截**（避免阻断演示链路），但审计里要能回答"这次启用是不是带着
    #: 仿真验证来的"。默认 `false` 表示"未标注"，而不是"已验证"。
    sim_verified: bool = False


class ValidateTreeIn(BaseModel):
    """`POST /api/v1/rules/validate-tree` 请求体。"""

    condition: Any = Field(..., description="待校验的 JSON 条件树")


class RuleOut(BaseModel):
    """规则对外表示（字段与 Spec §3.1 的 `items` 契约对齐 + 少量增补）。

    增补说明（如实登记）：`description` 与 `deleted` 不在 §3.1 的 items 列表里，
    但编辑抽屉需要回填说明、删除后的文档也要能被区分。**只增不改**：既有字段的
    名字与含义与契约逐字一致，前端忽略多余字段即可。

    Pydantic v2 把 `_` 前缀字段当私有属性（不进 `model_dump()`），因此规则编码用
    `code` 承载、以 `alias="_id"` 输出，序列化时必须 `by_alias=True`
    （与 `ListEntryOut` 同一处理）。
    """

    model_config = ConfigDict(populate_by_name=True)

    code: str = Field(..., alias="_id")
    name: str
    scene_code: str
    scene_name: Optional[str] = None
    description: Optional[str] = None
    condition: Any = None
    #: 人读条件摘要（Spec §2.2.2 的"条件摘要"列）。**仅用于展示**：
    #: 渲染失败时降级为 `[条件树]`，绝不抛错、也绝不参与任何判定。
    condition_summary: str = ""
    score: int
    priority: int = 10
    version: int = 1
    status: str = "disabled"
    is_system: bool = False
    deleted: bool = False
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    @classmethod
    def from_doc(cls, doc: dict, scene_name: Optional[str] = None) -> "RuleOut":
        return cls(
            code=doc["_id"],
            name=doc.get("name") or "",
            scene_code=doc.get("scene_code") or "",
            scene_name=scene_name,
            description=doc.get("description"),
            condition=doc.get("condition"),
            condition_summary=condition_summary(doc.get("condition")),
            score=int(doc.get("score") or 0),
            priority=int(doc.get("priority") or 0),
            version=int(doc.get("version") or 1),
            status=doc.get("status") or "disabled",
            is_system=bool(doc.get("is_system")),
            deleted=bool(doc.get("deleted")),
            created_by=doc.get("created_by"),
            updated_by=doc.get("updated_by"),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
        )


class ScoreHint(BaseModel):
    """某场景**启用**规则的分值合计（BR-06-12 的非阻断提示）。"""

    scene_code: str
    scene_name: str = ""
    enabled_score_sum: int = 0
    enabled_rule_count: int = 0
    over_limit: bool = False


class RuleQueryResult(BaseModel):
    """`GET /api/v1/rules` 响应体（模块 00 §3.2 的分页契约）。"""

    items: list[RuleOut]
    total: int
    page: int
    page_size: int
    #: total=0 时为 0，前端据此走空态而不是"第 1/1 页"
    pages: int
    as_of: int
    score_hints: list[ScoreHint] = Field(default_factory=list)
    over_limit_scenes: list[str] = Field(default_factory=list)


class RuleDeleteResult(BaseModel):
    """`DELETE /api/v1/rules/{rule_code}` 响应体（软删除，BR-06-10）。"""

    rule_code: str
    name: str
    status: str
    deleted: bool
    version: int
    deleted_at: int
    deleted_by: str
    #: 影响面：历史决策**不受影响**（`decision_hits` 是快照，BR-05-21）
    affected_decisions: int = 0


class RuleImpactOut(BaseModel):
    """删除确认弹窗前的影响面预览（Spec §5.2「删除规则」行）。"""

    rule_code: str
    name: str
    version: int
    status: str
    is_system: bool
    deleted: bool
    hit_count_30d: int
    decision_refs: int
    hint: str


class ValidateTreeOut(BaseModel):
    """`POST /api/v1/rules/validate-tree` 响应体（Spec §3.1）。

    `normalized` 是 **05 归一化后的树**：前端保存时提交它，保证"编辑器里的树"
    与"求值器读到的树"是同一份（少了这层归一化，前端可以提交一棵"字段都在、
    但类型不对"的树，保存时通过、求值时行为不同）。
    """

    valid: bool
    errors: list[dict] = Field(default_factory=list)
    normalized: Optional[dict] = None


# ============================================================
# 规则批量导入（Spec §3.1 `POST /rules/import`，§8 新增-7）
# ============================================================
#: 导入模板的列（**只在这里出现一次**）：模板下载与解析共用同一元组，
#: 因此"模板给的表头"与"导入认的表头"在物理上不可能漂移——CFG-4012
#: 最常见的成因就是两处各写一份字面量。
#:
#: `rule_code` 可留空（§8 新增-7：留空则服务端按 BR-06-01 生成）。
#: `condition` 列放**条件树的 JSON 文本**（AD-07 禁字符串表达式指的是
#: "不要把整条规则写成表达式"，而不是"条件树不能出现在 CSV 里"；
#: 该列的内容会被解析成 JSON 结构后再交给 05 校验）。
RULE_IMPORT_HEADER: tuple[str, ...] = (
    "rule_code", "name", "scene_code", "description", "condition",
    "score", "priority", "status",
)

#: 单次导入行数上限（Spec §3.1：≤500 行）。不含表头。
RULE_IMPORT_MAX_ROWS = 500
#: 错误行明细回传上限（与名单导入同口径）：超出只回传前 N 条。
RULE_IMPORT_ERROR_LIMIT = 200
RULE_IMPORT_MODES = ("partial", "atomic")

#: 导入时可显式指定的规则编码格式（BR-06-01 的通用形态 `R{...}{3位}`）。
#: **只校验格式、不强制与 `scene_code` 的前缀一致**：`PUT` 允许改场景而编码
#: 不可改（BR-06-02），因此"编码前缀与场景不一致"本来就是合法状态。
RULE_CODE_PATTERN = re.compile(r"^R[A-Z0-9_]{2,20}\d{3}$")


class RuleImportFailRow(BaseModel):
    """一行导入失败明细（`row` 为**文件内真实行号**，表头是第 1 行）。"""

    row: int
    rule_code: str = ""
    reason: str


class RuleImportResult(BaseModel):
    """`POST /rules/import` 响应体（Spec §3.1：总数/成功/失败 + 错误明细）。"""

    total: int
    success: int
    failed: int
    mode: str
    rows: list[RuleImportFailRow] = Field(default_factory=list)
    imported_ids: list[str] = Field(default_factory=list)
    #: 错误明细被截断时为 True，页面提示"下载错误明细"
    rows_truncated: bool = False


# ============================================================
# 辅助
# ============================================================
#: 算子 -> 展示符号（仅用于条件摘要）。取值域与 05 的 `condition_op` 枚举一致，
#: 但这张表**不参与校验**——未知算子会原样显示，不会报错（校验归 05）。
_OP_SYMBOLS: dict[str, str] = {
    "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
    "in": "∈", "not_in": "∉", "exists": "存在", "contains": "包含",
}
_LOGIC_LABELS: dict[str, str] = {"and": "AND", "or": "OR"}
#: 摘要的最大深度与最大长度：坏数据（人工改库/历史遗留）不能让列表页渲染卡住。
#: 超限即降级为 `[条件树]` 而不是继续拼——这是"展示可用性"，不是校验。
_SUMMARY_MAX_DEPTH = 12
_SUMMARY_MAX_LEN = 300
_FALLBACK_SUMMARY = "[条件树]"


def _leaf_summary(node: dict) -> str:
    field = str(node.get("field") or "")
    op = str(node.get("op") or "")
    symbol = _OP_SYMBOLS.get(op, op or "?")
    if op == "exists":
        return f"{field} {symbol}"
    value = node.get("value")
    if isinstance(value, (list, tuple)):
        rendered = "[" + ", ".join(_render_value(v) for v in value) + "]"
    else:
        rendered = _render_value(value)
    return f"{field} {symbol} {rendered}"


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # 只给**含空格或空串**的值加引号：`139****0001 = x` 这种已经够清楚，
        # 全加引号只会让摘要变长（列表列宽有限）。
        return f'"{value}"' if (value == "" or " " in value) else value
    if value is None:
        return "null"
    return str(value)


def _node_summary(node: Any, depth: int) -> str:
    if depth > _SUMMARY_MAX_DEPTH or not isinstance(node, dict):
        raise ValueError("条件树超出可渲染范围")
    if "logic" in node or "children" in node:
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("非叶节点缺少 children")
        joined = f" {_LOGIC_LABELS.get(str(node.get('logic')), str(node.get('logic')))} ".join(
            _node_summary(child, depth + 1) for child in children
        )
        # 只在"组里还有组"时加括号，避免根节点被无谓地包一层
        return f"({joined})" if any(isinstance(c, dict) and "children" in c for c in children) else joined
    return _leaf_summary(node)


def condition_summary(condition: Any) -> str:
    """把条件树渲染成人读摘要（Spec §2.2.2 的"条件摘要"列）。

    **这不是校验的第二个实现**（BR-06-17 禁止的是结构校验）：它只做"能拼就拼、
    拼不出就降级"，任何异常/超限都返回 `[条件树]`，与页面上"渲染失败时降级显示
    `[条件树]` 并给出「查看 JSON」"的要求一致。真正的合法性判定唯一的入口是
    05 的 `validate_tree()`。
    """
    try:
        text = _node_summary(condition, 0)
    except Exception:  # noqa: BLE001 - 展示降级，绝不因一条坏数据打断整个列表
        return _FALLBACK_SUMMARY
    if not text or len(text) > _SUMMARY_MAX_LEN:
        return _FALLBACK_SUMMARY
    return text


def parse_rule_sort(sort: str) -> list[tuple[str, int]]:
    """解析 `字段:asc|desc`（逗号分隔多级）；非法字段抛 `ValueError` 由服务层转 `CFG-4008`。

    与 `list_schema.parse_sort` 同形但**白名单不同**：规则列表默认按
    `priority asc, _id asc`（BR-06-11），而名单默认按 `effective_at desc`。
    两处各写一份是因为"可排序字段"本就是各列表自己的契约；共用一份会迫使
    一个列表接受另一个列表的字段。
    """
    pairs: list[tuple[str, int]] = []
    for token in (sort or "").split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            field, _, direction = token.partition(":")
        else:
            field, direction = token, "asc"
        field, direction = field.strip(), direction.strip().lower()
        if field not in SORTABLE_FIELDS:
            raise ValueError(f"排序字段不在白名单内：{field}")
        if direction not in ("asc", "desc"):
            raise ValueError(f"排序方向非法：{direction}")
        pairs.append((field, 1 if direction == "asc" else -1))
    if not pairs:
        pairs = [("priority", 1), ("_id", 1)]
    return pairs


__all__ = [
    "RULE_STATUSES", "SORTABLE_FIELDS", "DEFAULT_SORT", "SCORE_MIN", "SCORE_MAX",
    "SCORE_SUM_HINT_LIMIT", "RULE_NAME_MAX", "RULE_DESCRIPTION_MAX", "RULE_KEYWORD_MAX",
    "IMMUTABLE_FIELDS", "RULE_IMPORT_HEADER", "RULE_IMPORT_MAX_ROWS",
    "RULE_IMPORT_ERROR_LIMIT", "RULE_IMPORT_MODES", "RULE_CODE_PATTERN",
    "RuleCreate", "RuleUpdate", "RuleToggleIn", "ValidateTreeIn", "RuleOut",
    "ScoreHint", "RuleQueryResult", "RuleDeleteResult", "RuleImpactOut",
    "ValidateTreeOut", "RuleImportFailRow", "RuleImportResult",
    "condition_summary", "parse_rule_sort",
]
