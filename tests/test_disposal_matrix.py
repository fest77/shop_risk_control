# -*- coding: utf-8 -*-
"""结论×动作相容矩阵与备注必填（BR-08-13 ~ 08-17，Spec 08 §6 的 `test_disposal_matrix.py`）。

矩阵是 3 结论 × 5 动作 = 15 格，**逐格断言**（不是挑几个典型）。理由与
`test_case_state` 相同：漏判的那一格恰恰是"violation 只勾了 pass"这类
看起来无害、实际把拦截变成放行的组合。

判定分两层，两层都要覆盖：
- **纯函数层**（`case_schema.incompatible_reason`）：前端与服务端共用的那份结论；
- **服务层**（`DisposalService.validate_*`）：它负责把结论翻译成 `DSP-4001` /
  `DSP-4002`——**错误码只在服务层产生**（模型层写死会把码先变成 `COM-4001`）。
"""
from __future__ import annotations

import pytest

from app.errors import DisposalMatrixViolationError, DisposalParamInvalidError
from app.schemas.case_schema import (
    ACTION_TYPES,
    CONCLUSIONS,
    REMARK_MAX,
    incompatible_reason,
    normalize_action_types,
)
from app.services.disposal_service import DisposalService

pytestmark = pytest.mark.anyio

#: 逐格写死的期望（**独立于实现**再写一遍）：
#: `violation` 必须有紧动作；`normal` / `suspicious` 只允许 `pass`
EXPECTED_OK: dict[str, set[str]] = {
    "violation": {"block_order", "blacklist_user", "ban_device", "reject_refund"},
    "normal": {"pass"},
    "suspicious": {"pass"},
}


@pytest.mark.parametrize("conclusion", CONCLUSIONS)
@pytest.mark.parametrize("action", ACTION_TYPES)
async def test_matrix_cell_by_cell(conclusion: str, action: str):
    """15 格：单动作提交时是否相容。"""
    reason = incompatible_reason(conclusion, [action])
    should_pass = action in EXPECTED_OK[conclusion]
    if should_pass:
        assert reason is None, f"{conclusion}+[{action}] 应相容，却报：{reason}"
    else:
        assert reason, f"{conclusion}+[{action}] 应拒绝，却放行了"


@pytest.mark.parametrize("actions,ok", [
    (["block_order", "blacklist_user", "ban_device"], True),   # 原型的「违规拦截」预置
    (["pass"], False),                                        # 只有 pass：禁止
    (["pass", "block_order"], True),                          # 字面合法（见下方说明）
    (["reject_refund"], True),
])
async def test_violation_needs_at_least_one_tight_action(actions: list[str], ok: bool):
    """`violation` 的"至少 1 个紧动作"（BR-08-14 前半句）。

    `["pass", "block_order"]` 按 Spec 原文**是合法的**——原文只禁止"只有 `pass`"。
    这里如实钉住这一格，并把"是否应该加严"留给 Spec 裁定：实现擅自加严会让
    按同一份 Spec 实现的前端（动态禁用非法复选框）与服务端互相矛盾。
    """
    reason = incompatible_reason("violation", actions)
    assert (reason is None) is ok, f"violation+{actions} → {reason}"


async def test_empty_actions_is_a_param_error_not_a_matrix_error():
    """空动作集属 `DSP-4001`（没填），**不是** `DSP-4002`（填了但不相容）。

    因此纯函数对空数组返回 `None`（它只判"相容性"），把"必填"留给
    `validate_conclusion_and_actions`——两件事混在一个码里，前端就无法区分
    "用户什么都没选"与"用户选错了"，而这两种提示语完全不同。
    """
    assert incompatible_reason("violation", []) is None
    with pytest.raises(DisposalParamInvalidError) as ei:
        DisposalService.validate_conclusion_and_actions("violation", [])
    assert ei.value.code == "DSP-4001"


@pytest.mark.parametrize("conclusion", ["normal", "suspicious"])
async def test_normal_and_suspicious_allow_only_pass(conclusion: str):
    assert incompatible_reason(conclusion, ["pass"]) is None
    for action in ACTION_TYPES:
        if action == "pass":
            continue
        assert incompatible_reason(conclusion, [action]), f"{conclusion}+{action} 应拒绝"


async def test_normalize_dedups_and_sorts():
    """BR-08-15 的去重 + 稳定排序（令牌绑定与"同集合不同顺序"都依赖它）。"""
    assert normalize_action_types(["ban_device", "block_order", "ban_device"]) == [
        "ban_device", "block_order"
    ]
    assert normalize_action_types(None) == []
    assert normalize_action_types("pass") == ["pass"]
    assert normalize_action_types(["", "  ", "pass"]) == ["pass"]


# ============================================================ 服务层：错误码
@pytest.mark.parametrize("conclusion,actions,code", [
    ("", ["pass"], "DSP-4001"),          # 结论缺失
    ("unknown", ["pass"], "DSP-4001"),   # 结论取值非法
    ("normal", [], "DSP-4001"),          # 动作空
    ("normal", [""], "DSP-4001"),        # 动作全是空白
    ("normal", ["not_an_action"], "DSP-4001"),  # 动作取值非法
    ("normal", ["block_order"], "DSP-4002"),    # 相容矩阵
    ("suspicious", ["ban_device"], "DSP-4002"),
    ("violation", ["pass"], "DSP-4002"),
])
async def test_validate_conclusion_and_actions_codes(
    conclusion: str, actions: list[str], code: str
):
    with pytest.raises(Exception) as ei:
        DisposalService.validate_conclusion_and_actions(conclusion, actions)
    assert getattr(ei.value, "code", None) == code, ei.value
    assert ei.value.http_status == (400 if code == "DSP-4001" else 422)


async def test_validate_accepts_the_prototype_presets():
    """原型两个快捷按钮的预置组合必须都能通过（否则演示第一步就 422）。"""
    assert DisposalService.validate_conclusion_and_actions("normal", ["pass"]) == (
        "normal", ["pass"]
    )
    verdict, actions = DisposalService.validate_conclusion_and_actions(
        "violation", ["block_order", "blacklist_user", "ban_device", "block_order"]
    )
    assert verdict == "violation"
    assert actions == ["ban_device", "blacklist_user", "block_order"]


@pytest.mark.parametrize("remark,ok", [
    ("这是处置原因", True),
    ("  前后有空格但非空  ", True),
    (None, False),
    ("", False),
    ("   ", False),
    ("\t\n ", False),
    ("a" * REMARK_MAX, True),
    ("a" * (REMARK_MAX + 1), False),
])
async def test_remark_is_required_and_bounded(remark, ok: bool):
    """BR-08-16 / V-08-04：`remark` trim 后非空且 ≤500 字，否则 `DSP-4001`(400)。"""
    if ok:
        assert DisposalService.validate_remark(remark) == remark
        return
    with pytest.raises(DisposalParamInvalidError) as ei:
        DisposalService.validate_remark(remark)
    assert ei.value.code == "DSP-4001"
    assert ei.value.http_status == 400


async def test_remark_is_stored_verbatim():
    """BR-08-16 的"备注**原样**写入"：服务端不替审核员改他的证据。"""
    raw = "  用户申诉说不是本人操作  "
    assert DisposalService.validate_remark(raw) == raw


async def test_matrix_error_carries_reason_and_inputs():
    """`DSP-4002` 必须带"为什么"与当时的入参（前端要显示原因文案）。"""
    with pytest.raises(DisposalMatrixViolationError) as ei:
        DisposalService.validate_conclusion_and_actions("normal", ["block_order"])
    exc = ei.value
    assert exc.code == "DSP-4002" and exc.http_status == 422
    assert "拦截订单" in exc.data["detail"]
    assert exc.data["conclusion"] == "normal"
    assert exc.data["action_types"] == ["block_order"]
