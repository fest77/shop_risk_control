# -*- coding: utf-8 -*-
"""案件的请求 / 响应模型、结论×动作相容矩阵与副作用清单（模块 08，Spec §2.2 / §3.1 / §4.3~4.5）。

## 模型层为什么"宽严分明"（与 06-A/06-B 同一取舍）

| 字段 | 判定位置 | 理由 |
|---|---|---|
| `conclusion` / `action_types` / `remark` 的**取值域与必填** | **服务层**（`DSP-4001` / `DSP-4002`） | Spec §5 给这三种情形分配的是 `DSP-4001`(400) 与 `DSP-4002`(422)。若用 `Literal` / `min_length` 表达，会被 Pydantic 先拦成模块 00 的 `COM-4001`(422)，**契约要求的码永远拿不到**（V-08-04/05 会失败）。这与 `list_schema` 对 `list_type`、`rule_schema` 对 `score` 的处理完全同源 |
| `confirm_token` 缺失 | **服务层**（`DSP-4006`） | BR-08-27 明确"缺失/过期/参数不匹配"一律 `DSP-4006`，不是"字段没填" |
| `list_writes[].list_type` 之类**结构**问题 | **服务层**（`DSP-4001`） | 同上：它是业务参数非法，不是报文格式错 |

因此本文件的请求模型**只声明形状**（类型 + 可空），把取值域判定全部留给
`service` 层；模型层唯一"严格"的地方是不接受请求体里出现 `operator` /
`operator_role`（BR-08-07：处置人一律取自 JWT，模型里根本没有这两个字段，
传了也是"透传不进模型"，由服务层显式忽略）。

## 相容矩阵（BR-08-14）为什么是纯函数

它同时被三处使用：`/dispose/preview`（生成副作用清单）、`/dispose`（服务端
二次校验）、以及前端（动态禁用非法复选框）。**前端不得自行推导**，但服务端
必须能对同一条规则给出同一结论；写成纯函数才能被逐格穷举断言（3 结论 ×
5 动作 = 15 格，`tests/test_disposal_matrix.py` 全部覆盖）。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.enums import ActionType, Conclusion, label_of
from app.schemas.list_schema import ENTITY_TYPES

#: 处理结论取值域（BR-08-13）
CONCLUSIONS: tuple[str, ...] = tuple(c.value for c in Conclusion)

#: 联动处置动作取值域（E09.action_type）
ACTION_TYPES: tuple[str, ...] = tuple(a.value for a in ActionType)

#: **紧动作**（BR-08-30 的 fail-closed 对象）：它们的名单写入失败必须整体回滚。
#: `pass` 刻意不在其中——放行不写名单，也不拦任何东西。
TIGHT_ACTIONS: frozenset[str] = frozenset({
    ActionType.BLOCK_ORDER.value,
    ActionType.BLACKLIST_USER.value,
    ActionType.BAN_DEVICE.value,
    ActionType.REJECT_REFUND.value,
})

#: `remark` 的长度上限（Spec §8「新增 1」：trim 后非空且 ≤500 字）
REMARK_MAX = 500

#: 二次确认令牌的有效期（BR-08-27，秒）
CONFIRM_TTL_SEC = 30

#: `list_writes` 覆盖时允许的名单类型（BR-08-20 的请求约束：
#: 处置**只能**写黑名单与灰名单——白名单是"免风控"的授权，绝不能由处置链路授予）
OVERRIDE_LIST_TYPES: tuple[str, ...] = ("black", "gray")


# ============================================================
# 相容矩阵（BR-08-14）
# ============================================================
def incompatible_reason(conclusion: str, action_types: list[str]) -> Optional[str]:
    """返回不相容的原因文案；相容则返回 `None`（纯函数，无副作用）。

    规则（BR-08-14 逐字落地）：

    - `violation` → **至少 1 个紧动作**，禁止"只有 `pass`"；
    - `normal`    → **只允许** `[pass]`；
    - `suspicious`→ **只允许** `[pass]`（灰名单由 BR-08-20 的默认映射自动写入，
      它不是 `action_type` 的取值——E09 的枚举里没有"写灰名单"这一项，
      凭空造一个动作值会让 06 的名单页与 07 的处置区都认不出它）。

    ⚠️ 一处**按字面执行**并在交付报告中登记的取舍：`violation` 与 `pass`
    同时提交（如 `[pass, block_order]`）按 Spec 原文是允许的（只禁止"只有
    `pass`"）。这里不擅自加严——加严会让前端（按同一份 Spec 实现动态禁用）
    允许的组合被服务端 422 拒绝，属于两端不一致；若确需禁止，应改 Spec。
    """
    actions = [a for a in action_types if a in ACTION_TYPES]
    if not actions:
        # 动作取值本身就是 `DSP-4001` 的范畴（空数组/全是非法值），
        # 由 `validate_action_types` 负责，这里不重复报不相容
        return None
    label = label_of(Conclusion, conclusion, conclusion)
    if conclusion == Conclusion.VIOLATION.value:
        if not (set(actions) & TIGHT_ACTIONS):
            extra = "（`pass` 表示放行，不能作为违规处置的唯一动作）" if (
                ActionType.PASS.value in actions
            ) else ""
            return f"结论为『{label}』时至少需要一项拦截类动作{extra}"
        return None
    if conclusion in (Conclusion.NORMAL.value, Conclusion.SUSPICIOUS.value):
        bad = sorted(set(actions) - {ActionType.PASS.value})
        if bad:
            names = "、".join(label_of(ActionType, a, a) for a in bad)
            return f"结论为『{label}』时不可勾选拦截类动作：{names}"
        return None
    # 未知结论：留给 `validate_conclusion` 报 DSP-4001
    return None


def assert_compatible(conclusion: str, action_types: list[str]) -> None:
    """不相容则抛 `DSP-4002`（`/dispose` 与 `/dispose/preview` 共用同一判定）。"""
    from app.errors import DisposalMatrixViolationError

    reason = incompatible_reason(conclusion, action_types)
    if reason:
        raise DisposalMatrixViolationError(
            reason, conclusion=conclusion, action_types=action_types
        )


def normalize_action_types(raw: Any) -> list[str]:
    """去重并**排序**（BR-08-15）。

    排序是二次确认令牌"参数绑定"的前提（BR-08-27）：用户把复选框的勾选顺序
    换一下（`[ban_device, block_order]` vs `[block_order, ban_device]`）不应该
    让令牌失效——那是同一份意图。因此 preview 与 dispose 都用同一份归一化结果，
    令牌哈希对"集合"而不是"顺序"生效。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    seen: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.append(value)
    return sorted(seen)


# ============================================================
# 副作用清单与默认名单映射（BR-08-20 / 21 / 23 / 29）
# ============================================================
#: 副作用清单的固定五项（Spec §2.2：①~④ 是原型原文，⑤ 是本模块补充）。
#: **这是展示编号，不是执行顺序**（真实执行顺序见 BR-08-29）；
#: 前端**不得自行拼装**这份清单（漏列副作用就是安全漏洞，BR-08-26）。
SIDE_EFFECT_STEPS: tuple[tuple[str, str, str], ...] = (
    ("case_action", "case_actions", "写入案件处置流水（每个动作一条，BR-08-37）"),
    ("list_entry", "list_entries", "写入名单库（source=auto，BR-08-21）"),
    ("biz_sync", "biz_adapter", "调用业务系统适配器（默认 MockBizAdapter，返回值不具权威性）"),
    ("audit_log", "audit_logs", "写入审计哈希链（单写者串行，AD-04）"),
    ("case_status", "risk_cases", "更新案件状态为 disposed"),
)


def build_side_effects(
    conclusion: str, action_types: list[str], list_writes: list[dict],
    *, skipped: Optional[list[str]] = None,
) -> list[dict]:
    """生成 `side_effects[]`（Spec §3.1 的 `{seq, code, target, detail}`）。

    `detail` 里带上**本次的实际情况**（哪些动作、写哪几条名单），而不是一句
    泛泛的"将写入名单库"：二次确认弹窗存在的意义就是让审核员在按下 danger
    按钮之前看清"这一下会做什么"。
    """
    actions_text = "、".join(label_of(ActionType, a, a) for a in action_types)
    writes_text = (
        "、".join(
            f"{w.get('list_type')}/{w.get('entity_type')}={w.get('entity_value')}"
            for w in list_writes
        ) or "本次无名单写入"
    )
    items: list[dict] = []
    for index, (code, target, detail) in enumerate(SIDE_EFFECT_STEPS, start=1):
        extra = ""
        if code == "case_action":
            extra = f"：{actions_text or '（无动作）'}（{len(action_types)} 条）"
        elif code == "list_entry":
            extra = f"：{writes_text}"
        items.append({
            "seq": index, "code": code, "target": target, "detail": f"{detail}{extra}",
        })
    # BR-08-23：事件未携带 device_id 时默认映射**跳过**，且必须在预览里说明，
    # 否则审核员勾了"封禁设备"却在结果里看不到那条名单，会以为系统漏做了
    for index, note in enumerate(skipped or [], start=len(items) + 1):
        items.append({
            "seq": index, "code": "skip", "target": "list_entries", "detail": note,
        })
    return items


def default_list_writes(
    conclusion: str, action_types: list[str], *, user_id: str,
    device_id: Optional[str],
) -> tuple[list[dict], list[str]]:
    """BR-08-20 的默认映射，返回 `(list_writes, skipped_notes)`。

    | 动作 / 结论 | 默认写入 |
    |---|---|
    | `blacklist_user` | `{black, user, case.user_id}` |
    | `ban_device` | `{black, device, event.device_id}`（**缺 `device_id` 则跳过**，BR-08-23） |
    | `suspicious` + `pass` | `{gray, user, case.user_id}`（观察名单，BR-08-20 末行） |
    | `block_order` / `reject_refund` / `pass`（其余） | 不写名单（只调 `BizAdapter`） |

    `expire_at` 一律 `None`（永久）：黑名单与"存疑观察"都不该被时间静默解除
    （灰名单的 30 天默认有效期属 06 的手工新增口径，处置写的是**案件结论**，
    它的效力不该由一条 TTL 悄悄终止）。
    """
    writes: list[dict] = []
    skipped: list[str] = []
    actions = set(action_types)
    if ActionType.BLACKLIST_USER.value in actions and user_id:
        writes.append({
            "list_type": "black", "entity_type": "user",
            "entity_value": str(user_id), "expire_at": None,
        })
    if ActionType.BAN_DEVICE.value in actions:
        if device_id:
            writes.append({
                "list_type": "black", "entity_type": "device",
                "entity_value": str(device_id), "expire_at": None,
            })
        else:
            # 缺 device_id 时**跳过**而不是写空值：E07 的 `entity_value` 为空
            # 会变成一条"谁都拦不住"的黑名单，而它在页面上看起来完全正常
            skipped.append("该事件无 device_id，本次不写设备黑名单（BR-08-23）")
    if conclusion == Conclusion.SUSPICIOUS.value and ActionType.PASS.value in actions:
        if user_id:
            writes.append({
                "list_type": "gray", "entity_type": "user",
                "entity_value": str(user_id), "expire_at": None,
            })
    return writes, skipped


# ============================================================
# 请求模型（**只声明形状**，取值域判定在服务层）
# ============================================================
class _InModel(BaseModel):
    """本模块请求模型的共同配置。

    `extra="ignore"`：请求体里出现 `operator` / `operator_role` 时**必须被忽略**
    而不是报错或写库（BR-08-07：处置人一律取自 JWT，请求体传入的同名字段被忽略）。
    报错会让"前端顺手带了个 operator"变成一个 500 级的联调故障，而正确处置
    本来就是"忽略它"。
    """

    model_config = ConfigDict(extra="ignore")


class DisposePreviewIn(_InModel):
    """`POST /cases/{case_no}/dispose/preview` 请求体（Spec §3.1）。

    **不产生任何副作用**（BR-08-28）：它只签发令牌 + 返回清单，
    不写库、不发事件。
    """

    conclusion: str = Field(default="", description="violation / normal / suspicious")
    action_types: list[str] = Field(default_factory=list, description="去重后 ≥1")


class ListWriteIn(_InModel):
    """`list_writes[]` 的元素（BR-08-20 的可选覆盖）。"""

    list_type: str = Field(default="", description="black / gray（白名单不可由处置授予）")
    entity_type: str = Field(default="")
    entity_value: str = Field(default="")
    expire_at: Optional[int] = Field(default=None, description="毫秒时间戳；缺省永久")


class BizTargetsIn(_InModel):
    """业务单据号（供 `BizAdapter` 使用，Spec §3.1 `biz_targets`）。

    E09 未存 `order_no` / `after_sale_no`（Spec §8「新增 6」已登记该建议），
    因此它们**只能**从请求体带进来；缺省时取该案件 `event_id` 关联的 E01。
    """

    order_no: Optional[str] = None
    after_sale_no: Optional[str] = None
    device_id: Optional[str] = None


class DisposeIn(_InModel):
    """`POST /cases/{case_no}/dispose` 请求体（Spec §3.1）。

    所有"必填"都以**可空 + 服务层判定**的方式表达，见模块 docstring：
    这样 `DSP-4001` / `DSP-4006` 才拿得到（Pydantic 的必填会先变成 `COM-4001`）。
    """

    conclusion: str = Field(default="")
    action_types: list[str] = Field(default_factory=list)
    remark: Optional[str] = Field(default=None, max_length=None)
    confirm_token: str = Field(default="", description="由 /dispose/preview 签发")
    list_writes: Optional[list[ListWriteIn]] = Field(default=None)
    evidence_refs: list[Any] = Field(default_factory=list)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    biz_targets: Optional[BizTargetsIn] = Field(default=None)


class ArchiveIn(_InModel):
    """`POST /cases/{case_no}/archive` 请求体（Spec §3.1：`{remark?}`）。"""

    remark: Optional[str] = Field(default=None, max_length=REMARK_MAX)


class BizSyncRetryIn(_InModel):
    """`POST /cases/{case_no}/biz-sync/retry` 请求体。

    `action_id` 缺省表示"重试该案件全部未成功的联动"（Spec §3.1）。
    """

    action_id: Optional[str] = Field(default=None)


def entity_types() -> tuple[str, ...]:
    """E07 的实体类型（供 `list_writes` 覆盖校验复用 06 的唯一真源）。"""
    return tuple(ENTITY_TYPES)


__all__ = [
    "ACTION_TYPES",
    "CONCLUSIONS",
    "CONFIRM_TTL_SEC",
    "OVERRIDE_LIST_TYPES",
    "REMARK_MAX",
    "SIDE_EFFECT_STEPS",
    "TIGHT_ACTIONS",
    "ArchiveIn",
    "BizSyncRetryIn",
    "BizTargetsIn",
    "DisposeIn",
    "DisposePreviewIn",
    "ListWriteIn",
    "assert_compatible",
    "build_side_effects",
    "default_list_writes",
    "entity_types",
    "incompatible_reason",
    "normalize_action_types",
]
