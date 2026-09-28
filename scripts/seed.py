# -*- coding: utf-8 -*-
"""统一种子脚本：建索引 + 灌种子数据（BR-00-24 / 25 / 26）。

用法（在项目根目录执行）：

    .venv\\Scripts\\python.exe scripts\\seed.py                 # 建索引 + 幂等灌种子
    .venv\\Scripts\\python.exe scripts\\seed.py --reset         # 先清空业务集合再灌
    .venv\\Scripts\\python.exe scripts\\seed.py --indexes-only  # 只建索引

**幂等性怎么保证**：
- 索引：`create_index` 本身幂等
- 名单：走 `ListService.create_entry`，重复条目被唯一索引/冲突判定拦下并跳过
- 场景与用户：按 `_id` 做 `replace_one(upsert=True)`，重复执行结果一致

**可复现性（BR-00-25）**：所有种子值都是**字面量常量**，没有随机数。这比
"固定随机种子"更强——随机数生成器的实现变化都不会影响种子结果。
`--reset` 连 `seq_counters` 一起清空，因此业务编号会从 1 重新开始，两次
`--reset` 后数据库内容逐字节一致。

**为什么各模块的种子要放在这里而不是各模块自己写**：BR-00-24 要求"一个幂等
种子脚本"。分散的种子脚本会导致演示环境初始化需要按顺序执行多个命令，
漏掉一个就出现"页面空白但接口正常"的情况。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认编码不是 UTF-8（通常 GBK），中文输出会变乱码——
# 演示时看到一片乱码会让人怀疑数据本身有问题，因此显式把标准输出设为 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老版本或特殊重定向场景下不阻断脚本
    pass

from app import config, db  # noqa: E402
from app.constants import (  # noqa: E402
    COLLECTION_NAMES,
    COLL_AUDIT_LOGS,
    COLL_CASE_ACTIONS,
    COLL_DEVICES,
    COLL_DECISION_HITS,
    COLL_DECISIONS,
    COLL_ENTITY_EDGES,
    COLL_FEATURE_SNAPSHOTS,
    COLL_IP_POOL,
    COLL_LIST_ENTRIES,
    COLL_MODEL_CONFIGS,
    COLL_RISK_CASES,
    COLL_RISK_EVENTS,
    COLL_RULES,
    COLL_RULE_SCENES,
    COLL_SEQ_COUNTERS,
    COLL_SIM_CASES,
    COLL_SYS_USERS,
    COLL_USER_ADDRESSES,
    COLL_USERS,
)
from app.core.degraded import DEGRADED  # noqa: E402
from app.repos.list_repo import ListRepo  # noqa: E402
from app.schemas.list_schema import ListEntryCreate  # noqa: E402
from app.services.list_service import ListService  # noqa: E402
from app.security.password import hash_password  # noqa: E402
from app.utils.mask import mask_phone  # noqa: E402
from app.utils.timeutil import date_key, now_ms  # noqa: E402

# ============================================================
# E06 规则场景（6 行）
# D10 明确要求 `common`（通用场景）以**数据行**存在，禁止在代码里对字符串
# "common" 特判。排序值故意让 common 排在最后：它不对应单一事件类型，
# 属于跨场景的通用规则集合。
# 场景码取值对齐 E06：login / coupon / order / pay / aftersale / common
# （决策 D12：order_pay 事件映射到 `pay` 场景）
# ============================================================
ALL_EVENT_TYPES = [
    "login",
    "coupon_receive",
    "order_create",
    "order_pay",
    "after_sale_apply",
]

SEED_SCENES: list[dict[str, Any]] = [
    {"_id": "login", "name": "登录", "event_types": ["login"], "sort": 10},
    {"_id": "coupon", "name": "领券", "event_types": ["coupon_receive"], "sort": 20},
    {"_id": "order", "name": "下单", "event_types": ["order_create"], "sort": 30},
    {"_id": "pay", "name": "支付", "event_types": ["order_pay"], "sort": 40},
    {"_id": "aftersale", "name": "售后", "event_types": ["after_sale_apply"], "sort": 50},
    {"_id": "common", "name": "通用", "event_types": ALL_EVENT_TYPES, "sort": 90},
]

# ============================================================
# E07 名单条目（黑 3 / 白 2 / 灰 1，覆盖三种名单类型的默认有效期差异）
# ============================================================
SEED_LISTS: list[dict[str, str]] = [
    dict(list_type="black", entity_type="device", entity_value="D8F2A1C4",
         reason="羊毛党设备聚集：同设备 12 个新注册账号集中领券"),
    dict(list_type="black", entity_type="user", entity_value="U000128",
         reason="批量套券，已由案件 CASE0923001 处置确认"),
    dict(list_type="black", entity_type="address", entity_value="ADDR-7712",
         reason="地址聚集刷单：同地址关联 8 个账号"),
    dict(list_type="white", entity_type="phone", entity_value="13900000001",
         reason="内部测试账号，免风控拦截"),
    dict(list_type="white", entity_type="user", entity_value="U009999",
         reason="大客户白名单：季度采购账号"),
    dict(list_type="gray", entity_type="ip", entity_value="117.136.12.88",
         reason="疑似代理出口，仅观察不干预"),
]

# ============================================================
# E19 系统用户（三个角色各一个）
# 用户名与测试夹具/前端身份选择器一致：admin01 / strategy01 / reviewer01
# 密码为**初始演示口令**，模块 01（登录与鉴权）接入后由登录链路校验；
# 凭据策略（强度、锁定、改密）属模块 01/13 的范围。
# ============================================================
SEED_USERS: list[dict[str, str]] = [
    {"username": "admin01", "password": "admin123", "real_name": "张运维", "role": "admin"},
    {"username": "strategy01", "password": "strategy123", "real_name": "李策略", "role": "strategist"},
    {"username": "reviewer01", "password": "reviewer123", "real_name": "王审核", "role": "reviewer"},
]

# real_name 用**人名**而不是角色名：界面已单独显示角色标签，
# 若姓名也写"风控策略师"，侧栏会出现"风控策略师 / strategy01 · 风控策略师"的重复。

# 各模块实现后需要在此补充种子数据的集合（如实列出覆盖缺口，不静默跳过）
# 模块 09（E10~E14）已落地，相关条目已移除（见 `seed_profiles`）
# 模块 08（E08/E09）已落地：`risk_cases` / `case_actions` 由 `seed_demo_cases`
# 走**真实链路**产出（不是手写文档），因此相关条目已移除
PENDING_SEEDS: dict[str, str] = {
    "risk_events": "E01 风控事件 → 模块 03（运行时由事件网关产生；"
                   "种子只预置两条演示事件，见 `seed_demo_decision` / `seed_demo_cases`）",
    "feature_snapshots": "E02 特征快照 → 模块 04（随决策产生）",
    "decisions": "E03 决策记录 → 模块 05 已落地（随事件决策产生，不预置）",
    "decision_hits": "E04 决策命中明细 → 模块 05 已落地（随命中产生，不预置）",
    "metric_buckets": "E15 指标桶 → 模块 11（决策写入时增量聚合产生）",
    "audit_logs": "E16 审计日志 → 模块 12（由真实写操作产生，不预置）",
    "sim_cases": "E17 仿真用例 → 模块 10 已落地（4 条演示用例，见 `seed_sim_cases`）",
    "sim_runs": "E18 仿真执行记录 → 模块 10 已落地（运行仿真时产生，不预置）",
    "feature_baselines": "E21 特征基线 → 模块 04/07（每日刷新任务产生）",
    # 模块 13 已落地：运行参数**刻意不预置**——"从未保存过"是一种合法状态，
    # 此时按代码默认值运行（`GET /system/config` 的 `config_version=w1`），
    # 与 04 的 `WINDOW_CONFIG_VERSION="w1"` 是同一个值。预置一份等于多一个
    # "种子值 vs 代码默认值"的漂移面，而它们必须永远相等。
    "system_config": "运行参数 → 模块 13（未保存时按代码默认值运行；首次保存后落库，不预置）",
}


# ============================================================
# E05 风控规则（模块 05 的预设规则集）
# ------------------------------------------------------------
# ## 为什么必须有这套种子
#
# E05 是**决策的输入**：没有规则，`rule_score` 恒为 0，三档决策里只有 `pass`
# 可达——`review`/`reject` 与模块 08 的建案、07 的处置、11 的命中排行全部
# 没有数据可展示（V-05-01 的三档验收也就无从谈起）。
#
# ## `field` 只能取 E02 的 18 项特征键（任务书 §15 / `feature_compute.FEATURE_KEYS`）
#
# 引用一个不存在的特征名**不会报错**：求值时取不到值 → 条件恒为假 → 规则
# **永不命中**。页面上一堆"配好了的规则"却一条都不生效，是最难排查的形态。
# 因此这里的每一棵树都只用真实键，并由 `tests/test_engine_decision.py` 逐条比对。
#
# ## 三档是怎么被这套种子构造出来的（V-05-01）
#
# `login` 场景的三条规则分值刻意取 **45 / 20 / 25**（而不是 40/30/20），
# 因为三档里**最需要被演示的那一档（medium/review）必须能由"只用持久化画像"
# 的特征组合构造出来**——滑动窗口只活在进程内存里（悬空点 G-02），
# 服务一重启就不可复现，靠它凑出来的分数没人能再算出来：
#
#   0 分   → 一个干净用户：三条都不命中                        → pass
#   65 分  → 同设备 5+ 账号 + 近 1h 登录 10+ 次（用到窗口）      → review
#   **70 分 → 同设备 5+ 账号 + 代理 IP 且账号很新（不用窗口）**  → review ← 演示主路径
#   90 分  → 上面三条全中                                      → reject
#
# 那个 70 分正是 `SCORING_USERS`（团伙 B，`U000132`）要展示的档位：
# **名单干净、但规则实打实累加出 2 条命中**，前端才看得到"计分 + 命中明细表"。
#
# 分值刻意让"三条全中"停在 90 而不是溢出到 100：截断（BR-05-16）确实会兜住
# 溢出，但"靠截断才达标"的种子无法区分"累加对了"与"截断掩盖了重复计分"。
#
# ## 幂等
#
# 按 `_id`（即规则编码）`replace_one(upsert=True)`，与 E06 场景同一写法。
# 不做"先查再插"：并发下必然漏判，而且 06 后续会改这些规则（改分/改名/停用），
# 覆盖式 upsert 会让"重跑种子"回到**种子定义的初始状态**——这正是可复现的要求
# （BR-00-25），比"已存在就跳过"更符合种子脚本的语义。
# ============================================================
SEED_RULES: list[dict[str, Any]] = [
    # ---------- login：登录场景（三档的载体） ----------
    {
        "_id": "RLOGIN001", "name": "同设备聚集登录", "scene_code": "login",
        "description": "同一设备指纹下关联 5 个以上账号，疑似团伙/养号登录",
        "condition": {"logic": "and", "children": [
            {"field": "device_user_cnt", "op": "gte", "value": 5},
        ]},
        "score": 45, "status": "enabled", "priority": 10, "version": 1, "is_system": True,
    },
    {
        "_id": "RLOGIN002", "name": "高频登录", "scene_code": "login",
        "description": "近 1 小时登录 10 次以上，疑似撞库或盗号试探",
        "condition": {"logic": "and", "children": [
            {"field": "login_cnt_1h", "op": "gte", "value": 10},
        ]},
        "score": 20, "status": "enabled", "priority": 20, "version": 1, "is_system": True,
    },
    {
        "_id": "RLOGIN003", "name": "代理IP新账号登录", "scene_code": "login",
        "description": "代理/机房 IP，且账号是新注册或已带风险标签（and 套 or）",
        "condition": {"logic": "and", "children": [
            {"field": "ip_is_proxy", "op": "eq", "value": True},
            {"logic": "or", "children": [
                {"field": "user_age_days", "op": "lte", "value": 7},
                {"field": "user_risk_tag_cnt", "op": "gte", "value": 2},
            ]},
        ]},
        "score": 25, "status": "enabled", "priority": 30, "version": 1, "is_system": True,
    },
    {
        # **停用**的规则：它存在的价值是证明 `status=disabled` 真的不参与决策。
        # 条件刻意写得"一碰就中"（任意登录都命中）且分值 100——若状态过滤失效，
        # 每个登录事件都会直接变成 reject，一眼就能看出来。
        "_id": "RLOGIN901", "name": "【已停用示例】可疑登录一律拦截", "scene_code": "login",
        "description": "演示用停用规则：证明 status=disabled 的规则不参与求值",
        "condition": {"logic": "and", "children": [
            {"field": "login_cnt_1h", "op": "gte", "value": 1},
        ]},
        "score": 100, "status": "disabled", "priority": 99, "version": 1, "is_system": False,
    },

    # ---------- coupon：领券场景 ----------
    {
        "_id": "RCOUPON001", "name": "领券设备聚集", "scene_code": "coupon",
        "description": "同设备关联 8 个以上账号且近 1h 领券 5 次以上（典型羊毛党设备）",
        "condition": {"logic": "and", "children": [
            {"field": "device_user_cnt", "op": "gte", "value": 8},
            {"field": "coupon_cnt_1h", "op": "gte", "value": 5},
        ]},
        "score": 45, "status": "enabled", "priority": 10, "version": 1, "is_system": True,
    },
    {
        "_id": "RCOUPON002", "name": "新账号同IP批量领券", "scene_code": "coupon",
        "description": "同 IP 关联 5 个以上账号且账号注册不满 3 天",
        "condition": {"logic": "and", "children": [
            {"field": "ip_user_cnt", "op": "gte", "value": 5},
            {"field": "user_age_days", "op": "lte", "value": 3},
        ]},
        "score": 35, "status": "enabled", "priority": 20, "version": 1, "is_system": True,
    },

    # ---------- order：下单场景 ----------
    {
        "_id": "RORDER001", "name": "地址聚集刷单", "scene_code": "order",
        "description": "同收货地址关联 5 个以上账号且近 1h 下单 3 单以上",
        "condition": {"logic": "and", "children": [
            {"field": "address_user_cnt", "op": "gte", "value": 5},
            {"field": "order_cnt_1h", "op": "gte", "value": 3},
        ]},
        "score": 40, "status": "enabled", "priority": 10, "version": 1, "is_system": True,
    },
    {
        "_id": "RORDER002", "name": "新设备密集下单", "scene_code": "order",
        "description": "设备首次出现不足 24 小时且近 24h 已下单 3 单以上",
        "condition": {"logic": "and", "children": [
            {"field": "device_age_hours", "op": "lt", "value": 24},
            {"field": "order_cnt_24h", "op": "gte", "value": 3},
        ]},
        "score": 30, "status": "enabled", "priority": 20, "version": 1, "is_system": True,
    },

    # ---------- pay：支付场景（决策 D12：order_pay 映射到 pay，本场景必须可达） ----------
    {
        "_id": "RPAY001", "name": "支付失败试探", "scene_code": "pay",
        "description": "近 24h 支付失败 3 次以上，疑似盗卡试探",
        "condition": {"logic": "and", "children": [
            {"field": "pay_fail_cnt_24h", "op": "gte", "value": 3},
        ]},
        "score": 30, "status": "enabled", "priority": 10, "version": 1, "is_system": True,
    },
    {
        "_id": "RPAY002", "name": "代理IP或共享IP支付", "scene_code": "pay",
        "description": "代理/机房 IP，或同 IP 关联 6 个以上账号（or 组合两种迹象）",
        "condition": {"logic": "or", "children": [
            {"field": "ip_is_proxy", "op": "eq", "value": True},
            {"field": "ip_user_cnt", "op": "gte", "value": 6},
        ]},
        "score": 25, "status": "enabled", "priority": 20, "version": 1, "is_system": True,
    },

    # ---------- aftersale：售后场景 ----------
    {
        "_id": "RAFTER001", "name": "高退款率售后滥用", "scene_code": "aftersale",
        "description": "近 24h 退款率 ≥50% 且售后 3 次以上",
        "condition": {"logic": "and", "children": [
            {"field": "aftersale_rate_24h", "op": "gte", "value": 0.5},
            {"field": "aftersale_cnt_24h", "op": "gte", "value": 3},
        ]},
        "score": 50, "status": "enabled", "priority": 10, "version": 1, "is_system": True,
    },
    {
        "_id": "RAFTER002", "name": "同地址售后聚集", "scene_code": "aftersale",
        "description": "同收货地址历史售后 5 次以上",
        "condition": {"logic": "and", "children": [
            {"field": "address_aftersale_cnt", "op": "gte", "value": 5},
        ]},
        "score": 30, "status": "enabled", "priority": 20, "version": 1, "is_system": True,
    },

    # ---------- common：通用场景（D10：以数据行存在，对全部场景生效） ----------
    {
        "_id": "RCOMMON001", "name": "多风险标签", "scene_code": "common",
        "description": "账号已带 2 个以上风险标签（跨场景通用）",
        "condition": {"logic": "and", "children": [
            {"field": "user_risk_tag_cnt", "op": "gte", "value": 2},
        ]},
        "score": 20, "status": "enabled", "priority": 10, "version": 1, "is_system": True,
    },
    {
        "_id": "RCOMMON002", "name": "非常规会员密集下单", "scene_code": "common",
        "description": "会员等级不在 vip/gold 内，但近 24h 下单 8 单以上（`not_in` 示例）",
        "condition": {"logic": "and", "children": [
            {"field": "user_level", "op": "not_in", "value": ["vip", "gold"]},
            {"field": "order_cnt_24h", "op": "gte", "value": 8},
        ]},
        "score": 15, "status": "enabled", "priority": 20, "version": 1, "is_system": True,
    },
    {
        "_id": "RCOMMON003", "name": "普通会员高售后率", "scene_code": "common",
        "description": "会员等级为 normal，且近 24h 退款率 ≥30%（`in` 示例）",
        "condition": {"logic": "and", "children": [
            {"field": "user_level", "op": "in", "value": ["normal"]},
            {"field": "aftersale_rate_24h", "op": "gte", "value": 0.3},
        ]},
        "score": 25, "status": "enabled", "priority": 30, "version": 1, "is_system": True,
    },
]


# ============================================================
# 各集合的灌数函数
# ============================================================
async def seed_rule_scenes() -> int:
    """E06：按 `_id` upsert，重复执行结果一致。"""
    col = db.get_db()[COLL_RULE_SCENES]
    for row in SEED_SCENES:
        await col.replace_one({"_id": row["_id"]}, dict(row), upsert=True)
    return len(SEED_SCENES)


def validate_seed_rules() -> list[str]:
    """灌库前**先自检**每棵树的结构与特征键（失败即返回问题清单）。

    为什么要在种子里判一次：`rules` 是数据，写进库之后一棵非法的树不会让
    任何接口报错——决策链路上它只会被 BR-05-13 记成"这条规则求值失败"并跳过。
    于是表现是"种子灌了 15 条规则，页面上全都在，命中数却总是 0 或偏少"，
    排查要一路追到告警日志。在这里 `validate_tree()` 一次就能拦住。

    特征键的比对同样是必须的（任务书 §15）：引用不存在的 `field` 不会报错，
    只会让规则**永不命中**。
    """
    from app.engine.condition import validate_tree
    from app.engine.feature_compute import FEATURE_KEYS

    problems: list[str] = []
    known = set(FEATURE_KEYS)

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            problems.append(f"{path} 不是对象：{node!r}")
            return
        if "logic" in node:
            for index, child in enumerate(node.get("children") or []):
                walk(child, f"{path}.children[{index}]")
            return
        field = node.get("field")
        if field not in known:
            problems.append(f"{path}.field={field!r} 不是 E02 的 18 项特征键之一")

    for row in SEED_RULES:
        try:
            validate_tree(row["condition"])
        except Exception as e:  # noqa: BLE001 - 校验失败就是要报出来的问题
            problems.append(f"{row['_id']} 条件树非法：{e}")
            continue
        walk(row["condition"], row["_id"])
    return problems


async def seed_rules() -> int:
    """E05：按规则编码 upsert（幂等，见 `SEED_RULES` 的说明）。"""
    col = db.get_db()[COLL_RULES]
    for row in SEED_RULES:
        await col.replace_one({"_id": row["_id"]}, dict(row), upsert=True)
    return len(SEED_RULES)


async def seed_lists() -> tuple[int, int]:
    """E07：走服务层新增，因此唯一约束、脱敏、默认有效期都被真实执行。

    不直接 `insert_many`：那样会绕过 BR-06-21 的脱敏与 BR-06-22 的有效期规则，
    种子数据就会和页面新增出来的数据长得不一样。
    """
    service = ListService(ListRepo(db.get_db()))
    created = skipped = 0
    for row in SEED_LISTS:
        try:
            await service.create_entry(ListEntryCreate(**row), operator="seed")
            created += 1
        except Exception as e:  # noqa: BLE001 - 已存在属于正常的幂等跳过
            skipped += 1
            print(f"    跳过 {row['list_type']:<6}{row['entity_value']:<14}{type(e).__name__}")
    return created, skipped


# ============================================================
# 模块 13：E20 `model_configs` 的默认记录（BR-13-21）
# ------------------------------------------------------------
# `_id="default"`，内容就是 Spec §2.4 的四项默认（`rule` / `rule_first` /
# `1.0 / 0.0`）。**它不代表"模型引擎可用"**：AD-09 已裁定 `ModelEngine` 是空实现
# （恒返回 None，`final_score ≡ rule_score`），`engine_type=model/hybrid` 会被
# `SYS-4003` 明确拒绝。灌这一行的意义是让"当前用哪套引擎"在库里可读、可审计，
# 而不是让页面把它渲染成一个可以打开的开关。
# ============================================================
SEED_MODEL_CONFIG: dict[str, Any] = {
    "_id": "default",
    "engine_type": "rule",
    "model_name": None,
    "weights": {"rule": 1.0, "model": 0.0},
    "fuse_mode": "rule_first",
    "status": "enabled",
}


async def seed_model_config() -> int:
    """E20：按 `_id` upsert（幂等）。返回写入行数。"""
    col = db.get_db()[COLL_MODEL_CONFIGS]
    await col.replace_one({"_id": SEED_MODEL_CONFIG["_id"]}, dict(SEED_MODEL_CONFIG), upsert=True)
    return 1


async def seed_sys_users() -> int:
    """E19：bcrypt 哈希后 upsert（明文口令绝不入库）。"""
    col = db.get_db()[COLL_SYS_USERS]
    for row in SEED_USERS:
        doc = {
            "_id": row["username"],
            "username": row["username"],
            "password_hash": hash_password(row["password"]),
            "real_name": row["real_name"],
            "role": row["role"],
            "status": "active",
        }
        # last_login_at 不重置：重复灌种子不该把"最后登录时间"抹掉，
        # 否则演示时无法证明登录链路写过这一字段。
        await col.update_one(
            {"_id": row["username"]},
            {"$set": doc, "$setOnInsert": {"last_login_at": None}},
            upsert=True,
        )
    return len(SEED_USERS)


# ============================================================
# 模块 09：E10~E14 画像与关联图谱
# ------------------------------------------------------------
# ## 为什么画像种子必须"结构上自洽"
#
# `linked_user_cnt` 是冗余计数（BR-09-12），它与 E14 的边数必须**永远相等**——
# 验收 V-09-12 断言的正是"04 的 `device_user_cnt`（取自 09）与 09 的
# `linked_user_cnt` 数值一致"。若这里手写一个数字、那边手写一批边，
# 两个数字迟早对不上，而"设备关联了几个账号"是并案判断的核心依据。
#
# 因此下面的写法是：**先声明关联关系（哪些账号挂在哪个设备/IP/地址上），
# 再由同一份声明同时生成 E14 的边和 E10~E13 的冗余计数**。
# 数字只有一个来源，改一处两边一起变。
#
# ## IP 画像为什么必须预置（决策 D46 的硬性要求）
#
# `app/services/feature_service.py` 的 `DECISION_GATING_FEATURES = ("ip_is_proxy",)`，
# 而 04 从 E12 的 `is_proxy`/`is_idc` 取这个值。E12 **没有**该 IP 的行 ⇒
# `ip_is_proxy` 缺失 ⇒ 03 在调 05 之前短路降级（`stage=feature`），
# 05/07/08/10 全部无法演示。
#
# 模块 09 落地后，事件带的 IP 会被 `edge_writer` **自动补一行**（含显式
# `is_proxy=False` / `is_idc=False`），因此"首个新 IP 事件降级一次、之后不再降级"。
# 种子额外把**演示与 E2E 会用到**的 IP 全部预置好，这样演示一开始
# `stage` 就是 `rule`，不必先"喂"一条事件。
#
# ⚠️ **代理判定口径的如实声明**：本项目**没有外部 IP 库**，`region`/`isp` 是
# 演示用字面量，`is_proxy`/`is_idc` 的默认判定是"**非代理**"（只在下面**手工**
# 标注了一个演示用的代理 IP，用于展示 §2.1 的"`is_proxy=true` 标橙"与
# `proxy_ip` 标签）。**没有任何基于规则或名单的自动代理判定逻辑**——
# 凭空编一套（例如"某省某运营商=代理"）会产生大量误报，那是不可接受的。
# ============================================================
#: 团伙 A 共用的设备（E07 黑名单条目的理由就是"同设备 12 个新注册账号集中领券"）
CLUSTER_DEVICE = "D8F2A1C4"
#: 团伙 A 的主 IP —— **也是 E2E 脚本与灰度名单使用的那个 IP**
CLUSTER_IP_MAIN = "117.136.12.88"
#: 团伙 A 的第二批账号用的 IP（用于演示"同 IP 聚集"但又不是同一个 IP）
CLUSTER_IP_ALT = "203.0.113.77"
#: 团伙 A 共用的收货地址（E07 黑名单条目"同地址关联 8 个账号"）
CLUSTER_ADDRESS = "ADDR-7712"
#: 手工标注的演示代理 IP（**无外部 IP 库**，见上面的声明）
DEMO_PROXY_IP = "203.0.113.99"

#: 团伙 A 的 12 个账号：`(user_id, 脱敏手机号, 注册天数, 等级, 状态, 风险标签, stat)`
#: stat = `(order_cnt, aftersale_cnt, block_cnt, total_amount)`（金额单位分）
CLUSTER_USERS: list[tuple[str, str, int, str, str, list[str], tuple[int, int, int, int]]] = [
    # U000128 是 E2E/前端演示的默认用户：资料最饱满（有设备、IP、地址、多个标签、
    # stat、最近决策、风险分历史），图谱同时有 1 跳与 2 跳节点
    ("U000128", "139****0001", 42, "normal", "active",
     ["device_cluster", "blacklist_history", "address_cluster"], (3, 1, 1, 20000)),
    ("U001201", "138****0002", 30, "silver", "active",
     ["device_cluster", "ip_cluster"], (7, 0, 0, 88000)),
    ("U001202", "139****0001", 12, "normal", "active",          # 与 U000128 同号（掩码相同）
     ["device_cluster", "new_account"], (1, 0, 0, 9900)),
    ("U001203", "137****0004", 9, "normal", "active",
     ["device_cluster", "new_account"], (2, 0, 1, 15600)),
    ("U001204", "136****0005", 8, "normal", "frozen",
     ["device_cluster", "new_account", "aftersale_abuse"], (4, 3, 2, 42000)),
    ("U001205", "135****0006", 6, "normal", "active",
     ["device_cluster", "new_account"], (1, 0, 0, 5000)),
    ("U001206", "134****0007", 21, "gold", "active",
     ["device_cluster", "ip_cluster", "high_freq"], (11, 1, 0, 233000)),
    ("U001207", "133****0008", 18, "normal", "active",
     ["device_cluster", "ip_cluster"], (5, 0, 0, 61000)),
    ("U001208", "132****0009", 15, "normal", "active",
     ["ip_cluster"], (3, 0, 0, 24000)),
    ("U001209", "131****0010", 11, "silver", "active",
     ["ip_cluster", "new_account"], (2, 0, 0, 11800)),
    ("U001210", "130****0011", 7, "normal", "banned",
     ["ip_cluster", "blacklist_history"], (6, 2, 4, 77000)),
    ("U001211", "189****0012", 4, "normal", "active",
     ["ip_cluster", "new_account"], (1, 0, 0, 3300)),
]
#: 用主 IP 的那批账号（前 6 个）→ `ip_pool[CLUSTER_IP_MAIN].linked_user_cnt = 6`
#: 这里用切片而不是再抄一遍名单：抄一遍就有两个来源，迟早对不上
CLUSTER_MAIN_IP_USER_IDS: list[str] = [row[0] for row in CLUSTER_USERS[:6]]
#: 用备用 IP 的那批账号（后 6 个）
CLUSTER_ALT_IP_USER_IDS: list[str] = [row[0] for row in CLUSTER_USERS[6:]]
#: 共用收货地址的账号（8 个，对应 E07 黑名单条目的"同地址关联 8 个账号"）
CLUSTER_ADDRESS_USER_IDS: list[str] = [row[0] for row in CLUSTER_USERS[:8]]

#: 常态账号：`(user_id, 脱敏手机号, 注册天数, 等级, 状态, 风险标签, stat, 设备, IP, 地址)`
NORMAL_USERS: list[tuple[str, str, int, str, str, list[str],
                        tuple[int, int, int, int], str, str, str]] = [
    # U000129 出现在 E2E 的批量接入用例里（`{...common, user_id: 'U000129'}`）
    ("U000129", "137****8899", 5, "normal", "active", ["new_account"],
     (1, 0, 0, 9900), "DNORM0001", "203.0.113.5", "ADDR-9001"),
    ("U000130", "136****7788", 120, "silver", "active",
     ["aftersale_abuse", "high_freq"], (18, 9, 0, 356000),
     "DNORM0002", "203.0.113.6", "ADDR-9002"),
    # 大客户白名单账号（E07 的 white 条目）
    ("U009999", "139****0001", 400, "vip", "active", [],
     (52, 1, 0, 1580000), "DNORM0003", DEMO_PROXY_IP, "ADDR-9003"),
]

#: 模拟器会用到的 IP（`app/core/event_simulator.build_profiles` 的字面量池）：
#: 四模式各一组。预置它们以后，模拟器**第一条**事件就能解析出 `ip_is_proxy`，
#: 不会出现"演示刚开始就先降级一次"的观感问题。
#: 池来源与 `event_simulator` 的代码逐字对应（改了那边要同步改这里）。
SIMULATOR_IPS: list[str] = (
    ["203.0.113.10"]                                  # wool：同 IP
    + [f"198.51.100.{20 + i}" for i in range(3)]      # brush：同 sku
    + [f"192.0.2.{30 + i}" for i in range(6)]         # refund
    + [f"192.0.2.{100 + i}" for i in range(24)]       # normal
)

#: 团伙 B（"名单干净却会命中规则"的演示组，见下面那一节的完整说明）的三个环境字面量。
#: **必须先于 `IP_REGION_ISP` 定义**：那张归属表要引用 `SCORING_IP`。
SCORING_DEVICE = "DMULE0001"
SCORING_IP = "203.0.113.88"
SCORING_ADDRESS = "ADDR-9132"

#: 演示用的 IP 归属字面量（**无外部 IP 库**，仅为了界面上不显示「—」）
IP_REGION_ISP: dict[str, tuple[str, str]] = {
    CLUSTER_IP_MAIN: ("湖南省长沙市", "中国移动"),
    CLUSTER_IP_ALT: ("湖南省长沙市", "中国联通"),
    DEMO_PROXY_IP: ("境外", "未知机房"),
    SCORING_IP: ("广东省深圳市", "中国电信（机房出口）"),
}


# ============================================================
# 团伙 B：**名单干净、却会命中规则**的演示组（种子完备性缺口，对齐 D51）
# ------------------------------------------------------------
# ## 为什么必须有这一组（前端用真实数据实测发现的缺口，不是猜的）
#
# 原来的名单种子覆盖太广：`U000128` 本身在**黑名单**里，且与 `U000129` 同设备
# `D8F2A1C4`、同 IP `117.136.12.88`、同地址 `ADDR-7712`；`U009999` 在白名单。
# 于是**任何演示事件都会先撞名单直通**：按 BR-05-02/03，直通时 `hits=[]`、
# `rule_score=0`、**不求值任何规则**——页面上**永远看不到"规则累计计分 +
# 命中明细表"**。
#
# 这与模块 09 的"孤立账号分支不可达"（决策 D51）是**同一类缺陷**：
# 功能实现了、测试也过了，但**种子让它的分支永远走不到**，验收里那一格
# 实际上从未被真实数据验证过。
#
# ## 这一组的四个"刻意"
#
# 1. **不在任何名单里**（E07 一条都不给）：直通态与计分态都必须能被演示；
# 2. **不与 U000128 共用设备/IP/地址**（`D8F2A1C4` / `117.136.12.88` /
#    `ADDR-7712`）：否则会连带撞上那几条黑名单条目，又回到"看不到计分"；
# 3. **恰好命中 2 条规则、正好 70 分**（medium/review）：推导见下；
# 4. 画像/设备/IP/地址**给全**：09 的画像卡与关联图谱在这条演示路径上
#    也有真实数据可画（否则又是一个"分支走不到"）。
#
# ## 70 分是怎么来的（`U000132` 的一条 `login` 事件）
#
# | 规则 | 条件 | 该用户的实际特征 | 分值 |
# |---|---|---|---|
# | `RLOGIN001` 同设备聚集登录 | `device_user_cnt >= 5` | **6**（本组 6 个账号共用 `DMULE0001`） | 45 |
# | `RLOGIN003` 代理IP新账号登录 | `ip_is_proxy eq true` 且（`user_age_days <= 7` **或** `user_risk_tag_cnt >= 2`） | `is_proxy=true`、注册 3 天（`user_age_days=2`） | 25 |
# | `RCOMMON001` 多风险标签 | `user_risk_tag_cnt >= 2` | **1**（刻意只给一个标签，否则 90 分变 reject） | 不命中 |
# | `RLOGIN002` 高频登录 | `login_cnt_1h >= 10` | 窗口里 1 次 | 不命中 |
#
# → **70 分 / 命中 2 条 / `medium` / `review`**。
#
# ## 关键性质：这套特征**不依赖滑动窗口**
#
# 70 分所需的四项（E10 的注册时间与标签、E11 的关联账号数、E12 的代理标记）
# **全部是 Mongo 里的持久化画像**。因此前端既可以读种子预置的那条事件，
# 也可以自己 POST 一条新事件——**两者得到同一个 70 分**。
#
# 反例（刻意避开）：靠"先喂 10 条历史登录把 `login_cnt_1h` 顶到 10"也能凑出
# 70 分，但滑动窗口只活在进程内存里（悬空点 G-02），服务一重启就不可复现，
# 演示会退化成"种子里的分数没人能再算出来"。
# ============================================================
#: 团伙 B 共用的设备（6 个账号挂在同一台设备上 → `device_user_cnt = 6 > 阈值 5`）
#: （`SCORING_DEVICE` / `SCORING_IP` / `SCORING_ADDRESS` 定义在 `IP_REGION_ISP`
#:  之前——那张归属表要引用 `SCORING_IP`。）

#: 团伙 B 的 6 个账号，字段形状与 `NORMAL_USERS` 一致：
#: `(user_id, 脱敏手机号, 注册天数, 等级, 状态, 风险标签, stat, 设备, IP, 地址)`
SCORING_USERS: list[tuple[str, str, int, str, str, list[str],
                         tuple[int, int, int, int], str, str, str]] = [
    # **演示主路径的用户**：名单干净、6 账号共用设备、代理 IP、注册 3 天、
    # **恰好一个**风险标签（多一个就会命中 `RCOMMON001`，总分变 90 → reject）
    ("U000132", "135****0132", 3, "normal", "active", ["device_cluster"],
     (2, 0, 0, 19800), SCORING_DEVICE, SCORING_IP, SCORING_ADDRESS),
    # 同设备的另外 5 个账号：它们的作用是让 `device_user_cnt` 达到 6（阈值 5 之上留一格余量），
    # 同时让图谱上有"一台设备挂 6 个账号"的真实团伙形态可看
    ("U000133", "135****0133", 4, "normal", "active", ["device_cluster"],
     (1, 0, 0, 6600), SCORING_DEVICE, SCORING_IP, SCORING_ADDRESS),
    ("U000134", "135****0134", 6, "silver", "active", ["device_cluster"],
     (4, 1, 0, 32000), SCORING_DEVICE, SCORING_IP, SCORING_ADDRESS),
    ("U000135", "135****0135", 5, "normal", "active", ["device_cluster"],
     (1, 0, 0, 8800), SCORING_DEVICE, SCORING_IP, SCORING_ADDRESS),
    ("U000136", "135****0136", 8, "normal", "active", ["device_cluster"],
     (3, 0, 0, 21000), SCORING_DEVICE, SCORING_IP, SCORING_ADDRESS),
    ("U000137", "135****0137", 7, "normal", "active", ["device_cluster"],
     (2, 0, 0, 14500), SCORING_DEVICE, SCORING_IP, SCORING_ADDRESS),
]
#: 全部账号（建边用；用切片而不是再抄一遍名单，抄一遍就有两个来源）
SCORING_USER_IDS: list[str] = [row[0] for row in SCORING_USERS]
#: 演示主路径的用户 = 本组第一个
SCORING_DEMO_USER = SCORING_USERS[0][0]
#: 演示事件的**明文**手机号。
#:
#: 两个口径必须分开且对得上（这是 03/06 的既有约定）：
#: - **E01 的入参**要求 11 位明文，`validate_event` 在**入库前**脱敏（BR-03-04）；
#: - **E10 画像**存的是脱敏后的 `135****0132`（`SCORING_USERS` 里那个）。
#:
#: `mask_phone(SCORING_DEMO_PHONE_RAW) == SCORING_USERS[0][1]`，由
#: `seed_demo_decision` 在运行期断言一次——两处写的是同一个人，不能各写各的。
SCORING_DEMO_PHONE_RAW = "13500000132"

#: 预置演示事件的**固定编号**。
#:
#: 为什么是字面量而不是"运行当天的 `EVT{date}{seq}`"：前端要**硬编码引用**这条
#: 事件（如同它引用 `EVT20260925000000000001` 那样），编号必须跨天稳定。
#: 序列段以 `9` 开头，与运行时从 1 开始递增的编号天然错开（撞车需要一天内
#: 生成 9000 亿条事件）。同 `U000131` 一样，它是**固定的演示 ID**。
SCORING_DEMO_EVENT_ID = "EVT20260101900000000001"

#: **已处置**演示案件的来源事件（模块 08 的种子）。
#:
#: ## 为什么必须另起一条事件，而不是复用上面那条
#:
#: E08 的 `event_id` 上有唯一索引（BR-08-02 的幂等落点）：一个事件只能有一个
#: 案件。而工作台必须同时能看到「待审」与「已处置」两种状态（否则"已处置"
#: 那一格的分支永远走不到——与 D51 的"孤立账号"是同一类种子完备性问题）。
#: 因此这里再产出一条事件，并且**刻意选 `SCORING_USERS` 的第二个账号
#: （`U000133`）**而不是演示主路径的 `U000132`：
#:
#: - 它同样在团伙 B（同设备 `DMULE0001`、代理 IP、注册 4 天）→ 同样命中 2 条
#:   规则、同样 70 分 / `review`，因此不需要任何特殊构造；
#: - 它**不占用**演示主路径（D63：`U000132` + `EVT20260101900000000001`），
#:   `U000132` 的画像、图谱与"名单干净却计分"的那一屏完全不受影响。
#:
#: ## 处置用 `block_order`（不写名单）
#:
#: 演示案件的处置结论是 `violation` + `block_order`。**刻意不选"拉黑用户/封禁设备"**：
#: 那两条会写 `list_entries`，从而改动 E07 的 6 条种子条目（`U000133` 的脱敏手机号
#: 关联、`DMULE0001` 已在种子里作为"团伙 B 设备"存在）——演示数据是**只读的展示
#: 前提**，为造一条案件去改名单库，会连带影响 05 的名单直通演示与 09 的画像计数。
#: `block_order` 只调 `BizAdapter`（默认 `MockBizAdapter`），因此对既有数据零影响。
DISPOSED_DEMO_EVENT_ID = "EVT20260101900000000002"
#: 已处置演示案件的涉事用户（团伙 B 的第二个账号，见上）
DISPOSED_DEMO_USER = "U000133"
#: 已处置演示案件的**明文**手机号（与 `SCORING_USERS` 的 `U000133` 那条脱敏号对应：
#: `mask_phone(DISPOSED_DEMO_PHONE_RAW) == "135****0133"`，运行期断言一次）
DISPOSED_DEMO_PHONE_RAW = "13500000133"
#: 演示案件的处置人（E19 的种子账号之一）
SEED_REVIEWER = "reviewer01"
#: 演示处置的动作：**只拦截订单**（不写名单，理由见上方注释）
SEED_DISPOSE_ACTION = "block_order"
#: 演示处置的备注（PRD 2.3.3 要求必填；内容与规则命中原因对得上）
SEED_DISPOSE_REMARK = "同设备聚集登录 + 代理 IP 新账号，确认违规，拦截本次订单"


def _cluster_user_doc(row: tuple, now: int) -> dict:
    """把团伙行拼成 E10 文档（含 `risk_score_history` 与 `latest_decision`）。"""
    user_id, phone, age_days, level, status, tags, stat = row
    register_at = now - age_days * 86_400_000
    order_cnt, aftersale_cnt, block_cnt, total_amount = stat
    # 风险分历史：3 个固定比例的分值（**不用随机数**，BR-00-25 要求可复现）
    scores = [30 + (order_cnt * 3) % 25, 45 + (aftersale_cnt * 7) % 20, 55 + (block_cnt * 11) % 25]
    return {
        "_id": user_id,
        "phone": phone,
        "register_at": register_at,
        "level": level,
        "status": status,
        "risk_tags": list(tags),
        "risk_score_history": [
            {"score": float(score), "decided_at": now - (3 - i) * 86_400_000}
            for i, score in enumerate(scores)
        ],
        "stat": {
            "order_cnt": order_cnt, "aftersale_cnt": aftersale_cnt,
            "block_cnt": block_cnt, "total_amount": total_amount,
        },
        # E10 的 `latest_decision` 只保留最近一次（BR-09-06）
        "latest_decision": {
            "risk_score": float(scores[-1]),
            "risk_level": "high" if block_cnt or aftersale_cnt else "medium",
            "decision": "review",
            "decided_at": now - 2 * 3_600_000,
        },
    }


def _normal_user_doc(row: tuple, now: int) -> dict:
    """常态账号的 E10 文档（最近一次决策是放行，与团伙账号一眼可辨）。"""
    user_id, phone, age_days, level, status, tags, stat = row[:7]
    doc = _cluster_user_doc((user_id, phone, age_days, level, status, tags, stat), now)
    doc["latest_decision"]["risk_level"] = "low"
    doc["latest_decision"]["decision"] = "pass"
    return doc


#: **孤立账号**：有完整画像，但**没有任何 E14 边**。
#:
#: 存在理由（种子完备性，不是"为测试造数据"）：从未与任何设备/IP/地址产生过
#: 关联的新注册用户是**真实存在的业务情形**，而 §2.2 的两种空态必须能被区分：
#: - 孤立账号 → **200** + 只有中心节点、`edges=[]` →「该用户暂无关联实体（孤立账号）」
#: - 实体不存在 → **404 `GRP-4004`** →「未找到该用户/实体」
#: 若种子里每个用户都有边，`V-09-09` 的前半段（以及前端 §5.2 的对应 UI 分支）
#: 就**永远走不到**，"两种空态能区分"这件事也就从未被真正验证过。
#: 因此这里刻意**不给它造任何边**（见 `build_profile_seed` 的说明）。
ISOLATED_USERS: list[tuple[str, str, int, str, str, list[str],
                          tuple[int, int, int, int]]] = [
    ("U000131", "188****6621", 3, "normal", "active", [], (0, 0, 0, 0)),
]


def _edge_doc(from_type: str, from_id: str, to_type: str, to_id: str, relation: str,
              weight: int, now: int, *, risk_flag: bool = False) -> dict:
    """拼一条 E14 边（`first_seen_at` 用 7 天前，`last_seen_at` 用本次种子时刻）。"""
    return {
        "from_type": from_type, "from_id": from_id,
        "to_type": to_type, "to_id": to_id,
        "relation": relation, "weight": weight,
        "first_seen_at": now - 7 * 86_400_000,
        "last_seen_at": now,
        "risk_flag": risk_flag,
    }


def build_profile_seed(now: int) -> dict[str, list[dict]]:
    """由上面那份"关联关系声明"生成全部 E10~E14 文档（纯函数，便于单测/复核）。

    返回 `{集合名: [doc, ...]}`。**冗余计数与边都从这里出**，因此
    `linked_user_cnt` 与边数在结构上不可能不一致（V-09-12 的前提）。

    `ISOLATED_USERS` 只出现在 E10，**不产出边**：它承载 §2.2 的"孤立账号"空态
    （`V-09-09` 的前半段），如果顺手给它也建一条边，这条空态就再也验证不到了。
    """
    users = [_cluster_user_doc(row, now) for row in CLUSTER_USERS]
    users += [_normal_user_doc(row[:7], now) for row in NORMAL_USERS]
    # 团伙 B（名单干净、会命中规则）：与常态账号同一个文档构造器，只是多了
    # "6 个账号共用一台设备"这一层关系（由下面的建边表达）
    users += [_normal_user_doc(row[:7], now) for row in SCORING_USERS]
    # 孤立账号：只进 E10，**不产出任何边**（下面所有建边循环都用
    # CLUSTER_USERS / NORMAL_USERS 的名单，它不在其中，见 ISOLATED_USERS 的说明）
    users += [_normal_user_doc(row, now) for row in ISOLATED_USERS]

    devices: list[dict] = [{
        "_id": CLUSTER_DEVICE, "fingerprint": "fp-cluster-a",
        "first_seen_at": now - 30 * 86_400_000, "last_seen_at": now,
        "os": "Android 13", "ua": "Mozilla/5.0 (Linux; Android 13)",
        "linked_user_cnt": len(CLUSTER_USERS), "risk_level": "high",
    }]
    ips: list[dict] = [
        {"_id": CLUSTER_IP_MAIN, "region": IP_REGION_ISP[CLUSTER_IP_MAIN][0],
         "isp": IP_REGION_ISP[CLUSTER_IP_MAIN][1],
         "is_proxy": False, "is_idc": False,
         "linked_user_cnt": len(CLUSTER_MAIN_IP_USER_IDS),
         "first_seen_at": now - 30 * 86_400_000},
        {"_id": CLUSTER_IP_ALT, "region": IP_REGION_ISP[CLUSTER_IP_ALT][0],
         "isp": IP_REGION_ISP[CLUSTER_IP_ALT][1],
         "is_proxy": False, "is_idc": False,
         "linked_user_cnt": len(CLUSTER_ALT_IP_USER_IDS),
         "first_seen_at": now - 30 * 86_400_000},
        # 手工标注的演示代理 IP（见文件头的如实声明）
        {"_id": DEMO_PROXY_IP, "region": IP_REGION_ISP[DEMO_PROXY_IP][0],
         "isp": IP_REGION_ISP[DEMO_PROXY_IP][1],
         "is_proxy": True, "is_idc": True, "linked_user_cnt": 1,
         "first_seen_at": now - 20 * 86_400_000},
    ]
    # 模拟器 IP：0 个关联账号（还没有任何事件），布尔字段**显式在场**（D46）
    for ip in SIMULATOR_IPS:
        if ip in (CLUSTER_IP_MAIN, CLUSTER_IP_ALT, DEMO_PROXY_IP):
            continue
        ips.append({
            "_id": ip, "region": None, "isp": None,
            "is_proxy": False, "is_idc": False, "linked_user_cnt": 0,
            "first_seen_at": now,
        })

    addresses: list[dict] = [{
        "_id": CLUSTER_ADDRESS, "user_id": CLUSTER_ADDRESS_USER_IDS[0],
        "receiver": "张伟", "phone": "139****0001",
        "province": "湖南省", "city": "长沙市", "district": "岳麓区",
        # BR-09-05：只存哈希，**不存明文详细地址**
        "detail_hash": "9f2c4a1d7e5b8036a4c9f1e2d3b4a5c6",
        "linked_user_cnt": len(CLUSTER_ADDRESS_USER_IDS), "aftersale_cnt": 4,
        "first_seen_at": now - 25 * 86_400_000, "last_seen_at": now,
    }]

    for idx, row in enumerate(NORMAL_USERS):
        user_id, _phone, _age, _lv, _st, _tags, _stat, device, ip, address = row
        devices.append({
            "_id": device, "fingerprint": f"fp-normal-{idx}",
            "first_seen_at": now - 60 * 86_400_000, "last_seen_at": now,
            "os": "Windows 11" if idx % 2 == 0 else "iOS 17",
            "ua": "Mozilla/5.0", "linked_user_cnt": 1,
            "risk_level": "low" if idx != 1 else "medium",
        })
        if ip not in {row2["_id"] for row2 in ips}:
            ips.append({
                "_id": ip, "region": None, "isp": None,
                "is_proxy": ip == DEMO_PROXY_IP, "is_idc": ip == DEMO_PROXY_IP,
                "linked_user_cnt": 1, "first_seen_at": now - 60 * 86_400_000,
            })
        addresses.append({
            "_id": address, "user_id": user_id, "receiver": f"用户{idx}",
            "phone": _phone, "province": "浙江省", "city": "杭州市",
            "district": "西湖区",
            "detail_hash": f"seedhash{idx:04d}",
            "linked_user_cnt": 1, "aftersale_cnt": 1 if idx == 1 else 0,
            "first_seen_at": now - 50 * 86_400_000, "last_seen_at": now,
        })

    # ---------- 团伙 B：一台设备挂 6 个账号 + 一个共用代理出口 IP ----------
    # 设备/IP/地址的 `linked_user_cnt` 初值直接由声明推出（与团伙 A 同一口径），
    # 末尾的 `_reconcile_linked_counts` 还会按**边集合**再核一遍（BR-09-12）
    devices.append({
        "_id": SCORING_DEVICE, "fingerprint": "fp-scoring-b",
        "first_seen_at": now - 10 * 86_400_000, "last_seen_at": now,
        "os": "Android 12", "ua": "Mozilla/5.0 (Linux; Android 12; SM-A5360)",
        "linked_user_cnt": len(SCORING_USER_IDS),
        # 等级取 medium 而不是 high：它**不在名单里**，是"规则算出有风险"的演示对象，
        # 与团伙 A（已被拉黑 → high）刻意区分开
        "risk_level": "medium",
    })
    ips.append({
        "_id": SCORING_IP, "region": IP_REGION_ISP[SCORING_IP][0],
        "isp": IP_REGION_ISP[SCORING_IP][1],
        # 代理/机房出口（手工标注，见文件头声明）——`RLOGIN003` 的唯一开关
        "is_proxy": True, "is_idc": True,
        "linked_user_cnt": len(SCORING_USER_IDS),
        "first_seen_at": now - 10 * 86_400_000,
    })
    addresses.append({
        "_id": SCORING_ADDRESS, "user_id": SCORING_DEMO_USER,
        "receiver": "李强", "phone": "135****0132",
        "province": "广东省", "city": "深圳市", "district": "南山区",
        # BR-09-05：只存哈希，**不存明文详细地址**
        "detail_hash": "3b7e1c9a5d2f8064e1a7c3b9d5f20648",
        "linked_user_cnt": len(SCORING_USER_IDS), "aftersale_cnt": 0,
        "first_seen_at": now - 10 * 86_400_000, "last_seen_at": now,
    })

    edges: list[dict] = []
    # 团伙 B 的边：`used_device` / `shared_ip` / `shared_address` 各 6 条。
    # `device_user_cnt`（= 6）与 `ip_user_cnt`（= 6）就是从这些边算出来的
    # （`_reconcile_linked_counts` 按真源重算，BR-09-12/13）
    for user_id in SCORING_USER_IDS:
        edges.append(_edge_doc("user", user_id, "device", SCORING_DEVICE,
                               "used_device", 1, now,
                               risk_flag=user_id == SCORING_DEMO_USER))
        edges.append(_edge_doc("user", user_id, "ip", SCORING_IP, "shared_ip", 1, now))
        edges.append(_edge_doc("user", user_id, "address", SCORING_ADDRESS,
                               "shared_address", 1, now))

    for idx, user_id in enumerate(row[0] for row in CLUSTER_USERS):
        # `weight` 用固定的确定式（1~3）：不用随机数，重跑结果一致（BR-00-25）
        edges.append(_edge_doc("user", user_id, "device", CLUSTER_DEVICE,
                               "used_device", 1 + idx % 3, now,
                               risk_flag=user_id in ("U000128", "U001201")))
    for user_id in CLUSTER_MAIN_IP_USER_IDS:
        edges.append(_edge_doc("user", user_id, "ip", CLUSTER_IP_MAIN, "shared_ip",
                               2 if user_id == "U000128" else 1, now))
    for user_id in CLUSTER_ALT_IP_USER_IDS:
        edges.append(_edge_doc("user", user_id, "ip", CLUSTER_IP_ALT, "shared_ip", 1, now))
    for user_id in CLUSTER_ADDRESS_USER_IDS:
        edges.append(_edge_doc("user", user_id, "address", CLUSTER_ADDRESS,
                               "shared_address", 1, now))
    # `same_phone`：U000128 与 U001202 的脱敏手机号相同（`139****0001`）。
    # 方向固定按编号字典序（`user|user` 的边只留一条，见 `edge_writer` 的说明）
    edges.append(_edge_doc("user", "U000128", "user", "U001202", "same_phone", 1, now))
    # `transferred_to`：案件转交（E14 的第五种关系，用于图例演示）
    edges.append(_edge_doc("user", "U001210", "user", "U000128", "transferred_to", 1, now,
                           risk_flag=True))
    for idx, row in enumerate(NORMAL_USERS):
        user_id, _p, _a, _l, _s, _t, _stat, device, ip, address = row
        edges.append(_edge_doc("user", user_id, "device", device, "used_device", 1, now))
        edges.append(_edge_doc("user", user_id, "ip", ip, "shared_ip", 1, now))
        edges.append(_edge_doc("user", user_id, "address", address, "shared_address", 1, now))

    return {
        COLL_USERS: users,
        COLL_DEVICES: devices,
        COLL_IP_POOL: ips,
        COLL_USER_ADDRESSES: addresses,
        COLL_ENTITY_EDGES: edges,
    }


async def seed_demo_decision() -> dict[str, Any]:
    """预置一条**由真实 04 + 真实 05 产出**的演示事件（E01~E04）。

    ## 为什么不是"手写四条文档"

    手写文档也能让页面显示出来，但它**证明不了任何事**：05 的字段形状或分值口径
    一变，手写的行仍然"看起来对"，而前端会拿着一条**引擎永远不会产出**的记录去
    验证页面。这里改成**驱动真实链路**：

        validate_event（真实校验与脱敏）
          → FeatureService.compute（真实 04：读 E10~E13 与 09 的关联账号数）
          → decision.decide（真实 05：真实规则集、真实累加与仲裁，dry_run=False）
          → 04/05 各自的仓储写 E02/E03/E04
          → 只把 E01 的文档补齐后 upsert（种子不是接入网关，E01 没有别的写手）

    因此这条事件的每一个数字都能被引擎重算——**前端自己 POST 一条新事件，
    会得到同一个 70 分**（推导见 `SCORING_USERS` 的说明）。

    ## 幂等（BR-00-25）

    先按 `event_id` 删掉这三条集合里**属于本事件**的旧行，再重新产出：

    - 不删就会在重复执行时叠加（`decisions` 每次一条新决策、`decision_hits`
      成批追加），而 `feature_snapshots` 有 `uq_event` 唯一索引，会在第二次
      直接撞键并刷 `FEA-5002` 告警；
    - 只删 `event_id == SCORING_DEMO_EVENT_ID` 的行，**不碰任何其它事件**
      （`--reset` 之外的种子脚本从不删别人写的数据）。

    ⚠️ 编号（`SNP...` / `DEC...`）走正常的每日序列，因此**重复执行会换号**，
    但行数与内容收敛；`--reset` 之后两次执行则完全一致（计数器被清零）。
    这与 E07 名单条目用随机 `_id` 是同一个口径：追求"不重复、可收敛"，
    而不是字节级复现。

    ## 返回值

    `{event_id, decision, rule_score, hit_rule_count, degraded}`，由 `main()`
    断言并打印——**这条种子本身就是一个种子完备性门禁**（同 `validate_seed_rules`）：
    若新规则/画像调整导致这条事件不再落在 `review` + ≥2 条命中，
    种子脚本会**失败退出**，而不是静默产出一条"看起来对但演示不了计分"的数据。
    """
    from app.engine.decision import decide
    from app.repos.event_repo import expire_at_for
    from app.schemas.event_schema import DECISION_FIELDS, SOURCE_MANUAL_SIM
    from app.services import feature_service
    from app.services.event_service import normalize_decision, validate_event

    now = now_ms()
    # 明文用于 E01 入参，脱敏值用于 E10；两者必须是同一个号（否则名单的
    # `phone` 维度与画像显示会对不上同一个用户）
    if mask_phone(SCORING_DEMO_PHONE_RAW) != SCORING_USERS[0][1]:
        raise RuntimeError(
            "SCORING_DEMO_PHONE_RAW 与 SCORING_USERS 的脱敏手机号不一致："
            f"{mask_phone(SCORING_DEMO_PHONE_RAW)} != {SCORING_USERS[0][1]}"
        )
    payload: dict[str, Any] = {
        "event_type": "login",
        "user_id": SCORING_DEMO_USER,
        "device_id": SCORING_DEVICE,
        "ip": SCORING_IP,
        "phone": SCORING_DEMO_PHONE_RAW,
        "scene_extra": {
            "login_type": "pwd",
            "ua": "Mozilla/5.0 (Linux; Android 12; SM-A5360)",
            "success": True,
        },
        # `ts` 比接收时间早一分钟：既不是"未来时间"，也不触发 BR-03-08 的迟到判定
        "ts": now - 60_000,
        # 来源标成 `manual_sim` 而不是 `mock_biz`：这条**不是模拟器产出的**，
        # 页面/审计上必须能区分"种子预置"与"事件流跑出来的"
        "source": SOURCE_MANUAL_SIM,
    }
    event = validate_event(payload, now)          # 真实校验 + 手机号脱敏
    event["_id"] = SCORING_DEMO_EVENT_ID

    # ---- 幂等：清掉本事件在上一次执行里留下的 E02/E03/E04 ----
    for coll in (COLL_FEATURE_SNAPSHOTS, COLL_DECISIONS, COLL_DECISION_HITS):
        await db.get_db()[coll].delete_many({"event_id": SCORING_DEMO_EVENT_ID})

    # ---- 真实 04：算快照（真实读 E10~E13 + 09 的关联账号数） ----
    service = feature_service.get_feature_service()
    snapshot = await service.compute(event)
    await service.flush()

    # ---- 真实 05：名单 → 条件树 → 累加 → 仲裁 → 落 E03/E04 ----
    from app.engine.decision import flush as flush_decisions

    outcome = await decide(event, snapshot.get("features") or {},
                           snapshot_id=snapshot.get("snapshot_id"))
    await flush_decisions()

    # ---- E01：把详情页需要的四段之一补齐（E02/E03/E04 已由 04/05 写好） ----
    event["feature_snapshot"] = snapshot
    event["decision"] = normalize_decision(outcome.block, snapshot.get("snapshot_id"))
    event["decision"] = {
        field: event["decision"].get(field) for field in DECISION_FIELDS
    }
    event["expire_at"] = expire_at_for(event["received_at"])
    await db.get_db()[COLL_RISK_EVENTS].replace_one(
        {"_id": SCORING_DEMO_EVENT_ID}, dict(event), upsert=True
    )

    return {
        "event_id": SCORING_DEMO_EVENT_ID,
        "decision_id": outcome.decision_id,
        "decision": outcome.block.get("decision"),
        "rule_score": outcome.block.get("rule_score"),
        "hit_rule_count": outcome.block.get("hit_rule_count"),
        "degraded": bool(outcome.degraded),
        "hits": [h.get("rule_code") for h in (outcome.block.get("hits") or [])],
        "snapshot_id": snapshot.get("snapshot_id"),
    }


async def cleanup_demo_cases() -> int:
    """删掉上一次种子产出的两条演示案件与其流水，返回删除的案件数。

    **必须在 `seed_demo_decision()` 之前调用**（见 `seed_demo_cases` 的说明）：
    演示事件一落库，建案钩子就会建出那条待审案件；若等两条事件都跑完再清，
    就会把刚建好的案件删掉并换号重建。

    **不删审计**：审计是 append-only（BR-12-11），重跑种子留下的 `case.create`
    行是"这次种子确实建过案"的真实记录，由 `--reset` 统一归零。
    因此重跑种子后，库里会存在指向已删案件编号的审计行——这是**如实留痕**，
    而不是需要被擦掉的脏数据（擦掉它才是篡改审计）。
    """
    db_ = db.get_db()
    removed = 0
    for event_id in (SCORING_DEMO_EVENT_ID, DISPOSED_DEMO_EVENT_ID):
        stale = await db_[COLL_RISK_CASES].find({"event_id": event_id},
                                                {"_id": 1}).to_list(length=10)
        case_ids = [str(r["_id"]) for r in stale]
        if not case_ids:
            continue
        await db_[COLL_CASE_ACTIONS].delete_many({"case_no": {"$in": case_ids}})
        result = await db_[COLL_RISK_CASES].delete_many({"_id": {"$in": case_ids}})
        removed += int(result.deleted_count)
    return removed


async def seed_demo_cases(pending_event_id: str, pending_decision_id: str) -> dict[str, Any]:
    """E08/E09 的演示案件：**一条待审 + 一条已处置**，全部由真实链路产出。

    ## 为什么必须两种状态都有

    工作台（07）要按 `status` 筛选，处置区在 `pending` / `reviewing` / `disposed`
    三态下的行为完全不同（禁用 / 可编辑 / 只读回显）。若种子只有一种状态，
    另外两种分支在真实数据上**永远走不到**——这与决策 D51 的"孤立账号分支
    不可达"是同一类种子完备性问题，而它的暴露方式也一样：测试全绿、页面上
    少一屏。

    ## 两条案件分别怎么来的

    1. **待审（`pending`）**：演示事件（`SCORING_DEMO_EVENT_ID`，真实 05 判
       `review` / 70 分）落库后由**建案钩子**建出（决策 D5）。这里再显式调
       一次 `create_from_decision`：钩子是异步旁路，脚本不能假设它一定跑完，
       而 `create_from_decision` 是幂等的（BR-08-02），调第二次只会返回既有案件。
    2. **已处置（`disposed`）**：另一条真实事件（`DISPOSED_DEMO_EVENT_ID`）→
       真实 04 + 真实 05 → 真实**认领** → 真实**处置**（`block_order`，不写名单）。
       于是它自带一条 `case_actions`、一条 `case.dispose` 审计与一个已被
       `MockBizAdapter`"同步"过的 `biz_sync_result`（页面要显示「模拟同步」）。

    ## 幂等

    **清理动作不在这里**：上一次留下的演示案件由 `cleanup_demo_cases()`
    在 `seed_demo_decision()` **之前**删除。顺序不能反过来——
    演示事件落库时建案钩子已经建了一条案件，若在这一步才清，就会把刚建好的
    那条删掉、再用一个新编号重建（白白消耗一个案件编号，审计链上还会留下
    一条指向已删案件的 `case.create`）。与 `seed_demo_decision` 同一口径：
    重复执行后**行数与内容收敛**，`--reset` 之后两次执行才完全一致。
    """
    from app.engine.decision import decide
    from app.engine.decision import flush as flush_decisions
    from app.repos.event_repo import expire_at_for
    from app.schemas.case_schema import DisposeIn, DisposePreviewIn
    from app.schemas.event_schema import DECISION_FIELDS, SOURCE_MANUAL_SIM
    from app.services import feature_service
    from app.services.case_service import get_case_service
    from app.services.disposal_service import get_disposal_service
    from app.services.event_service import normalize_decision, validate_event

    db_ = db.get_db()

    # 明文手机号与 E10 画像的脱敏值必须是同一个号（否则名单的 phone 维度与
    # 画像显示会对不上同一个人——与 `seed_demo_decision` 的同一条断言同因）
    if mask_phone(DISPOSED_DEMO_PHONE_RAW) != "135****0133":
        raise RuntimeError(
            f"DISPOSED_DEMO_PHONE_RAW 与 SCORING_USERS 的 U000133 脱敏号不一致："
            f"{mask_phone(DISPOSED_DEMO_PHONE_RAW)}"
        )

    case_service = get_case_service()

    # ---- ① 待审案件（由演示决策建出，钩子已在 05 落库时跑过一次） ----
    pending_no = await case_service.create_from_decision(pending_decision_id)

    # ---- ② 已处置案件：真实 04 → 真实 05 → 真实认领 → 真实处置 ----
    now = now_ms()
    payload: dict[str, Any] = {
        "event_type": "login",
        "user_id": DISPOSED_DEMO_USER,
        # 与 SCORING_USERS 里 U000133 的画像同设备/同 IP/同地址：
        # 它因此命中与演示用户相同的两条规则（70 分 / review），无需特殊构造
        "device_id": SCORING_DEVICE,
        "ip": SCORING_IP,
        "phone": DISPOSED_DEMO_PHONE_RAW,
        "scene_extra": {
            "login_type": "pwd",
            "ua": "Mozilla/5.0 (Linux; Android 12; SM-A5360)",
            "success": True,
        },
        "ts": now - 60_000,
        "source": SOURCE_MANUAL_SIM,
    }
    event = validate_event(payload, now)
    event["_id"] = DISPOSED_DEMO_EVENT_ID

    for coll in (COLL_FEATURE_SNAPSHOTS, COLL_DECISIONS, COLL_DECISION_HITS):
        await db_[coll].delete_many({"event_id": DISPOSED_DEMO_EVENT_ID})

    feature_svc = feature_service.get_feature_service()
    snapshot = await feature_svc.compute(event)
    await feature_svc.flush()
    outcome = await decide(event, snapshot.get("features") or {},
                           snapshot_id=snapshot.get("snapshot_id"))
    await flush_decisions()

    event["feature_snapshot"] = snapshot
    decided = normalize_decision(outcome.block, snapshot.get("snapshot_id"))
    event["decision"] = {field: decided.get(field) for field in DECISION_FIELDS}
    event["expire_at"] = expire_at_for(event["received_at"])
    await db_[COLL_RISK_EVENTS].replace_one({"_id": DISPOSED_DEMO_EVENT_ID},
                                            dict(event), upsert=True)

    disposed_case = await db_[COLL_RISK_CASES].find_one({"event_id": DISPOSED_DEMO_EVENT_ID})
    result: dict[str, Any] = {
        "pending_case_no": pending_no,
        "pending_decision": "review",
        "disposed_case_no": None,
        "disposed_decision": str(outcome.block.get("decision") or ""),
        "disposed_score": int(outcome.block.get("final_score") or 0),
        "disposed_actions": 0,
        "error": None,
    }
    if pending_no is None:
        result["error"] = "演示决策没有建出案件（建案钩子与补建案都失败了）"
        return result
    if disposed_case is None:
        result["error"] = "第二条演示事件没有建出案件"
        return result

    disposed_no = str(disposed_case["_id"])
    result["disposed_case_no"] = disposed_no
    # 认领（真实路径：原子条件更新 + 一条 case.claim 审计）
    await case_service.claim(disposed_no, SEED_REVIEWER, actor_role="reviewer")
    # 处置（真实路径：令牌 → 锁定 → 名单(无) → BizAdapter → 流水 → 状态 → 审计 → 事件）
    disposal = get_disposal_service()
    preview = await disposal.preview(
        disposed_no,
        DisposePreviewIn(conclusion="violation", action_types=[SEED_DISPOSE_ACTION]),
        SEED_REVIEWER,
    )
    body = await disposal.dispose(
        disposed_no,
        DisposeIn(conclusion="violation", action_types=[SEED_DISPOSE_ACTION],
                  remark=SEED_DISPOSE_REMARK,
                  confirm_token=preview["confirm_token"]),
        SEED_REVIEWER, actor_role="reviewer",
    )
    result["disposed_actions"] = len(body.get("action_ids") or [])
    result["disposed_status"] = body.get("status")
    result["degraded"] = bool(body.get("degraded"))
    if result["disposed_actions"] == 0:
        result["error"] = "处置没有产出 case_actions 流水"
    return result


async def _validate_seed_cases(result: dict[str, Any]) -> list[str]:
    """E08/E09 案件种子的**完备性自检**（照 `validate_seed_rules` 的做法）。

    查的是"演示所需的分支都有真实数据可走"，而不是"代码有没有报错"：

    - 待审案件必须真的存在、`status=pending`、且关联演示事件
      （否则工作台默认筛选 `status=pending` 是空的——而默认屏正是被走查最多的画面，D65）；
    - 已处置案件必须 `status=disposed` 且**至少一条** `case_actions`
      （"已处置却没有流水"会让 07 的处置流水区永远空白，而那是处置留痕的展示位）；
    - 处置的动作必须是 `block_order`：这条同时是"**名单库没被种子改动**"的守卫
      ——若有人把演示处置改成 `blacklist_user` / `ban_device`，E07 的 6 条种子
      条目就会被改写，而 05 的名单直通演示与 09 的画像计数都依赖它们
      （见 `DISPOSED_DEMO_EVENT_ID` 的注释）。
    """
    problems: list[str] = []
    if result.get("error"):
        problems.append(str(result["error"]))
    db_ = db.get_db()

    pending_no = result.get("pending_case_no")
    if not pending_no:
        problems.append("待审案件未产出")
    else:
        case = await db_[COLL_RISK_CASES].find_one({"_id": pending_no})
        if case is None:
            problems.append(f"待审案件 {pending_no} 不在库里")
        else:
            if case.get("status") != "pending":
                problems.append(f"待审案件状态不是 pending：{case.get('status')}")
            if case.get("event_id") != SCORING_DEMO_EVENT_ID:
                problems.append(f"待审案件未关联演示事件：{case.get('event_id')}")
            if case.get("decision") != "review":
                problems.append(f"待审案件的决策不是 review：{case.get('decision')}")

    disposed_no = result.get("disposed_case_no")
    if not disposed_no:
        problems.append("已处置案件未产出")
    else:
        case = await db_[COLL_RISK_CASES].find_one({"_id": disposed_no})
        if case is None:
            problems.append(f"已处置案件 {disposed_no} 不在库里")
        else:
            if case.get("status") != "disposed":
                problems.append(f"已处置案件状态不是 disposed：{case.get('status')}")
            if not case.get("disposed_at"):
                problems.append("已处置案件没有 disposed_at")
        actions = await db_[COLL_CASE_ACTIONS].find({"case_no": disposed_no}).to_list(length=10)
        if not actions:
            problems.append("已处置案件没有 case_actions 流水")
        else:
            if {a.get("action_type") for a in actions} != {SEED_DISPOSE_ACTION}:
                problems.append(
                    "演示处置的动作不是"
                    f" {SEED_DISPOSE_ACTION}（会改动名单库种子）："
                    f"{[a.get('action_type') for a in actions]}"
                )
            if {a.get("conclusion") for a in actions} != {"violation"}:
                problems.append("演示处置的结论不是 violation")
            if not all(a.get("operator") == SEED_REVIEWER for a in actions):
                problems.append(f"演示处置的处置人不是 {SEED_REVIEWER}")
        # 名单库必须**一条都没被这条处置写入**（BR-08-20 的 block_order 不写名单）
        touched = await db_[COLL_LIST_ENTRIES].count_documents(
            {"related_case_no": disposed_no}
        )
        if touched:
            problems.append(f"演示处置改动了名单库（related_case_no={disposed_no} 有 {touched} 条）")
    return problems


async def _reconcile_linked_counts() -> int:
    """把 E11/E12/E13 的 `linked_user_cnt` 按**边集合的真实去重账号数**重算一遍。

    ## 为什么种子必须做这一步

    `seed_profiles()` 是**覆盖式 upsert**：它把画像行恢复到种子真值，
    但**不会删除运行期由事件新建的边**（种子不删数据，`--reset` 才删）。
    于是"重跑种子但不 `--reset`"会留下"某设备 `linked_user_cnt=12`、实际 13 条边"
    这类不一致——而这正是 BR-09-12 要求必须相等的那个数字
    （V-09-12：04 的 `device_user_cnt` 与 09 的 `linked_user_cnt` 数值一致）。

    重算把不一致消掉，方向是**以边为准**：`entity_edges` 是关联事实的存储，
    `linked_user_cnt` 只是为省一次全表统计而存在的冗余（BR-09-12）。
    "以计数为准去改边"是本末倒置——那会凭空造出或删掉真实的关联记录。

    返回被修正的行数（0 表示种子与边完全一致，这是正常情况）。
    """
    edges_col = db.get_db()[COLL_ENTITY_EDGES]
    cursor = await edges_col.aggregate([
        {"$project": {"_ends": [
            {"type": "$from_type", "id": "$from_id",
             "other_type": "$to_type", "other_id": "$to_id"},
            {"type": "$to_type", "id": "$to_id",
             "other_type": "$from_type", "other_id": "$from_id"},
        ]}},
        {"$unwind": "$_ends"},
        # 只数"另一端是账号"的边：`linked_user_cnt` 数的是**账号**，
        # 不能把 user↔user 的转交/同号边也算进来
        {"$match": {"_ends.other_type": "user"}},
        {"$group": {"_id": {"type": "$_ends.type", "id": "$_ends.id"},
                    "users": {"$addToSet": "$_ends.other_id"}}},
    ])
    truth: dict[str, dict[str, int]] = {}
    for row in await cursor.to_list(length=10_000):
        key = row["_id"]
        truth.setdefault(str(key["type"]), {})[str(key["id"])] = len(row["users"])

    fixed = 0
    for entity_type, coll_name in (("device", COLL_DEVICES), ("ip", COLL_IP_POOL),
                                   ("address", COLL_USER_ADDRESSES)):
        col = db.get_db()[coll_name]
        expected = truth.get(entity_type, {})
        rows = await col.find({}, {"linked_user_cnt": 1}).to_list(length=10_000)
        for row in rows:
            want = int(expected.get(str(row["_id"]), 0))
            if int(row.get("linked_user_cnt") or 0) != want:
                await col.update_one({"_id": row["_id"]},
                                     {"$set": {"linked_user_cnt": want}})
                fixed += 1
    return fixed


async def seed_profiles() -> dict[str, int]:
    """E10~E14：由 `build_profile_seed` 生成并 upsert（幂等，重跑结果一致）。

    - E10~E13 按 `_id` 做 `replace_one(upsert=True)`：**覆盖式**写入，
      因此重复执行会把画像恢复到种子真值（包括把运行期被事件加过的
      `linked_user_cnt` 复位）——这正是"种子"该有的语义。
    - E14 按唯一键 `from_id + to_id + relation` 覆盖式 upsert：同理。
    """
    now = now_ms()
    payload = build_profile_seed(now)
    counts: dict[str, int] = {}
    for coll_name, docs in payload.items():
        col = db.get_db()[coll_name]
        for doc in docs:
            if coll_name == COLL_ENTITY_EDGES:
                await col.replace_one(
                    {"from_id": doc["from_id"], "to_id": doc["to_id"],
                     "relation": doc["relation"]},
                    dict(doc), upsert=True,
                )
            else:
                await col.replace_one({"_id": doc["_id"]}, dict(doc), upsert=True)
        counts[coll_name] = len(docs)
    # 覆盖式 upsert 会重置计数，但**不会**删掉运行期新建的边；
    # 因此最后按边集合把冗余计数重算一遍，保证种子落地后两者一致（BR-09-12）
    counts["linked_user_cnt_reconciled"] = await _reconcile_linked_counts()
    return counts


# ============================================================
# 模块 10：E17 `sim_cases` 的 4 条内置演示用例
# ------------------------------------------------------------
# ## 为什么必须由种子灌入（不是"前端可以自己造"）
#
# Spec §2.1 明确写「**默认内置用例（4 条，由种子脚本灌入 `sim_cases`）**」，
# 并把它们列成一张表。没有它们，仿真页左栏是空的——而空态与缺陷在演示现场
# 分不清（与 08 的案件演示数据同一理由）。E2E 的门禁直接断言"用例数 ≥4 且
# 每条都有名称与预期决策"。
#
# ## 四个 event_type 各一条（覆盖 4 类判定路径）
#
# | 用例 | event_type | 预期 | 它证明什么 |
# |---|---|---|---|
# | 羊毛党批量领券 | `coupon_receive` | reject | 名单直通（`U000128` 在黑名单） |
# | 恶意退款欺诈 | `after_sale_apply` | review | 售后类规则的计分路径 |
# | 正常下单支付 | `order_pay` | pass | **误伤统计的分母**（预期 pass） |
# | 代理 IP 聚集登录 | `login` | review | 边界样本：名单干净但规则计分 |
#
# ## 事件体为什么必须是**完整**的（BR-10-17）
#
# 「用例模板必须包含完整事件体，载入即填充整个表单，不允许只填部分字段」。
# 因此下面每条都把 `event_type`、`user_id` 与该类型的**全部必填字段**写全，
# 并带上 `scene_extra` 的必填键——`seed_sim_cases` 会用 03 的
# `validate_event` 逐条验一遍，产不出就**失败退出**。
#
# ## 为什么复用既有的种子画像常量
#
# `U000128` / `D8F2A1C4` / `117.136.12.88` / `ADDR-7712` 都是上面已经定义的
# 团伙 A 常量，`U000132` / `SCORING_DEVICE` / `SCORING_IP` 是团伙 B 的常量。
# 直接用它们而不是另抄一份字面量：抄一份就有两个来源，画像改了事件体不会跟着改，
# 于是"演示用例跑出来不是预期的档位"这种缺陷只会在演示当天暴露。
# ============================================================
def sim_case_rows() -> list[dict[str, Any]]:
    """E17 的 4 条内置用例（返回新 dict，调用方可以安全改动）。

    函数而不是模块级常量：它引用了**定义在文件后半段**的团伙 A/B 常量
    （`SCORING_DEMO_USER` 等），模块级常量会在定义顺序上被"用到还没定义的名字"
    绊倒。函数体的求值发生在调用时，顺序问题自然消失。
    """
    main_phone = "13900000001"          # 与 `CLUSTER_USERS` 的 U000128 脱敏号对应
    return [
        {
            "name": "羊毛党批量领券",
            "category": "coupon_abuse",
            "expected_decision": "reject",
            "description": "团伙 A 的设备/地址/用户均在黑名单，用于验证名单直通",
            "event_template": {
                "event_type": "coupon_receive",
                "user_id": "U000128",                 # E07 的黑名单用户
                "device_id": CLUSTER_DEVICE,          # E07 的黑名单设备
                "ip": CLUSTER_IP_MAIN,
                "address_id": CLUSTER_ADDRESS,        # E07 的黑名单地址
                "phone": main_phone,
                "amount": 5000,
                # `coupon_receive` 的必填键是 `coupon_id` + `activity_id`
                "scene_extra": {
                    "coupon_id": "CP-2026-0001",
                    "activity_id": "ACT-2026-01",
                    "face_value": 5000,
                    "batch_id": "B-2026-01",
                },
            },
        },
        {
            "name": "恶意退款欺诈",
            "category": "aftersale_abuse",
            "expected_decision": "review",
            "description": "同地址历史售后聚集，用于验证售后滥用类规则",
            "event_template": {
                "event_type": "after_sale_apply",
                "user_id": "U000128",
                "biz_no": "AS-2026-000001",
                "address_id": CLUSTER_ADDRESS,
                "amount": 29900,
                # `after_sale_apply` 的必填键：after_sale_no / order_no / reason_code
                "scene_extra": {
                    "after_sale_no": "AS-2026-000001",
                    "order_no": "SO-2026-000001",
                    "reason_code": "not_received",
                    "refund_amount": 29900,
                    "received_goods": False,
                },
            },
        },
        {
            "name": "正常下单支付",
            "category": "normal",
            "expected_decision": "pass",
            "description": "干净账号的正常支付，批量回放时作为误伤统计的分母",
            "event_template": {
                "event_type": "order_pay",
                "user_id": "U000130",                 # E10 的常态账号（无名单、无标签）
                "amount": 19900,
                # `order_pay` 的必填键：order_no / pay_channel
                "scene_extra": {
                    "order_no": "SO-2026-000002",
                    "pay_channel": "alipay",
                    "pay_amount": 19900,
                    "card_tail": "8899",
                },
            },
        },
        {
            "name": "代理 IP 聚集登录",
            "category": "boundary",
            "expected_decision": "review",
            "description": "边界样本：名单干净，但代理 IP + 同设备多账号（团伙 B）",
            "event_template": {
                "event_type": "login",
                "user_id": SCORING_DEMO_USER,         # U000132：名单干净却命中 2 条规则
                "device_id": SCORING_DEVICE,
                "ip": SCORING_IP,
                "phone": SCORING_DEMO_PHONE_RAW,      # 明文：`validate_event` 会脱敏
                "scene_extra": {
                    "login_type": "pwd",
                    "ua": "Mozilla/5.0 (Linux; Android 12; SM-A5360)",
                    "success": True,
                },
            },
        },
    ]


def validate_sim_cases() -> list[str]:
    """E17 种子的**完备性自检**（照 `validate_seed_rules` / `_validate_seed_cases`）。

    查的是"演示所需的分支都有真实数据可走"，而不是"代码有没有报错"：

    - **至少 4 条**（Spec §2.1 的内置条数，也是 E2E 的门禁）；
    - 每条都有**非空名称**与**合法预期决策**（`pass`/`review`/`reject`）——
      没有预期决策的用例无法参与"是否符合预期"与误伤统计；
    - 每条的事件体都经 **03 的真实校验器**验一遍。这是最要紧的一条：
      一个字段缺失的模板会在仿真页点开后才报 `EVT-4004`，
      而那时策略师已经在演示了。**校验失败即产不出，种子脚本失败退出。**
    - 四条用例的 `event_type` 不重复（覆盖 4 条不同的判定路径）。

    为什么用 `validate_event` 而不是自己数字段：那会是**第二套校验**，
    而它与 03 的漂移表现恰好是"种子说合法、真实入口 422"——正是本模块
    一致性要求（任务书 §3）要防的事。
    """
    from app.schemas.sim_schema import CASE_CATEGORIES, EXPECTED_DECISIONS
    from app.services.event_service import validate_event

    problems: list[str] = []
    rows = sim_case_rows()
    if len(rows) < 4:
        problems.append(f"内置用例不足 4 条（Spec §2.1），实际 {len(rows)} 条")
    seen_types: list[str] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not (2 <= len(name) <= 64):
            problems.append(f"用例名长度不合法（{name!r}）")
        if row.get("expected_decision") not in EXPECTED_DECISIONS:
            problems.append(f"{name}：预期决策非法 {row.get('expected_decision')!r}")
        if row.get("category") not in CASE_CATEGORIES:
            problems.append(f"{name}：分类非法 {row.get('category')!r}")
        template = row.get("event_template") or {}
        event_type = str(template.get("event_type") or "")
        if event_type in seen_types:
            problems.append(f"{name}：event_type={event_type} 与前面的用例重复")
        seen_types.append(event_type)
        try:
            validate_event(dict(template), now_ms())
        except Exception as e:  # noqa: BLE001 - 校验失败就是要报出来的问题
            code = getattr(e, "code", type(e).__name__)
            problems.append(f"{name}：事件体未通过 03 的校验（{code}：{e}）")
    return problems


async def seed_sim_cases() -> int:
    """E17：按 `_id` **覆盖式 upsert**（幂等，重跑结果一致）。

    编号用固定的 `SIMC{日期}0000NN` 而不是走 `seq_counters`：
    内置用例是**演示资产**，前端与 E2E 需要能稳定引用它们
    （与 `SCORING_DEMO_EVENT_ID` 用固定编号同一理由）。序列段从 9000 起，
    与运行时从 1 递增的用户自建用例天然错开。
    """
    col = db.get_db()[COLL_SIM_CASES]
    ts = now_ms()
    today = date_key(ts)
    for index, row in enumerate(sim_case_rows(), start=1):
        doc = {
            "_id": f"SIMC{today}900{index:03d}",
            "name": row["name"],
            "category": row["category"],
            "expected_decision": row["expected_decision"],
            "description": row["description"],
            "event_template": dict(row["event_template"]),
            "created_by": "seed",
            "created_at": ts,
            "status": "active",
        }
        await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return len(sim_case_rows())


# ============================================================
# 主流程
# ============================================================
async def reset_collections() -> int:
    """清空全部业务集合与序列计数器（`--reset`）。

    连 `seq_counters` 一起清：业务编号（EVT/DEC/CASE/LOG）是按天计数，
    若不清，两次 `--reset` 之后编号会从上次的位置继续，无法复现。

    **关于审计日志**：BR-12-11 禁止**应用代码**修改或删除审计（append-only）。
    本函数是演示复现工具而非应用路径，`--reset` 需要把审计链一并归零才能得到
    可复现的初始状态，因此这里会清空它——但**显式告知**，避免"审计被悄悄清掉"。
    """
    total = 0
    audit_deleted = 0
    for name in COLLECTION_NAMES:
        result = await db.get_db()[name].delete_many({})
        total += result.deleted_count
        if name == COLL_AUDIT_LOGS:
            audit_deleted = result.deleted_count
    if audit_deleted:
        print(f"  [注意] --reset 清空了 {audit_deleted} 条审计记录，"
              f"审计哈希链将从创世块重新开始（仅演示复现用；应用代码不得删除审计，BR-12-11）")
    return total


async def main(reset: bool, indexes_only: bool) -> int:
    config.validate()
    print(f"配置：{config.MONGO_URL} / 库={config.MONGO_DB_NAME}")

    connected, latency_ms, error = await db.ping()
    if not connected:
        print(f"[失败] MongoDB 不可达：{error}")
        return 1
    print(f"MongoDB 已连接（{latency_ms} ms）")

    indexes = await db.ensure_indexes()
    print(f"索引就绪（{len(indexes)} 个）：")
    for name in indexes:
        print(f"  · {name}")

    if indexes_only:
        await db.close()
        return 0

    if reset:
        deleted = await reset_collections()
        print(f"[--reset] 已清空 {deleted} 条既有数据")

    print("灌入种子数据：")
    scenes = await seed_rule_scenes()
    print(f"  + rule_scenes（E06 规则场景）{scenes} 行")
    rule_problems = validate_seed_rules()
    if rule_problems:
        # 种子自检失败**不静默继续**：灌进去的非法树只会表现为"规则永不命中"，
        # 那种缺陷在演示当天极难定位（见 `validate_seed_rules` 的说明）。
        print(f"[失败] E05 规则种子自检未通过（{len(rule_problems)} 项）：")
        for item in rule_problems:
            print(f"      ✗ {item}")
        await db.close()
        return 1
    rules = await seed_rules()
    enabled = sum(1 for row in SEED_RULES if row["status"] == "enabled")
    print(f"  + rules（E05 风控规则）{rules} 条（启用 {enabled} 条 / 停用 "
          f"{rules - enabled} 条，条件树与 18 项特征键已自检通过）")
    created, skipped = await seed_lists()
    print(f"  + list_entries（E07 名单条目）新增 {created} 行，跳过 {skipped} 行")
    users = await seed_sys_users()
    print(f"  + sys_users（E19 系统用户）{users} 行（密码已 bcrypt 哈希）")
    # 模块 13：E20 决策引擎配置（一行默认记录；`engine_type=model/hybrid` 仍被
    # SYS-4003 拒绝——这一行只是把"当前用 rule 引擎"变成库里的可审计事实）
    model_cfg = await seed_model_config()
    print(f"  + model_configs（E20 决策引擎配置）{model_cfg} 行"
          f"（engine_type=rule / fuse_mode=rule_first / 权重 1.0:0.0，"
          f"模型引擎未接入 G-01）")
    profiles = await seed_profiles()
    print("  + 画像与关联图谱（模块 09）：")
    for coll_name, count in profiles.items():
        if coll_name == "linked_user_cnt_reconciled":
            print(f"      冗余计数按边集合重算，修正 {count} 行"
                  f"（0 = 种子与边本来就一致）")
            continue
        print(f"      {coll_name:<20}{count} 行")
    print(f"      （含预置 IP 画像 {len(SIMULATOR_IPS) + 4} 个："
          f"E2E 用的 {CLUSTER_IP_MAIN}、模拟器四模式的 IP 池、1 个演示代理 IP、"
          f"团伙 B 的 {SCORING_IP}）")

    # 团伙 B 的演示事件：**真实 04 + 真实 05** 产出的一条"名单干净、规则计分"样本。
    # 没有它，前端在真实数据上只能看到名单直通（hits 恒空）——见 SCORING_USERS 的说明。
    # 先清掉上一次种子留下的两条演示案件（见 `cleanup_demo_cases` 的顺序说明）
    stale_cases = await cleanup_demo_cases()
    if stale_cases:
        print(f"  - 已清理上一次的演示案件 {stale_cases} 条（含其处置流水）")
    demo = await seed_demo_decision()
    print(f"  + 演示事件（团伙 B 的规则计分样本）{demo['event_id']}")
    print(f"      decision={demo['decision']} / rule_score={demo['rule_score']} / "
          f"命中 {demo['hit_rule_count']} 条 {demo['hits']} / degraded={demo['degraded']}")
    print(f"      snapshot={demo['snapshot_id']}（E02/E03/E04 均由真实 04/05 写入）")
    if demo["degraded"] or demo["decision"] != "review" or demo["hit_rule_count"] < 2:
        # 种子完备性门禁：这条数据存在的唯一理由就是让前端看到"规则累计计分 +
        # 命中明细表"。它一旦不成立（例如规则分值被改、画像给了两个标签导致
        # 总分变 90 → reject），就必须**在这里失败**，而不是静默产出一条
        # 演示不了计分的样本——那正是本缺口当初没被发现的原因。
        print(f"[失败] 演示事件未落在 review + ≥2 条命中："
              f"decision={demo['decision']} hits={demo['hit_rule_count']} "
              f"degraded={demo['degraded']}")
        await db.close()
        return 1

    # 模块 08：E08/E09 的演示案件（一条待审 + 一条已处置，全部由真实链路产出）
    cases = await seed_demo_cases(demo["event_id"], demo["decision_id"])
    print("  + 演示案件（模块 08：E08 案件 / E09 处置流水）：")
    print(f"      待审案件 {cases['pending_case_no']}"
          f"（由演示决策 {demo['decision_id']} 建出，status=pending）")
    print(f"      已处置案件 {cases['disposed_case_no']}"
          f"（decision={cases['disposed_decision']} / {cases['disposed_score']} 分 / "
          f"流水 {cases['disposed_actions']} 条 / status={cases.get('disposed_status')}）")
    problems = await _validate_seed_cases(cases)
    if problems:
        # 与规则种子、演示事件同一处置：**产不出就失败退出**。
        # 案件是工作台的入口，缺了它 07/08 的页面只能显示空态，
        # 而"空态"与"缺陷"在演示现场是分不清的。
        print(f"[失败] E08/E09 案件种子自检未通过（{len(problems)} 项）：")
        for item in problems:
            print(f"      ✗ {item}")
        await db.close()
        return 1

    # 模块 10：E17 的 4 条内置仿真用例（Spec §2.1 明文要求由种子脚本灌入）
    sim_problems = validate_sim_cases()
    if sim_problems:
        # 与规则种子、演示事件、案件同一处置：**产不出就失败退出**。
        # 缺了这些用例，仿真页左栏是空的，而"空态"与"缺陷"在演示现场分不清；
        # 更要紧的是**事件体必须过 03 的校验**——一条字段缺失的模板会在
        # 策略师点开之后才报 EVT-4004。
        print(f"[失败] E17 仿真用例种子自检未通过（{len(sim_problems)} 项）：")
        for item in sim_problems:
            print(f"      ✗ {item}")
        await db.close()
        return 1
    sim_cases = await seed_sim_cases()
    print(f"  + sim_cases（E17 仿真用例）{sim_cases} 条"
          f"（事件体已用 03 的真实校验器逐条验过）")
    for row in sim_case_rows():
        print(f"      {row['name']:<12} {row['event_template']['event_type']:<16}"
              f"预期 {row['expected_decision']}")

    print("\n当前各集合条数：")
    seeded = (COLL_LIST_ENTRIES, COLL_RULE_SCENES, COLL_RULES, COLL_SYS_USERS,
              COLL_SEQ_COUNTERS, COLL_USERS, COLL_DEVICES, COLL_IP_POOL,
              COLL_USER_ADDRESSES, COLL_ENTITY_EDGES, COLL_RISK_CASES,
              COLL_CASE_ACTIONS, COLL_RISK_EVENTS, COLL_DECISIONS,
              COLL_DECISION_HITS, COLL_FEATURE_SNAPSHOTS, COLL_SIM_CASES,
              COLL_MODEL_CONFIGS)
    for name in COLLECTION_NAMES:
        count = await db.get_db()[name].count_documents({})
        mark = "·" if count else " "
        print(f" {mark} {name:<20}{count}")
    empty = [n for n in COLLECTION_NAMES
             if n not in seeded and not await db.get_db()[n].count_documents({})]
    if empty:
        print("\n尚未灌入种子的集合（等所属模块实现后补充，非故障）：")
        for name in empty:
            print(f"  - {name:<20}{PENDING_SEEDS.get(name, '（待登记）')}")

    print(f"\n降级状态：{DEGRADED.snapshot()}")
    print(f"完成于 {now_ms()}")
    await db.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="shop_risk_control 种子脚本")
    parser.add_argument("--reset", action="store_true",
                        help="先清空业务集合与序列计数器再灌（用于复现演示数据）")
    parser.add_argument("--indexes-only", action="store_true",
                        help="只建索引，不灌数据")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.reset, args.indexes_only)))
