# -*- coding: utf-8 -*-
"""分值 → 风险等级 → 决策动作（BR-05-15 ~ 05-18，含 V-05-05 的截断）。

## 为什么边界值必须逐个钉住

三档的边界（59/60、79/80）与截断（>100 记 100）是**一分数之差就改变处置**
的地方：60 分是"放行"与"建案"的分界，80 分是"人审"与"拦截"的分界。少写一个
边界值，错的就是真实资金损失的那一侧。因此这里把 0/59/60/79/80/100/101/120
逐个列出，而不是抽查三档各一个。

## 为什么 `LEVEL_TO_ACTION` 必须与 11 的 `DECISION_TO_LEVEL` 互逆

模块 11 的交叉筛选（scene 桶的分档计数直接当等级分布用）**完全建立在这两张表
互为逆映射之上**。若有人在 05 把 `medium` 改成 `pass`，11 的大盘会出现
"等级=中 而 决策=放行"的自相矛盾数据，而两处的测试各自都是绿的。
"""
from __future__ import annotations

import pytest

from app.engine.arbiter import (
    LEVEL_ORDER,
    LEVEL_TO_ACTION,
    SCORE_BANDS,
    SCORE_MAX,
    SCORE_MIN,
    action_for,
    arbitrate,
    band_for,
    clamp_score,
)
from app.engine.metric_agg import DECISION_TO_LEVEL, LEVEL_TO_DECISION

pytestmark = pytest.mark.anyio


# ============================================================
# 截断（BR-05-16 / V-05-05 / 悬空点 G-03）
# ============================================================
async def test_score_is_truncated_to_100():
    """三条 40 分规则全命中 → 100，**不是 120**（V-05-05 的纯函数面）。"""
    assert clamp_score(120) == SCORE_MAX
    assert arbitrate(120) == (100, "high", "reject")
    assert arbitrate(3 * 40) == (100, "high", "reject")


async def test_score_floor_is_zero():
    """负分夹到 0：`score` 在 E05 里是 0~100，负值只可能来自脏数据。"""
    assert clamp_score(-5) == SCORE_MIN
    assert arbitrate(-5) == (0, "low", "pass")


async def test_clamp_rejects_non_numeric():
    """脏数据（None / 字符串）取 0 而不是抛异常：决策链路不因一条脏数据中断。"""
    assert clamp_score(None) == 0          # type: ignore[arg-type]
    assert clamp_score("abc") == 0         # type: ignore[arg-type]


async def test_clamp_truncates_floats_toward_zero():
    """浮点向下取整：`多算 0.6 分跨过 60 分线` 会把一次放行变成建案。"""
    assert clamp_score(59.9) == 59
    assert clamp_score(60.0) == 60


# ============================================================
# 三档边界（BR-05-17）
# ============================================================
BAND_CASES: tuple[tuple[int, str, str], ...] = (
    (0, "low", "pass"),
    (1, "low", "pass"),
    (59, "low", "pass"),
    (60, "medium", "review"),
    (70, "medium", "review"),
    (79, "medium", "review"),
    (80, "high", "reject"),
    (90, "high", "reject"),
    (100, "high", "reject"),
    # 超界值：截断后落高档（方向是保守的）
    (101, "high", "reject"),
    (999, "high", "reject"),
)


@pytest.mark.parametrize("score,level,decision", BAND_CASES,
                         ids=[f"{c[0]}-{c[2]}" for c in BAND_CASES])
async def test_band_boundaries(score, level, decision):
    """0~59 low/pass、60~79 medium/review、80~100 high/reject。"""
    assert arbitrate(score) == (min(score, 100), level, decision)


async def test_bands_cover_zero_to_max_without_gap():
    """分档表必须**无缝覆盖** 0~100：任何分值都能落到一档。"""
    assert SCORE_BANDS[-1][0] == SCORE_MAX
    for value in range(SCORE_MIN, SCORE_MAX + 1):
        assert band_for(value) in LEVEL_ORDER, value


# ============================================================
# 与模块 11 的映射互逆（BR-11-12 的前提）
# ============================================================
async def test_level_to_action_is_the_inverse_of_metric_agg_mapping():
    """`LEVEL_TO_ACTION` == `LEVEL_TO_DECISION`（同一份 1:1 映射，不能各写一份）。"""
    assert LEVEL_TO_ACTION == LEVEL_TO_DECISION
    assert {v: k for k, v in DECISION_TO_LEVEL.items()} == LEVEL_TO_ACTION
    assert set(LEVEL_TO_ACTION) == set(LEVEL_ORDER)


async def test_unknown_level_actions_fail_closed():
    """未知等级一律 `review`，**绝不 `pass`**（脏数据的方向不可知）。"""
    assert action_for("critical") == "review"
    assert action_for("") == "review"
    assert action_for("low") == "pass"
