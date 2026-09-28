# -*- coding: utf-8 -*-
"""决策 → 指标桶 `$inc` 载荷构造（模块 11 §3.10 / §4.1，BR-11-04/05/06/12/13/14）。

**纯函数，无 IO**：把一次决策翻译成 `3 + N` 份 upsert 载荷（`global` + `level` +
`scene` + 每条命中规则的 `rule`）。正因为是纯函数，口径（资损怎么算、直方图怎么分档、
未知场景怎么办）才能被精确单测——这些口径一旦算错，大盘上看到的所有数字都是错的。

**只 `$inc` + `$setOnInsert`**（BR-11-05）：不做"读-改-写"。读改写在高并发下必然丢更新，
而 AD-03 要求写入时增量聚合的全部意义就在于并发安全。
"""
from __future__ import annotations

from typing import Any, Optional

from app.engine.metric_bucket import align, bucket_id, expire_at

# 事件类型 -> 场景码。**注意 `order_pay -> pay`**：
# 模块 05 的 BR-05-08 原先把 order_pay 映射到 order，那会让 E06 定义的 `pay`
# 场景永远不可达（N-11-6）。决策 D12 已修正为 pay，此处遵从 D12 与 E06。
EVENT_SCENE_MAP: dict[str, str] = {
    "login": "login",
    "coupon_receive": "coupon",
    "order_create": "order",
    "order_pay": "pay",
    "after_sale_apply": "aftersale",
}

# 场景筛选枚举：E06 的五个业务场景（`common` 不产生自己的桶，见 BR-11-12/N-11-7）
SCENE_KEYS: tuple[str, ...] = ("login", "coupon", "order", "pay", "aftersale")
UNKNOWN_SCENE = "unknown"

# 决策 <-> 风险等级的 1:1 映射（BR-05-17 / BR-11-12）。
# 它是"交叉筛选"能成立的全部依据：scene 桶的分档计数可直接当等级分布用，
# rule 桶的分档计数可直接当该等级下的命中数用——因此**不需要**第 5 个维度。
DECISION_TO_LEVEL: dict[str, str] = {"pass": "low", "review": "medium", "reject": "high"}
LEVEL_TO_DECISION: dict[str, str] = {v: k for k, v in DECISION_TO_LEVEL.items()}
LEVEL_KEYS: tuple[str, ...] = ("low", "medium", "high")

# 延迟直方图分桶边界（BR-11-13），最后一个键是 +∞
HIST_BOUNDS: tuple[int, ...] = (5, 10, 20, 30, 50, 75, 100, 200)
HIST_KEYS: tuple[str, ...] = tuple(f"le{b}" for b in HIST_BOUNDS) + ("gt200",)

# 资损口径（BR-11-14 / 悬空点 G-09）：仅 reject 计入；`login` 恒为 0
SAVED_AMOUNT_TYPES: tuple[str, ...] = (
    "login", "coupon_receive", "order_create", "order_pay", "after_sale_apply",
)


def hist_bucket(elapsed_ms: Optional[int]) -> Optional[str]:
    """把耗时映射到直方图分桶键；无法判定时返回 `None`（不计数）。"""
    if elapsed_ms is None:
        return None
    value = int(elapsed_ms)
    for bound in HIST_BOUNDS:
        if value <= bound:
            return f"le{bound}"
    return "gt200"


def saved_amount_for(event_type: str, decision: str, amount: Optional[int]) -> int:
    """单条事件的资损贡献（BR-11-14）：仅 `reject` 计入，`login` 恒为 0。"""
    if decision != "reject" or event_type not in SAVED_AMOUNT_TYPES:
        return 0
    if event_type == "login":
        return 0
    return int(amount or 0)


def scene_of(event_type: str) -> str:
    """事件类型 -> 场景码；无法映射时返回 `unknown`（BR-11-06，不阻断其余维度）。"""
    return EVENT_SCENE_MAP.get(event_type, UNKNOWN_SCENE)


def _base_doc(bucket_type: str, bucket_key: str, granularity: str, bucket_ts: int,
              now_ms: int) -> dict[str, Any]:
    return {
        "_id": bucket_id(bucket_type, bucket_key, granularity, bucket_ts),
        "bucket_type": bucket_type,
        "bucket_key": bucket_key,
        "granularity": granularity,
        "bucket_ts": bucket_ts,
        # 静态字段用 setOnInsert：重复 upsert 不该反复刷新创建时间
        "set_on_insert": {"created_at": now_ms},
        "expire_at": expire_at(granularity, bucket_ts),
    }


def build_payloads(
    *,
    event: dict,
    decision: dict,
    hits: list[dict],
    granularity: str = "1m",
    now_ms: int,
) -> list[dict]:
    """构造一次决策对应的全部桶 upsert 载荷（BR-11-04：`3 + N` 份）。

    入参见 §3.9：`event={event_id,event_type,user_id,ts,amount}`、
    `decision={scene_code,risk_level,decision,final_score,elapsed_ms}`、
    `hits=[{rule_code,score}]`。

    **`scene_code` 以事件类型映射为准**，不轻信传入值：`common` 规则的命中要计入
    事件实际场景的桶（N-11-7），若沿用规则的场景码就会凭空造出 `common` 桶。
    """
    event_type = str(event.get("event_type") or "")
    scene = scene_of(event_type)
    level = DECISION_TO_LEVEL.get(str(decision.get("decision") or ""), None)
    amount = saved_amount_for(event_type, str(decision.get("decision") or ""),
                              event.get("amount"))
    score = decision.get("final_score")
    elapsed = decision.get("elapsed_ms")
    bucket_ts = align(int(event.get("ts") or now_ms), granularity)
    hist_key = hist_bucket(elapsed)

    shared_inc: dict[str, Any] = {
        "metrics.event_cnt": 1,
        "metrics.score_sum": int(score) if score is not None else 0,
        "metrics.score_cnt": 1 if score is not None else 0,
        "metrics.estimated_saved_amount": amount,
        f"metrics.saved_amount_by_type.{event_type}": amount
        if event_type in SAVED_AMOUNT_TYPES else 0,
    }
    # 三档计数：键名与决策值一致，便于 BR-11-12 的映射直接使用
    for key, field in (("pass", "pass_cnt"), ("review", "review_cnt"), ("reject", "reject_cnt")):
        shared_inc[f"metrics.{field}"] = 1 if decision.get("decision") == key else 0

    payloads: list[dict] = []

    # ① global：全量字段（含延迟统计）
    global_inc = dict(shared_inc)
    global_inc["metrics.elapsed_sum"] = int(elapsed) if elapsed is not None else 0
    global_inc["metrics.elapsed_cnt"] = 1 if elapsed is not None else 0
    if hist_key:
        global_inc[f"metrics.elapsed_hist.{hist_key}"] = 1
    payloads.append({**_base_doc("global", "all", granularity, bucket_ts, now_ms),
                     "inc": global_inc})

    # ② level：按风险等级分档
    if level is not None:
        level_inc = {
            "metrics.event_cnt": 1,
            "metrics.score_sum": int(score) if score is not None else 0,
            "metrics.score_cnt": 1 if score is not None else 0,
            "metrics.estimated_saved_amount": amount,
            f"metrics.saved_amount_by_type.{event_type}": amount
            if event_type in SAVED_AMOUNT_TYPES else 0,
        }
        for key in ("pass_cnt", "review_cnt", "reject_cnt"):
            level_inc[f"metrics.{key}"] = 0
        # 本等级对应的那一档计 1（BR-11-12 的 1:1 映射）
        level_inc[f"metrics.{LEVEL_TO_DECISION[level]}_cnt"] = 1
        payloads.append({**_base_doc("level", level, granularity, bucket_ts, now_ms),
                         "inc": level_inc})

    # ③ scene：按事件实际场景分档
    scene_inc = {k: v for k, v in shared_inc.items()}
    payloads.append({**_base_doc("scene", scene, granularity, bucket_ts, now_ms),
                     "inc": scene_inc})

    # ④ rule：**每条命中一次**（N = 命中条数）
    for hit in hits:
        rule_code = hit.get("rule_code")
        if not rule_code:
            continue
        rule_inc: dict[str, Any] = {
            "metrics.hit_cnt": 1,
            "metrics.pass_hit_cnt": 1 if decision.get("decision") == "pass" else 0,
            "metrics.review_hit_cnt": 1 if decision.get("decision") == "review" else 0,
            "metrics.reject_hit_cnt": 1 if decision.get("decision") == "reject" else 0,
            "metrics.hit_score_sum": int(hit.get("score") or 0),
        }
        payloads.append({**_base_doc("rule", str(rule_code), granularity, bucket_ts, now_ms),
                         "inc": rule_inc})

    return payloads
