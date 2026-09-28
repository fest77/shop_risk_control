# -*- coding: utf-8 -*-
"""18 项特征的**静态参照区间**配置（模块 04 BR-04-16，纯数据 + 纯函数）。

## 这份配置解决的是哪一处缺口

`03_原型图` 的快照表有「基线值 / 偏离」两列（`≤1`、`2~5`、`false`、`≤5%`），
而 `01_数据实体` 的 E02 **没有存储基线的字段**。Spec §4.5 把它补定义为
「静态参照区间」：每个特征一条，集中在本文件，经 `GET /features/meta` 下发。
`BR-04-16` 明确 **前端不得硬编码**——一旦前端各写一份，后端改了口径前端不会跟着变，
研判人员就会拿着过期的标尺看数据。

## 为什么这里**不存**任何统计量

E21 `feature_baselines`（决策 D9）是**统计基线**（P50/P95，每日刷新，样本
不足即标记不可用），它与本文件的静态参照是**两个不同的东西**：

| | 静态参照（本文件） | 统计基线（E21 集合） |
|---|---|---|
| 含义 | 「这个值算不算异常」的行业/业务常识 | 「近期真实分布处在什么水平」 |
| 来源 | 人工配置，随 Spec 冻结 | 定时任务从历史快照算出来 |
| 取值 | `≤1` / `2~5` / `false` / `≤5%` | P50 / P95 数值 |
| 缺失时 | 显示 `—`（BR-04-19） | 集合里查不到 → 显示 `—` |

两者都通过 `/features/meta` 下发（统计基线在 `stat_baseline` 键下），
但**不合并成一个字段**——合并后就再也说不清「这个数字是拍脑袋定的还是算出来的」，
而那恰恰是复核时最需要分清的一件事。

## 为什么 `direction_hint` 也要后端给

「偏离」列由前端派生（BR-04-18：后端不下发文案）。但"往哪个方向偏算异常"
是**业务判断**（`user_age_days` 越小越可疑、`user_level` 越高越正常），
让前端各自猜就会出现"两个页面算法不一致"。因此后端只给方向提示，
文案与倍数由前端渲染——既满足 BR-04-18，又不把业务判断散落到各处。

## 为什么无基线比有基线更要多写一句说明

`BR-04-19`：无基线的项显示 `—` 而不是「正常」，避免被误读成"已判定正常"。
因此 `user_age_days` / `user_level` / `user_risk_tag_cnt` 三项**显式**标为
无基线（而不是漏填）——漏填与刻意的"没有绝对基线"在代码里看起来一样，
但只有后者是对的，所以 `baseline_reason` 里写明理由。
"""
from __future__ import annotations

from typing import Any

#: 「无基线」的哨兵取值。前端据此显示 `—`（BR-04-19）。
#:
#: 为什么用哨兵字符串而不是 `None`：`envelope` 序列化后 `None` 与"字段缺失"
#: 在前端看来完全一样，而这两件事的含义不同（一个是"我们决定不给基线"，
#: 一个是"接口漏了"）。显式的 `__none__` 让漏填立刻可见。
NO_BASELINE = "__none__"

#: 方向提示的合法取值（`|` 连接两档，前端按当前值挑一档渲染）
DIRECTION_LOW = "low_is_risky"
DIRECTION_HIGH = "high_is_risky"
DIRECTION_NONE = "none"
DIRECTION_NON_NUMERIC = "non_numeric"

VALID_DIRECTIONS: frozenset[str] = frozenset({
    DIRECTION_LOW, DIRECTION_HIGH, DIRECTION_NONE, DIRECTION_NON_NUMERIC,
})

#: 分组名（与 Spec §4.2 的五个分组逐字一致）
GROUP_FREQ = "行为频次"
GROUP_DEVICE = "设备环境"
GROUP_IP = "网络 IP"
GROUP_ADDRESS = "地址聚集"
GROUP_PROFILE = "账号画像"

# ============================================================
# 静态参照区间表（BR-04-16 的「集中定义」）
# ------------------------------------------------------------
# 结构：{feature_name: {...}}
#   reference        : 展示用区间文本；NO_BASELINE 表示无基线
#   direction_hint   : 偏离方向提示（前端派生「偏离」列的依据，BR-04-18）
#   baseline_reason  : 为什么是这个区间 / 为什么没有基线（写入 baseline_desc 下发）
# ============================================================
REFERENCE_BASELINES: dict[str, dict[str, str]] = {
    # ---------- 分组一 · 行为频次 ----------
    "login_cnt_1h": {
        "reference": "≤3",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "正常用户 1 小时内不会反复登录；超过 3 次多与撞库/多开有关",
    },
    "coupon_cnt_1h": {
        "reference": "≤2",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "正常用户近 1h 领券不超过 2 张（原型图示例值，直接采用）",
    },
    "order_cnt_1h": {
        "reference": "≤2",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "1 小时内多次下单是刷单的典型特征，正常不超过 2 单",
    },
    "order_cnt_24h": {
        "reference": "2~5",
        "direction_hint": DIRECTION_NONE,
        "baseline_reason": "正常下单频次区间；**两侧都算偏离**，故方向提示为 none（原型图示例值）",
    },
    "pay_fail_cnt_24h": {
        "reference": "≤3",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "偶发支付失败正常；24h 内超过 3 次多与盗卡试探有关",
    },
    "aftersale_cnt_24h": {
        "reference": "≤2",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "24 小时内频繁申请售后是退款欺诈的典型特征（原型图模式 refund 的观测口径）",
    },
    "aftersale_rate_24h": {
        "reference": "≤5%",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "正常退款率上限（原型图示例值）；注意分母为 0 时本项**标记缺失**而不是 0（BR-04-08）",
    },

    # ---------- 分组二 · 设备环境 ----------
    "device_user_cnt": {
        "reference": "≤1",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "一台设备正常只对应 1 个账号（原型图示例值）；取值来自模块 09，本模块不自行去重（BR-04-07）",
    },
    "device_order_cnt_1h": {
        "reference": "≤3",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "同一设备 1 小时内多次下单是设备聚集刷单的信号",
    },
    "device_age_hours": {
        "reference": "≥24",
        "direction_hint": DIRECTION_LOW,
        "baseline_reason": "新设备（出现不足 24 小时）风险更高，故方向为「偏低异常」（E11 口径，窗口内首次出现时间）",
    },

    # ---------- 分组三 · 网络 IP ----------
    "ip_user_cnt": {
        "reference": "≤2",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "同 IP 少量账号共享（家庭/公司网络）正常；上限 2，取值来自模块 09",
    },
    "ip_order_cnt_1h": {
        "reference": "≤3",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "同一 IP 1 小时内多次下单是 IP 聚集刷单的信号",
    },
    "ip_is_proxy": {
        "reference": "false",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "正常用户不应从代理/机房出口访问（原型图示例值）；`true` 即偏离",
    },

    # ---------- 分组四 · 地址聚集 ----------
    "address_user_cnt": {
        "reference": "≤2",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "同一收货地址关联多个账号是地址聚集的信号；上限 2，取值来自模块 09",
    },
    "address_aftersale_cnt": {
        "reference": "≤2",
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "同地址历史售后总次数上限（E13.aftersale_cnt 的长期累计口径）",
    },

    # ---------- 分组五 · 账号画像 ----------
    "user_age_days": {
        "reference": NO_BASELINE,
        "direction_hint": DIRECTION_NONE,
        "baseline_reason": "无绝对基线：新号与老号都正常。BR-04-19 要求显示 `—` 而不是「正常」，避免被误读成已判定正常",
    },
    "user_level": {
        "reference": NO_BASELINE,
        "direction_hint": DIRECTION_NON_NUMERIC,
        "baseline_reason": "等级是类别而非可比较的数值，无法用区间表达偏离；研判时看原值即可",
    },
    "user_risk_tag_cnt": {
        "reference": NO_BASELINE,
        "direction_hint": DIRECTION_HIGH,
        "baseline_reason": "无绝对基线：标签数量取决于历史处置，0 与 3 都不能单独判为异常；但方向明确——越多越可疑",
    },
}


def reference_of(feature_name: str) -> str:
    """取某特征的静态参照区间文本；未登记即 `NO_BASELINE`。

    未登记**不抛异常**：新增特征时先上线、后补基线是正常的演进顺序，
    此时前端显示 `—` 正是正确行为（BR-04-19）。抛异常会让"忘了写基线"
    升级成"接口 500"，代价远大于显示 `—`。
    """
    return REFERENCE_BASELINES.get(feature_name, {}).get("reference", NO_BASELINE)


def has_baseline(feature_name: str) -> bool:
    """该特征是否有可展示的参照区间（BR-04-19 的判定）。"""
    return reference_of(feature_name) != NO_BASELINE


def direction_hint_of(feature_name: str) -> str:
    """偏离方向提示；未登记返回 `none`（前端此时也无从派生，与无基线一致）。"""
    return REFERENCE_BASELINES.get(feature_name, {}).get(
        "direction_hint", DIRECTION_NONE
    )


def baseline_reason_of(feature_name: str) -> str:
    """基线的理由说明（下发给前端展示，也供评审核对每个数字的来源）。"""
    return REFERENCE_BASELINES.get(feature_name, {}).get("baseline_reason", "")


def baseline_meta(feature_name: str) -> dict[str, Any]:
    """打包成 `/features/meta` 的基线字段块。

    字段名与 Spec §3.3 的 `baseline` / `baseline_desc` 对齐；`baseline_desc`
    同时承载"为什么是这个区间"与"为什么没有基线"，前者让评审能核对数字来源，
    后者避免无基线被误读成遗漏。
    """
    reference = reference_of(feature_name)
    return {
        "baseline": reference,
        "baseline_desc": baseline_reason_of(feature_name),
        "has_baseline": reference != NO_BASELINE,
        "direction_hint": direction_hint_of(feature_name),
    }


def assert_baselines_are_sane() -> None:
    """自检配置内部一致性（由 `feature_meta.build_items` 与测试调用）。

    只检查**结构**（方向取值合法、有基线的必须给理由、无基线的不许配区间型方向）
    ——「上限取 3 还是 2」属业务判断，代码判不出来，硬编一个数字进校验只会让
    将来调参时误报。

    关于方向的约束分两种情况，因为它们说的是两件事：

    - **有基线**：必须给方向（区间型 `2~5` 除外，它两侧都算偏离 → `none`），
      否则前端拿到标尺却永远不会标偏离；
    - **无基线**：允许 `none` / `non_numeric`（无法派生偏离），也允许 `high_is_risky`
      ——后者表示"没有绝对区间，但越大越可疑"（如 `user_risk_tag_cnt`），
      前端据此渲染「▲ 偏多」而不给倍数。**禁止** `low_is_risky`：它必然要配一个
      下限（如 `≥24`），而下限就是一条基线，与"无基线"自相矛盾。
    """
    for name, cfg in REFERENCE_BASELINES.items():
        if not name or not name.strip():
            raise AssertionError("静态基线表存在空特征名")
        direction = cfg.get("direction_hint")
        if direction not in VALID_DIRECTIONS:
            raise AssertionError(
                f"特征 {name} 的 direction_hint={direction!r} 不在 {sorted(VALID_DIRECTIONS)} 中"
            )
        reference = cfg.get("reference")
        if not reference:
            raise AssertionError(f"特征 {name} 缺少 reference（无基线请显式写 NO_BASELINE）")
        if not cfg.get("baseline_reason"):
            raise AssertionError(f"特征 {name} 未写明 baseline_reason（BR-04-19 要求可解释）")
        if reference == NO_BASELINE:
            if direction == DIRECTION_LOW:
                raise AssertionError(
                    f"特征 {name} 无基线却声明「偏低异常」——该方向必然要配一个下限，"
                    "下限就是基线，两者自相矛盾；请补 reference 或改为 none"
                )
        elif direction == DIRECTION_NONE and name != "order_cnt_24h":
            # `order_cnt_24h` 是**区间**基线（2~5），两侧都算偏离，故方向必须是 none；
            # 其余有单侧基线的项若写成 none，等于前端永远不会标偏离
            raise AssertionError(
                f"特征 {name} 有基线但未给方向提示，前端无法派生「偏离」列"
            )


__all__ = [
    "DIRECTION_HIGH",
    "DIRECTION_LOW",
    "DIRECTION_NONE",
    "DIRECTION_NON_NUMERIC",
    "GROUP_ADDRESS",
    "GROUP_DEVICE",
    "GROUP_FREQ",
    "GROUP_IP",
    "GROUP_PROFILE",
    "NO_BASELINE",
    "REFERENCE_BASELINES",
    "VALID_DIRECTIONS",
    "assert_baselines_are_sane",
    "baseline_meta",
    "baseline_reason_of",
    "direction_hint_of",
    "has_baseline",
    "reference_of",
]
