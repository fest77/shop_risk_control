# -*- coding: utf-8 -*-
"""案件状态机（BR-08-08 / 09 / 10，Spec 08 §6 的 `test_case_state.py`）。

**穷举而不是抽样**：E08 的四个状态两两组合共 16 格，`pending` / `reviewing`
还能被四个动作各自触及。抽样断言（只测"disposed 不能认领"这类典型格）
放过的恰恰是最容易写错的那几格——回退（`disposed → pending`）、
跨级（`pending → archived`）、终态后的任何迁移。这里逐格钉住。
"""
from __future__ import annotations

import pytest

from app.engine import case_state
from app.enums import CaseStatus
from app.errors import CaseStateConflictError

pytestmark = pytest.mark.anyio

ALL_STATUSES = [s.value for s in CaseStatus]

#: 合法迁移表（E08 状态机 + BR-08-10 的超时回收），与实现**独立**写一遍：
#: 若两边都从同一个常量抄，这条测试就退化成"断言自己等于自己"
LEGAL: dict[str, set[str]] = {
    "pending": {"reviewing"},
    "reviewing": {"disposed", "pending"},
    "disposed": {"archived"},
    "archived": set(),
}


async def test_can_transition_covers_all_16_combinations():
    """4×4 全部格：合法放行、非法拒绝（BR-08-08 的"任何其他迁移一律拒绝"）。"""
    for source in ALL_STATUSES:
        for target in ALL_STATUSES:
            expected = target in LEGAL[source]
            assert case_state.can_transition(source, target) is expected, (
                f"{source} → {target} 期望 {expected}，实际 "
                f"{case_state.can_transition(source, target)}"
            )


async def test_unknown_source_state_is_rejected():
    """未知状态（脏数据/人工改库）一律**拒绝**，不是"没规则所以放行"。"""
    assert case_state.can_transition("unknown", "reviewing") is False
    assert case_state.can_transition("", "pending") is False


async def test_actions_map_to_the_right_targets():
    """四个动作的目标状态与前置状态（认领/处置/归档/回收）。"""
    assert case_state.source_of("claim") == "pending"
    assert case_state.target_of("claim") == "reviewing"
    assert case_state.source_of("dispose") == "reviewing"
    assert case_state.target_of("dispose") == "disposed"
    assert case_state.source_of("archive") == "disposed"
    assert case_state.target_of("archive") == "archived"
    # 回收是**回退**：它不是用户操作，而是 BR-08-10 的超时回收
    assert case_state.source_of("recycle") == "reviewing"
    assert case_state.target_of("recycle") == "pending"


@pytest.mark.parametrize("action,status,ok", [
    ("claim", "pending", True),
    ("claim", "reviewing", False),   # V-08-02：对已认领的案件再认领 → DSP-4003
    ("claim", "disposed", False),
    ("claim", "archived", False),
    ("dispose", "pending", False),   # 未认领不能处置（先认领）
    ("dispose", "reviewing", True),
    ("dispose", "disposed", False),  # 重复处置 → DSP-4004（在服务层细分）
    ("dispose", "archived", False),
    ("archive", "pending", False),   # 跨级：pending 不能直接归档
    ("archive", "reviewing", False),
    ("archive", "disposed", True),
    ("archive", "archived", False),  # 终态
    ("recycle", "reviewing", True),
    ("recycle", "pending", False),
    ("recycle", "disposed", False),
])
async def test_can_perform_matrix(action: str, status: str, ok: bool):
    assert case_state.can_perform(action, status) is ok


@pytest.mark.parametrize("status", ALL_STATUSES)
async def test_assert_can_perform_raises_dsp4003_with_human_hint(status: str):
    """非法迁移抛 `DSP-4003`，且 `data` 里带上当前状态与动作（前端要显示它）。"""
    if case_state.can_perform("archive", status):
        pytest.skip(f"{status} 合法可归档，本用例只覆盖非法迁移")
    with pytest.raises(CaseStateConflictError) as ei:
        case_state.assert_can_perform("archive", status, detail="补充说明")
    exc = ei.value
    assert exc.code == "DSP-4003"
    assert exc.http_status == 409
    assert exc.data["current_status"] == status
    assert "补充说明" in exc.message


async def test_terminal_state_has_no_successor():
    """`archived` 是终态：处置不可撤销（BR-08-18）在状态机上的体现。"""
    assert case_state.LEGAL_NEXT["archived"] == ()
    assert case_state.TERMINAL_STATES == frozenset({"archived"})
    for target in ALL_STATUSES:
        assert not case_state.can_transition("archived", target)


async def test_helper_predicates_agree_with_the_table():
    assert case_state.is_disposable("reviewing") is True
    assert case_state.is_disposable("pending") is False
    assert case_state.is_archivable("disposed") is True
    assert case_state.is_archivable("reviewing") is False


async def test_table_consistency_selfcheck():
    """两处定义（`TRANSITIONS` / `LEGAL_NEXT`）必须一致——导入期已自检一次。"""
    case_state.check_table_consistency()
    for action, (source, target) in case_state.TRANSITIONS.items():
        assert target in case_state.LEGAL_NEXT[source], action
