# -*- coding: utf-8 -*-
"""模块 10「事件仿真测试」的请求 / 响应模型（Spec §3.1 ~ §3.5）。

## 为什么 `event` / `event_template` 是 `dict` 而不是强类型模型

与 `engine_schema.EngineEvaluateIn` **完全同因**（那一份的模块 docstring 已论证
过一次，这里不重复设计而是一致的取舍）：

1. 事件体的强类型模型是 `event_schema.EventIn`，但它**刻意不做语义校验**
   （五类枚举、按类型必填、`scene_extra` 白名单全部在 `event_service.validate_event`
   的服务层，为的是错误码不被 Pydantic 吃掉）。仿真表单提交的事件**必须走同一套**，
   否则会出现"仿真通过、真实入口 422"——一致性（本模块的核心价值）当场作废；
2. 若这里用 `EventIn` 直接声明，非法字段类型会先被 Pydantic 拦成通用 `COM-4001`，
   而任务书 §3 要求仿真表单拿到与 `POST /events` **同一个码**（`EVT-4005` 等）；
3. E01 的事件模型会演进，两处各写一遍必然有一处先漂移。

因此本文件只做**结构性**约束（"是不是对象"），字段级判定一律交给 03 的校验器，
由 `sim_service` 在步骤 1 调用它——**本模块没有第二套事件校验**。

## `scene_extra` 的两种形态都收（`SIM-4002` 的落点）

Spec §2.2 把 `scene_extra` 写成"多行文本（JSON 字符串）"，而 §3.2 的
`event_template` 写的是"完整事件体（结构同 `POST /events` 的 `event`）"——
后者的 `scene_extra` 是**对象**。两个口径都要认：

- 传对象（API 直连、用例模板回灌）→ 原样使用；
- 传字符串（仿真页表单原样提交）→ 本模块 `json.loads` 一次；
  解析失败即 `422 SIM-4002`「扩展参数不是合法 JSON」。

**解析结果非对象同样报 `SIM-4002`**：`"[1,2]"` 是合法 JSON 但不是对象，
若放行到 03 就会变成 `EVT-4005`（字段格式非法）——而用户改的地方是同一个
输入框，两个码会让前端的高亮分支只能命中一边。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.constants import (
    SIM_BATCH_DEFAULT_REPEAT,
    SIM_CASE_DESC_MAX,
    SIM_CASE_NAME_MAX,
    SIM_CASE_NAME_MIN,
)

# ============================================================
# 枚举取值（Spec §3.1 ~ §3.4）
# ============================================================
#: 用例分类（Spec §3.2 的 `category` 枚举；§2.1 的内置 4 条用的是前四个）
CASE_CATEGORIES: tuple[str, ...] = (
    "coupon_abuse", "aftersale_abuse", "normal", "boundary", "other",
)

#: 预期决策（Spec §3.2 的 `expected_decision` 枚举）
EXPECTED_DECISIONS: tuple[str, ...] = ("pass", "review", "reject")

#: 五步链路的**固定步骤名**（Spec §3.3：「`steps[].name` 取值固定」）。
#: 前端按这个名字渲染五个块，因此它是一份**契约**而不是"实现细节"——
#: 单独导出常量，让接口层/服务层/测试三处引用同一个元组，改一处即全改。
STEP_NAMES: tuple[str, ...] = (
    "event_validate", "feature_extract", "list_filter", "rule_evaluate", "arbitrate",
)

#: 步骤状态（Spec BR-10-10：`ok` / `failed` / `skipped`）
STEP_STATUSES: tuple[str, ...] = ("ok", "failed", "skipped", "pending")

#: 五步的中文标题（Spec §2.3 的表；页面标题由后端下发，前端不硬编码）
STEP_LABELS: dict[str, str] = {
    "event_validate": "事件校验",
    "feature_extract": "特征提取（18 项）",
    "list_filter": "名单快速过滤",
    "rule_evaluate": "规则命中链路",
    "arbitrate": "最终决策",
}


def step_label(name: str) -> str:
    """步骤中文标题（未知名字回退成名字本身，不抛异常）。"""
    return STEP_LABELS.get(name, name)


#: `scene_extra` 被当作 JSON 字符串解析时的键名（仿真页表单的字段名）。
SCENE_EXTRA_KEY = "scene_extra"


def parse_scene_extra(event: Any) -> tuple[dict[str, Any], Optional[str]]:
    """把事件体里的 `scene_extra` 归一成对象，返回 `(事件体, 失败原因)`。

    **成功时返回的是同一个对象**（没有 `scene_extra` 或已经是对象），
    因此这个函数在绝大多数调用上是零成本的；只有"字符串形态"那一支会
    复制一份并替换。`event` 不是对象时原样返回——那是 03 的 `EVT-4001`
    该管的事，本函数不越界报错。

    `scene_extra` 为 `None` / 空串 / 空白串时**视为未提供**（不报错）：
    仿真页的表单是"多行文本框"，用户不填时提交的就是空串；把它当成
    "非法 JSON" 会让每一次不带扩展参数的表单提交都红一次，而
    `login` 之外的事件类型本来就允许 `scene_extra` 缺失（缺的必填键由
    `EVT-4004` 报，那个提示才指得对地方）。

    失败原因是**面向用户的中文短句**（Spec §5 的提示文案由 `SIM-4002` 给），
    这里只回"哪个键、错在哪"，让服务层去配上错误码。
    """
    if not isinstance(event, dict) or SCENE_EXTRA_KEY not in event:
        return event, None
    raw = event.get(SCENE_EXTRA_KEY)
    if isinstance(raw, dict) or raw is None:
        return event, None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            normalized = dict(event)
            normalized.pop(SCENE_EXTRA_KEY, None)
            return normalized, None
        try:
            parsed = json.loads(text)
        except ValueError as e:
            return event, f"scene_extra 不是合法 JSON：{e}"
        if not isinstance(parsed, dict):
            return event, (
                "scene_extra 必须是 JSON 对象，"
                f"收到 {type(parsed).__name__}（如 {{\"coupon_id\": \"C1\"}}）"
            )
        normalized = dict(event)
        normalized[SCENE_EXTRA_KEY] = parsed
        return normalized, None
    return event, f"scene_extra 必须是 JSON 对象或 JSON 字符串，收到 {type(raw).__name__}"


# ============================================================
# §3.1 用例列表
# ============================================================
class SimCaseOut(BaseModel):
    """`GET /sim/cases` 的 `items[]`（Spec §3.1 逐字段对齐）。

    刻意**不下发** `status` / `deleted_at` 等软删字段：列表只返回
    `status=active` 的用例（BR-10-20），把内部状态摊给前端只会诱导它写
    "其实用不上"的分支。历史 `sim_runs` 的追溯走 `case_id`，不需要前端参与。
    """

    model_config = ConfigDict(extra="allow")

    case_id: str
    name: str
    category: str
    expected_decision: str
    description: Optional[str] = None
    event_template: dict[str, Any] = Field(default_factory=dict)
    created_by: str = ""
    created_at: int = 0


class SimCaseListOut(BaseModel):
    """`GET /sim/cases` 的响应 `data`。

    `items` 之外补 `total`：Spec 只写了 `items`，但空态文案（§2.1
    「暂无用例，可手动填写参数后保存」）与前端的"共 N 条"都需要一个总数，
    由前端 `items.length` 推出来在分页（未来）之后就是错的。
    """

    items: list[SimCaseOut] = Field(default_factory=list)
    total: int = 0


# ============================================================
# §3.2 保存用例
# ============================================================
class SimCaseIn(BaseModel):
    """`POST /sim/cases` 请求体（Spec §3.2）。

    ## 长度约束为什么在这里用 Pydantic 而不是服务层

    与事件体相反：`name` 的"2~64 字符"是**本模块自己的**输入约束，没有
    "另一个接口的码要对齐"的问题；而 `SIM-4003`（重名）才是本模块的业务不变量，
    它必须在服务层（要查库）。这类纯字段边界交给 Pydantic（`COM-4001`）与
    06-B 的 `RuleCreate` 完全同一口径。

    ## `event_template` 只判"是不是对象"

    内容合法性由 `sim_service` 用 03 的 `validate_event` 判（见模块 docstring）。
    在**保存**时就校验一次是刻意的：BR-10-17 要求"用例必须包含完整事件体，
    载入即填充整个表单"，一条存进来就跑不通的用例会在演示现场才发现。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=SIM_CASE_NAME_MIN, max_length=SIM_CASE_NAME_MAX)
    category: str = Field(description="/".join(CASE_CATEGORIES))
    expected_decision: str = Field(description="/".join(EXPECTED_DECISIONS))
    description: Optional[str] = Field(default=None, max_length=SIM_CASE_DESC_MAX)
    event_template: dict[str, Any] = Field(
        description="完整事件体（结构同 POST /api/v1/events 的 event）"
    )


class SimCaseCreatedOut(BaseModel):
    """`POST /sim/cases` 响应 `data`（Spec §3.2 只要求这两个字段）。"""

    case_id: str
    created_at: int
    name: str = ""
    #: 保存时由 03 的真实校验回填的结论（`ok` / 缺失字段清单）。
    #: 它不是 Spec 的字段，但**保存即验证**是本模块刻意加的一道门：
    #: 让"用例存进去了却跑不通"在保存那一刻就暴露，而不是在演示现场。
    validation: dict[str, Any] = Field(default_factory=dict)


# ============================================================
# §3.3 单条仿真执行
# ============================================================
class SimRunIn(BaseModel):
    """`POST /sim/run` 请求体（Spec §3.3）。

    `affect_window` 是**决策 D11 的开关**（Spec §8 明确登记为待确认项，
    D11 已裁定："默认不写入；提供 `affect_window` 开关，但批量回放强制 false"）。
    这里如实提供它并默认 `False`，理由写在字段说明里——
    仿真默认只读当前窗口，是"演示可复现"（BR-00-25）的前提。
    """

    model_config = ConfigDict(extra="forbid")

    event: dict[str, Any] = Field(description="事件体；校验与 POST /events 同一套")
    case_id: Optional[str] = Field(default=None, description="来源用例（用于预期比对）")
    trace: bool = Field(default=True, description="默认 true：本模块需要链路明细")
    affect_window: bool = Field(
        default=False,
        description=(
            "D11 开关：true 时把本次事件写进 04 的真实特征窗口。"
            "默认 false —— 仿真只读窗口，否则连续仿真会永久抬高线上特征"
        ),
    )


class SimStepOut(BaseModel):
    """五步链路里的单步（Spec BR-10-10 的五个属性，逐项对齐）。"""

    model_config = ConfigDict(extra="allow")

    seq: int
    name: str
    label: str = ""
    status: str = "pending"
    detail: str = ""
    elapsed_ms: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class SimRunDataOut(BaseModel):
    """`POST /sim/run` 的响应 `data`（Spec §3.3 的表逐字段对齐）。

    `model_config` 用 `extra="allow"`：实现额外回传了几个**审计性**字段
    （`window_premise` / `affects_window` / `record_saved` / `orchestration_ms` /
    `rule_degraded` / `warnings`），它们不在冻结表里但页面必须能看到——
    `extra="allow"` 保证"契约字段一个不少，附加字段不丢"，而不是靠
    `model_dump(exclude_none)` 之类的技巧去凑。
    """

    model_config = ConfigDict(extra="allow")

    run_id: str
    steps: list[SimStepOut] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    missing_features: list[str] = Field(default_factory=list)
    list_hit: dict[str, Any] = Field(default_factory=dict)
    hits: list[dict[str, Any]] = Field(default_factory=list)
    rule_score: int = 0
    final_score: int = 0
    risk_level: str = "low"
    decision: str = "review"
    expected_decision: Optional[str] = None
    matched_expected: Optional[bool] = None
    elapsed_ms: int = 0
    dry_run: bool = True
    #: 本次结论基于哪一版规则（BR-10-04：**全量**生效规则集，不只命中项，D60）
    rule_versions: dict[str, Any] = Field(default_factory=dict)
    engine_version: str = ""


# ============================================================
# §3.4 批量回放
# ============================================================
class SimBatchIn(BaseModel):
    """`POST /sim/batch` 请求体（Spec §3.4）。

    `event` 是可选的：给了就按它跑（前端表单里改过的参数），没给就从
    `case_id` 的用例模板取。Spec 的请求表只写了 `{case_id, repeat, seed}`，
    但 §2.2 的按钮是"批量回放**此用例**"——它回放的是**当前表单里的参数**
    而不是用例模板里的旧参数（策略师刚调过参数就要验证）。
    两者都支持，优先级：`event` > `case_id.event_template`。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: Optional[str] = Field(default=None, description="要回放的用例")
    event: Optional[dict[str, Any]] = Field(
        default=None, description="覆盖用例模板的事件体（表单当前参数）"
    )
    repeat: int = Field(default=SIM_BATCH_DEFAULT_REPEAT, description="1~200")
    seed: int = Field(default=42, description="同一 seed + 同一用例结果完全一致")


class SimMismatchSampleOut(BaseModel):
    """不符样例（Spec §2.4 的「前 3 条不符样例的 `sim_run` 链接」）。"""

    run_id: str
    decision: str
    expected_decision: Optional[str] = None


class SimBatchOut(BaseModel):
    """`POST /sim/batch` 的响应 `data`（Spec §3.4 的表逐字段对齐）。

    `mismatched` 与「与预期不符数」是**同一个数**（§2.4 明确写"单列（即未命中数）"），
    因此只回一个字段、由前端渲染两次——两处各算一遍迟早对不上。
    """

    model_config = ConfigDict(extra="allow")

    total: int = 0
    matched: int = 0
    mismatched: int = 0
    false_positive: int = 0
    mismatch_samples: list[SimMismatchSampleOut] = Field(default_factory=list)
    elapsed_ms: int = 0
    case_id: Optional[str] = None
    seed: int = 0
    repeat: int = 0
    expected_decision: Optional[str] = None
    decision_counts: dict[str, int] = Field(default_factory=dict)
    run_ids: list[str] = Field(default_factory=list)
    affects_window: bool = False


__all__ = [
    "CASE_CATEGORIES",
    "EXPECTED_DECISIONS",
    "SCENE_EXTRA_KEY",
    "STEP_LABELS",
    "STEP_NAMES",
    "STEP_STATUSES",
    "SimBatchIn",
    "SimBatchOut",
    "SimCaseCreatedOut",
    "SimCaseIn",
    "SimCaseListOut",
    "SimCaseOut",
    "SimMismatchSampleOut",
    "SimRunDataOut",
    "SimRunIn",
    "SimStepOut",
    "parse_scene_extra",
    "step_label",
]
