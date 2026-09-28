# -*- coding: utf-8 -*-
"""分值 → 风险等级 → 决策动作（BR-05-15 ~ 05-18）——**纯函数、无 IO**。

## 三档（BR-05-17，已确认，不做 Challenge）

    0~59   → low    / pass     放行
    60~79  → medium / review   转人工
    80~100 → high   / reject   拦截

`≥80 红 / 60~79 橙 / <60 绿`（Spec §2.1）与本表**必须同源**：界面上大字颜色的
分界点如果和这里差 1，就会出现"绿色大字 + 人审建议"这种自相矛盾的画面。
因此 `SCORE_BANDS` 同时供后端分级与前端说明使用（前端从 `risk_level` 取色，
不自己算分档）。

## 截断（BR-05-16，悬空点 G-03）

`rule_score = min(100, Σ score)`。**为什么是截断而不是归一化**：`score` 是
规则作者写的分值（40 分 = "这条很可疑"），它表达的是**绝对严重度**；归一化
（`Σ/max × 100`）会让"命中了 1 条 40 分"与"命中了 3 条 40 分里的第 1 条"
得到不同分数——同一件事实、同一个分值，分数却因当天配了几条规则而变，
历史决策立刻不可比。截断则只丢失超出部分的信息，且丢失方向是**保守的**
（仍然落在 `high/reject`）。

## 为什么 `medium` 的默认动作是 `review` 而不是"看情况"

`decision` 与 `risk_level` 是 1:1 的（BR-11-12 的交叉筛选依赖这一点：
scene 桶的分档计数可直接当等级分布用）。允许"medium 但 pass"会让同一条
决策在等级维度与动作维度上互相矛盾，而 08 建案、11 统计、07 列表筛选全都要
读这两个字段。

## 扩展路径（决策 D14：不引入 `Challenge`，但留路）

将来若要加第四档，只需在 60~79 区间内再切一刀并给 `Decision` 加一个成员——
`SCORE_BANDS` 是**顺序表**，加一行即可，不触碰任何数据模型或落库结构。
"""
from __future__ import annotations

from app.engine.metric_agg import DECISION_TO_LEVEL

#: 分值上限（BR-05-16：累加结果截断到 100）
SCORE_MAX = 100
#: 分值下限（负分没有语义，`score` 在 E05 里是 0~100；夹到 0 防止脏数据把决策拉成负分）
SCORE_MIN = 0

#: 三档定义：`(上界, risk_level)`，**顺序表**（区间左开右闭，由 `band_for` 逐段比较）
SCORE_BANDS: tuple[tuple[int, str], ...] = (
    (59, "low"),
    (79, "medium"),
    (SCORE_MAX, "high"),
)

#: 等级 -> 动作（BR-05-17）。**由 `DECISION_TO_LEVEL` 就地反查得到**，
#: 而不是各写一份：这两张表必须是彼此的逆（BR-11-12 的交叉筛选完全建立在此之上），
#: 分两处维护迟早出现"medium → pass"这种单边修改。
LEVEL_TO_ACTION: dict[str, str] = {
    level: decision for decision, level in DECISION_TO_LEVEL.items()
}

#: 等级的展示顺序（前端与统计都要按这个顺序，避免"高/中/低"和"低/中/高"混用）
LEVEL_ORDER: tuple[str, ...] = ("low", "medium", "high")


def clamp_score(total: float) -> int:
    """累加分截断到 `0~100`（BR-05-16）。

    非整数一律向下取整：`score` 在 E05 里是 int，浮点只可能来自脏数据，
    而"多算 0.6 分跨过 60 分线"会让一条本该放行的决策变成建案——宁可少算。
    """
    try:
        value = int(total)
    except (TypeError, ValueError):
        return SCORE_MIN
    return max(SCORE_MIN, min(SCORE_MAX, value))


def band_for(score: int) -> str:
    """分值 → 风险等级（BR-05-17）。超过 100 按 100 处理，负分按 0。"""
    value = clamp_score(score)
    for upper, level in SCORE_BANDS:
        if value <= upper:
            return level
    return SCORE_BANDS[-1][1]


def action_for(level: str) -> str:
    """风险等级 → 决策动作（BR-05-17）。

    未知等级返回 `review` 而不是 `pass`：**fail-closed**。等级取值只可能来自
    `band_for`，走到这里说明上游有脏数据——脏数据的方向不可知，只能转人工。
    """
    return LEVEL_TO_ACTION.get(level, "review")


def arbitrate(total: float) -> tuple[int, str, str]:
    """一次算完 `(rule_score, risk_level, decision)`。

    返回值的第一项是**截断后**的分值：调用方必须把截断值写进响应与 E03
    （`rule_score` 与 `final_score` 都是 0~100 的终值），**不要**把累加原值
    也带出去——两处存在两个分值，前端与 11 的统计必然会各取一个。
    """
    score = clamp_score(total)
    level = band_for(score)
    return score, level, action_for(level)


__all__ = [
    "LEVEL_ORDER",
    "LEVEL_TO_ACTION",
    "SCORE_BANDS",
    "SCORE_MAX",
    "SCORE_MIN",
    "action_for",
    "arbitrate",
    "band_for",
    "clamp_score",
]
