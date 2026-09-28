# -*- coding: utf-8 -*-
"""案件状态机（模块 08 §4.2，BR-08-08 / 09 / 10）。

**纯函数 + 一张表，因此可以被逐条断言**（`tests/test_case_state.py` 覆盖
"合法迁移全部放行、非法迁移全部拒绝"这 4×4 = 16 种组合，而不是只挑几个写）。
放在 `app/engine/` 而不是 Spec §6 规划稿里的 `app/domain/`：**本仓库没有
`domain/` 这一层，而 `app/engine/` 已经是"纯逻辑、无 IO、可单测"的既定去处**
（`condition.py` / `arbiter.py` / `metric_agg.py` / `audit_hash.py` 都在那里）。
这与决策 D36（`app/infra/` → `app/core/`）是同一条理由：Spec 的文件规划是
按职责写的，落地时必须复用已验证的骨架目录，否则同一类代码会被放在两个地方。

## 唯一合法迁移（E08 状态机）

    pending --认领--> reviewing --处置--> disposed --归档--> archived
                         |
                         +--超时回收--> pending

## 为什么"非法迁移"必须有一处集中的判定

`claim` / `dispose` / `archive` / `recycle` 四条写路径各自写一遍
`if status != "pending": ...`，迟早会出现（也已经在同类项目里出现过）
"某一条路径少判了一个前置状态"：认领能认领到 `archived` 的案件、
归档能把 `pending` 直接推到 `archived`。集中成一张表之后，**新增状态或
新增迁移只改一处**，而"哪些迁移合法"这件事可以被穷举测试钉住。

## 为什么 `reviewing → pending` 也算合法（回退）

它不是用户操作，而是 BR-08-10 的**超时回收**：认领了却没人处置的案件必须
回到待审队列，否则它会永远挂在某个已经下班的审核员名下——`pending` 才是
"等着被认领"的唯一状态。回退**只允许**发生在 `reviewing → pending` 这一条
（`disposed` / `archived` 一律不可回退，BR-08-18：处置不可撤销）。
"""
from __future__ import annotations

from app.enums import CaseStatus
from app.errors import CaseStateConflictError

#: 动作名 -> (允许的前置状态, 目标状态)。动作名同时是审计 `action` 的后缀
#: （`case.claim` / `case.dispose` / `case.archive` / `case.recycle`），
#: 因此这里只写后缀，避免"状态表里叫 claim、审计里叫 claimed"这类漂移。
TRANSITIONS: dict[str, tuple[str, str]] = {
    "claim": (CaseStatus.PENDING.value, CaseStatus.REVIEWING.value),
    "dispose": (CaseStatus.REVIEWING.value, CaseStatus.DISPOSED.value),
    "archive": (CaseStatus.DISPOSED.value, CaseStatus.ARCHIVED.value),
    # 超时回收（BR-08-10）：**不写 `case_actions`**，只写审计（BR-08-11）
    "recycle": (CaseStatus.REVIEWING.value, CaseStatus.PENDING.value),
}

#: 动作名 -> 面向用户的中文动词（拼错误文案用，避免在业务代码里散落中文）
ACTION_LABELS: dict[str, str] = {
    "claim": "认领",
    "dispose": "重复处置",
    "archive": "归档",
    "recycle": "回收",
}

#: 全部状态的合法后继（含"没有后继"的 `archived`）。
#: 与 `TRANSITIONS` 必须一致，由 `check_table_consistency()` 在导入后自检。
LEGAL_NEXT: dict[str, tuple[str, ...]] = {
    CaseStatus.PENDING.value: (CaseStatus.REVIEWING.value,),
    CaseStatus.REVIEWING.value: (CaseStatus.DISPOSED.value, CaseStatus.PENDING.value),
    CaseStatus.DISPOSED.value: (CaseStatus.ARCHIVED.value,),
    CaseStatus.ARCHIVED.value: (),
}

#: 终态：不允许任何后继迁移（`archived`）。
TERMINAL_STATES: frozenset[str] = frozenset({CaseStatus.ARCHIVED.value})


def target_of(action: str) -> str:
    """该动作的目标状态。未知动作抛 `KeyError`（**不给默认值**）。

    给默认值（例如"认领默认变 reviewing"）会让一个拼错的动作用错误的语义
    静默改库；这里宁可让它在开发期就炸出来。
    """
    return TRANSITIONS[action][1]


def source_of(action: str) -> str:
    """该动作要求的前置状态。"""
    return TRANSITIONS[action][0]


def can_transition(source: str, target: str) -> bool:
    """`source → target` 是否合法（纯判定，不抛异常）。"""
    return target in LEGAL_NEXT.get(str(source), ())


def can_perform(action: str, current_status: str) -> bool:
    """当前状态下能否执行该动作。"""
    return can_transition(current_status, target_of(action))


def assert_can_perform(action: str, current_status: str, *, detail: str = "") -> None:
    """不合法就抛 `DSP-4003`（BR-08-08：跨级、回退、终态后的任何迁移一律拒绝）。

    `detail` 用于补充**为什么**不合法（例如"另一次处置正在进行"），
    前端要把它显示在状态徽标旁边——只给一句"状态不允许"用户无从判断该刷新还是该等待。
    """
    if can_perform(action, current_status):
        return
    raise CaseStateConflictError(
        ACTION_LABELS.get(action, action), str(current_status), detail
    )


def next_status(action: str) -> str:
    """动作成功后的状态（`dispose` 成功恒为 `disposed`，Spec §3.1 的响应契约）。"""
    return target_of(action)


def is_disposable(status: str) -> bool:
    """仅 `reviewing` 可处置（`pending` 未认领、`disposed`/`archived` 已结束）。"""
    return can_perform("dispose", status)


def is_archivable(status: str) -> bool:
    """仅 `disposed` 可归档（BR-08-36：手动归档是 admin 的操作）。"""
    return can_perform("archive", status)


def check_table_consistency() -> None:
    """自检两张表一致（由导入时调用一次，也供测试直接断言）。

    有人只改了 `TRANSITIONS` 却忘了 `LEGAL_NEXT`（或反之）时，会立刻报错——
    这类"两处定义同一件事"的分叉在本项目里出现过多次，必须在导入期就拦住。
    """
    for action, (source, target) in TRANSITIONS.items():
        if not can_transition(source, target):
            raise AssertionError(
                f"状态机两处定义不一致：动作 {action} 允许 {source} → {target}，"
                f"但 LEGAL_NEXT 里没有这条迁移"
            )
    known = {s.value for s in CaseStatus}
    if set(LEGAL_NEXT) != known:
        raise AssertionError(
            f"LEGAL_NEXT 必须覆盖全部案件状态：缺 {known - set(LEGAL_NEXT)}，"
            f"多 {set(LEGAL_NEXT) - known}"
        )
    for source, targets in LEGAL_NEXT.items():
        unknown = set(targets) - known
        if unknown:
            raise AssertionError(f"LEGAL_NEXT[{source}] 含未知状态：{unknown}")
        if source in TERMINAL_STATES and targets:
            raise AssertionError(f"终态 {source} 不应有后继：{targets}")


check_table_consistency()


__all__ = [
    "ACTION_LABELS",
    "LEGAL_NEXT",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "assert_can_perform",
    "can_perform",
    "can_transition",
    "check_table_consistency",
    "is_archivable",
    "is_disposable",
    "next_status",
    "source_of",
    "target_of",
]
