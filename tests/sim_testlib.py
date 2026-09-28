# -*- coding: utf-8 -*-
"""模块 10（事件仿真测试）测试的公共助手。

文件名不以 `test_` 开头，pytest 不会收集它——这是**刻意的**：
`tests/event_testlib.py` 的模块 docstring 记录过一次真实踩坑——带
`autouse` 夹具的测试模块被另一个测试模块导入时，pytest 的夹具缓存会跨模块
复用实例并触发 `assert not self._finalizers`。因此"可被多个测试文件复用"
的东西一律放进非测试模块，各测试文件只在**自己的**夹具体里调用这里的函数。
"""
from __future__ import annotations

from typing import Any, Optional

from app import db as db_module
from app.constants import (
    COLL_DECISIONS,
    COLL_DECISION_HITS,
    COLL_FEATURE_SNAPSHOTS,
    COLL_METRIC_BUCKETS,
    COLL_RISK_CASES,
    COLL_RISK_EVENTS,
    COLL_SIM_CASES,
    COLL_SIM_RUNS,
)
from app.utils.timeutil import date_key, now_ms

#: 逐次递增的序号池（保证同一进程内生成的编号**互不相同**）。
#:
#: 为什么不能只靠时间戳：`now_ms() % 10**12` 在**同一毫秒内**生成两个编号时
#: 会完全相同，而仿真用例常在一个循环里连发两次（一致性比对、批量回放）。
#: 编号撞车的后果不是报错——`FeatureWindow._seen` 会按编号去重，
#: 表现为"第二次仿真没有入窗"，于是"窗口是否被污染"的实测会得出**错误结论**。
#: 这是本模块最危险的一类假绿，因此在源头就排除。
_SEQ = [0]


def unique_event_id() -> str:
    """生成一个合法的 `EVT{yyyyMMdd}{12位}` 编号（跨调用唯一）。"""
    _SEQ[0] += 1
    return f"EVT{date_key(now_ms())}{now_ms() % 10 ** 9:09d}{_SEQ[0] % 1000:03d}"


#: 与 SPEC 的 E01 ~ E04 / E08 / E15 / E17 / E18 一一对应的"真实数据快照"集合。
#:
#: 它存在的唯一目的是让"仿真前后各读一次"这件事**只有一处实现**：
#: 数据隔离的验收（BR-10-05/06/08）要同时看六个集合与 04 的窗口，
#: 若每个用例各写一遍读取逻辑，迟早有一处漏读某个集合——
#: 而"漏读"恰恰会让污染逃过检查（不测就等于没做）。
REAL_STATE_COLLECTIONS: tuple[str, ...] = (
    COLL_RISK_EVENTS,
    COLL_FEATURE_SNAPSHOTS,
    COLL_DECISIONS,
    COLL_DECISION_HITS,
    COLL_RISK_CASES,
    COLL_METRIC_BUCKETS,
)


async def snapshot_real_state() -> dict[str, Any]:
    """读一次**真实业务数据**的状态（仿真前后各调一次做对比）。

    ## 为什么必须包含 04 的窗口与 11 的指标桶

    任务书 §2 的原话：「跑一次仿真前后，**真实**的特征窗口计数与 **E15 指标桶
    不得变化**；`dry_run` 只挡住 `decisions`/`decision_hits`，**窗口写入你要自己
    解决**」。因此快照里必须有：

    | 读取项 | 覆盖的规则 |
    |---|---|
    | 04 窗口的 `total_entries` / 各维度键数 / `seen_ids` / 四个计数器 | **BR-10-06**（仿真不写真实窗口）——这是本模块自己解决的那一处 |
    | `risk_events` / `feature_snapshots` | BR-10-05（不写 E01/E02） |
    | `decisions` / `decision_hits` | BR-10-05（`dry_run` 挡住的那两个） |
    | `risk_cases` | BR-10-05 + D5（降级/拦截会建案，仿真不得建） |
    | **`metric_buckets`**（E15） | **D66**：`record_decision` 只在真实路径调用，仿真绝不可触发 |

    ## 为什么返回的是"计数 + 内容指纹"而不是整份文档

    文档内容也要比对：光比条数会漏掉"仿真覆盖了某条既有记录"（同 `_id` 的
    `insert` 会失败但 `replace_one` 会成功）。因此按 `_id` 排序取一份轻量指纹
    （条数 + 排序后的 `_id` 列表）。文档**数量级**在本项目是几十到几百，
    这个成本可以忽略；而它把"污染"从"条数变了"扩展到"哪一条被动过"。
    """
    from app.services import feature_service

    window = feature_service.get_feature_service().window
    stats = window.stats()
    state: dict[str, Any] = {
        "window": {
            "total_entries": stats["total_entries"],
            "distinct_keys": stats["distinct_keys"],
            "dimensions": dict(stats["dimensions"]),
            "seen_ids": stats["seen_ids"],
            "out_of_order_cnt": stats["out_of_order_cnt"],
            "truncated_cnt": stats["truncated_cnt"],
            "duplicate_cnt": stats["duplicate_cnt"],
            "dropped_cnt": stats["dropped_cnt"],
            "estimated_bytes": stats["estimated_bytes"],
        },
        "collections": {},
    }
    database = db_module.get_db()
    for name in REAL_STATE_COLLECTIONS:
        col = database[name]
        ids = sorted(
            str(row["_id"])
            for row in await col.find({}, {"_id": 1}).to_list(length=10_000)
        )
        state["collections"][name] = {"count": len(ids), "ids": ids}
    return state


def diff_real_state(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """比对两份真实数据快照，返回**人类可读的差异清单**（空列表 = 没被污染）。

    返回清单而不是 bool：数据隔离失败时，报告里需要的正是"哪一个数字从多少
    变成了多少"。只给一句 `assert before == after` 会让人重新跑一遍才能定位。
    """
    problems: list[str] = []
    for key, was in before["window"].items():
        got = after["window"].get(key)
        if was != got:
            problems.append(f"04 特征窗口 {key}：{was} → {got}")
    for name, was in before["collections"].items():
        got = after["collections"].get(name, {})
        if was["count"] != got.get("count"):
            problems.append(
                f"{name} 条数：{was['count']} → {got.get('count')}"
            )
            continue
        if was["ids"] != got.get("ids"):
            added = sorted(set(got.get("ids", [])) - set(was["ids"]))
            removed = sorted(set(was["ids"]) - set(got.get("ids", [])))
            problems.append(f"{name} 内容变化：新增 {added[:5]} 移除 {removed[:5]}")
    return problems


def format_real_state(state: dict[str, Any], title: str) -> str:
    """把快照渲染成报告里可以直接贴的几行数字（数据隔离的实测证据）。"""
    window = state["window"]
    lines = [
        f"[{title}] 04窗口 total_entries={window['total_entries']} "
        f"distinct_keys={window['distinct_keys']} seen_ids={window['seen_ids']} "
        f"duplicate_cnt={window['duplicate_cnt']} truncated_cnt={window['truncated_cnt']}",
    ]
    for name in REAL_STATE_COLLECTIONS:
        lines.append(f"[{title}] {name} count={state['collections'][name]['count']}")
    return "\n".join(lines)


# ============================================================
# 事件模板（与 `scripts/seed.py` 的 4 条演示用例保持一致）
# ============================================================
#: E17 的 4 条演示用例（与 Spec §2.1 的表逐条对应）。
#:
#: **测试与种子共用这一份定义**：两条来路若各写一份事件模板，
#: 迟早会出现"种子里的用例能跑、测试里的跑不通"这种只在一边暴露的分歧，
#: 而它们描述的是同一个演示场景。
SIM_CASE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "name": "羊毛党批量领券",
        "category": "coupon_abuse",
        "expected_decision": "reject",
        "description": "同设备多账号集中领券，用于验证领券聚集类规则",
        "event_template": {
            "event_type": "coupon_receive",
            "user_id": "U000128",
            "device_id": "D8F2A1C4",
            "ip": "117.136.12.88",
            "address_id": "ADDR-7712",
            "amount": 5000,
            "scene_extra": {
                "coupon_id": "CP-2026-0001", "activity_id": "ACT-2026-01",
                "face_value": 5000, "batch_id": "B-01",
            },
        },
    },
    {
        "name": "恶意退款欺诈",
        "category": "aftersale_abuse",
        "expected_decision": "review",
        "description": "高退款率 + 同地址售后聚集，用于验证售后滥用类规则",
        "event_template": {
            "event_type": "after_sale_apply",
            "user_id": "U000128",
            "biz_no": "AS-2026-000001",
            "address_id": "ADDR-7712",
            "amount": 29900,
            "scene_extra": {
                "after_sale_no": "AS-2026-000001", "order_no": "SO-2026-000001",
                "reason_code": "not_received", "refund_amount": 29900,
                "received_goods": False,
            },
        },
    },
    {
        "name": "正常下单支付",
        "category": "normal",
        "expected_decision": "pass",
        "description": "干净账号的正常支付，用于批量回放时观察误伤",
        "event_template": {
            "event_type": "order_pay",
            "user_id": "U000130",
            "amount": 19900,
            # ⚠️ **不要加 `success`**：`order_pay` 的 `scene_extra` 白名单是
            # `(order_no, pay_channel)` + `(pay_amount, card_tail)`，多一个键会触发
            # `EVT-4006`。实测后果：批量回放 20 条**全部** `SIM-4001` 失败，
            # 而表层症状只是"统计数字全是 0"，非常难定位（`run_ids` 里是一串空串）。
            "scene_extra": {
                "order_no": "SO-2026-000002", "pay_channel": "alipay",
                "pay_amount": 19900, "card_tail": "8899",
            },
        },
    },
    {
        "name": "代理 IP 聚集登录",
        "category": "boundary",
        "expected_decision": "review",
        "description": "边界样本：名单干净，但代理 IP + 同设备多账号",
        "event_template": {
            "event_type": "login",
            "user_id": "U000132",
            "device_id": "DMULE0001",
            "ip": "203.0.113.88",
            "phone": "13500000132",
            "scene_extra": {
                "login_type": "pwd",
                "ua": "Mozilla/5.0 (Linux; Android 12; SM-A5360)",
                "success": True,
            },
        },
    },
)


async def install_sim_cases(rows: Optional[tuple[dict[str, Any], ...]] = None) -> list[str]:
    """把演示用例直接写进 `sim_cases`（**绕过服务层的写路径**）。

    为什么绕过：`SimService.save_case` 会写一条 `sim.case.create` 审计，
    而"审计恰好一条"是本模块自己要验的断言之一——若夹具也用服务层灌用例，
    那些审计会污染断言（表现为"记了 5 条，期望 1 条"）。
    测试夹具直接落库是既有的做法（`engine_testlib.install_rules` 同理）。
    """
    col = db_module.get_db()[COLL_SIM_CASES]
    ids: list[str] = []
    ts = now_ms()
    for index, row in enumerate(rows or SIM_CASE_TEMPLATES, start=1):
        case_id = f"SIMC2026010100{index:04d}"
        doc = {
            "_id": case_id,
            "name": row["name"],
            "category": row["category"],
            "expected_decision": row["expected_decision"],
            "description": row.get("description"),
            "event_template": dict(row["event_template"]),
            "created_by": "seed",
            "created_at": ts - index,
            "status": "active",
        }
        await col.replace_one({"_id": case_id}, doc, upsert=True)
        ids.append(case_id)
    return ids


async def clear_sim_collections() -> None:
    """清空 E17/E18（供不依赖 conftest 的纯逻辑用例使用）。"""
    for name in (COLL_SIM_CASES, COLL_SIM_RUNS):
        await db_module.get_db()[name].delete_many({})


__all__ = [
    "REAL_STATE_COLLECTIONS",
    "SIM_CASE_TEMPLATES",
    "clear_sim_collections",
    "diff_real_state",
    "format_real_state",
    "install_sim_cases",
    "snapshot_real_state",
    "unique_event_id",
]
