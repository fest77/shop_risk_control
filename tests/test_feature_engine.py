# -*- coding: utf-8 -*-
"""模块 04 纯逻辑验收：18 项特征口径、窗口语义、静态基线、分位函数。

覆盖 `V-04-02`（键名与 E02 一致）、`V-04-03`（当前事件计入窗口）、
`V-04-04`（窗口边界）、`V-04-05`（分母 0 标记缺失）、`V-04-06`（冷启动如实标记）、
`V-04-07`（聚集度向 09 取）、`V-04-08`（容量截断）、`V-04-09`（sweep 回收）、
`V-04-10`（window_config 可复算）、`V-04-13`（基线配置完整）。

**为什么这些用例不碰 HTTP**：`feature_window` / `feature_compute` 是纯逻辑，
用注入的时钟与内存窗口就能把边界钉死；把它们混进接口用例会让失败原因变模糊
（到底是算法错了还是接口传错了）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config
from app.engine.feature_baselines import (
    NO_BASELINE,
    REFERENCE_BASELINES,
    assert_baselines_are_sane,
    has_baseline,
    reference_of,
)
from app.engine.feature_compute import (
    FEATURE_DATA_TYPES,
    FEATURE_KEYS,
    FEATURE_LABELS,
    LINKED_COUNT_FEATURES,
    ProfileData,
    compute_features,
)
from app.engine.feature_window import (
    QUEUE_CAPACITY,
    FeatureWindow,
    build_view,
)
from app.repos.feature_repo import percentile
from tests.feature_testlib import (
    ANCHOR_TS,
    FULL_PROFILE,
    ExplodingLinkedUserCountProvider,
    FakeLinkedUserCountProvider,
    WINDOW_CONFIG,
    entry,
    make_event,
    make_window,
    view_with,
)

pytestmark = pytest.mark.anyio

#: E02 的 18 个键名（`01_数据实体/数据实体设计.md` 的 `features` 键值结构表）。
#:
#: 这里**硬写一份**是刻意的：`V-04-02` 要的是"与 Step1 逐项一致"，
#: 若从实现里推出来就变成了自证。下面 `test_feature_keys_match_step1_document`
#: 会再拿 Step1 的**原始文档**解析一遍，两道一起构成完整证据链。
E02_KEYS_IN_ORDER: tuple[str, ...] = (
    "login_cnt_1h",
    "coupon_cnt_1h",
    "order_cnt_1h",
    "order_cnt_24h",
    "pay_fail_cnt_24h",
    "aftersale_cnt_24h",
    "aftersale_rate_24h",
    "device_user_cnt",
    "device_order_cnt_1h",
    "device_age_hours",
    "ip_user_cnt",
    "ip_order_cnt_1h",
    "ip_is_proxy",
    "address_user_cnt",
    "address_aftersale_cnt",
    "user_age_days",
    "user_level",
    "user_risk_tag_cnt",
)

#: E02 的分组归属（用于核对分组顺序，防止"键名对但分组错"）
E02_GROUPS_IN_ORDER: tuple[str, ...] = (
    "行为频次", "行为频次", "行为频次", "行为频次", "行为频次", "行为频次", "行为频次",
    "设备环境", "设备环境", "设备环境",
    "网络 IP", "网络 IP", "网络 IP",
    "地址聚集", "地址聚集",
    "账号画像", "账号画像", "账号画像",
)


def parse_step1_e02_keys() -> tuple[list[str], list[str]]:
    """从 Step1 的原始文档里解析 E02 的特征名与分组（`V-04-02` 的证据来源）。

    解析规则：定位含「分组 | 特征名」的表头行，向下读取连续的表格行；
    「分组」列有值时（如 `| 行为频次 | ...`）记录分组，为空（`| | ...`）时
    沿用上一个分组——这正是该表的写法。
    """
    path = Path(config.ROOT, "Spec_coding_步骤", "01_数据实体", "数据实体设计.md")
    lines = path.read_text(encoding="utf-8").splitlines()
    header = next(
        (i for i, line in enumerate(lines) if "特征名" in line and "分组" in line), None
    )
    assert header is not None, f"未在 {path} 找到 E02 的特征名表头"

    keys: list[str] = []
    groups: list[str] = []
    current_group = ""
    for line in lines[header + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0]:
            current_group = cells[0]
        match = re.fullmatch(r"`([a-z0-9_]+)`", cells[1])
        if not match:
            continue
        keys.append(match.group(1))
        groups.append(current_group)
    return keys, groups


# ============================================================
# V-04-02 键名与 E02 逐项一致
# ============================================================
async def test_feature_keys_match_e02_constant():
    """`V-04-02` / BR-04-06：18 项键名与 Step1 E02 **逐项、按序**一致。"""
    assert list(FEATURE_KEYS) == list(E02_KEYS_IN_ORDER)
    assert len(FEATURE_KEYS) == 18, f"必须有 18 项，实际 {len(FEATURE_KEYS)}"
    assert len(set(FEATURE_KEYS)) == 18, "键名不得重复"


async def test_feature_keys_match_step1_document():
    """`V-04-02` 的文档级证据：直接解析 `数据实体设计.md` 的 E02 表并比对。

    这条比上一条更强：它抓的是"实现与**冻结文档**漂移"这件事本身。
    将来有人在 E02 加了第 19 项而没同步代码（或反之），这里立刻变红。
    """
    keys, groups = parse_step1_e02_keys()
    assert keys, "E02 特征名解析为空，测试本身失效了"
    assert keys == list(E02_KEYS_IN_ORDER), (
        f"实现与 Step1 E02 的键名不一致：\n  文档={keys}\n  实现={list(E02_KEYS_IN_ORDER)}"
    )
    assert groups == list(E02_GROUPS_IN_ORDER), f"分组归属不一致：{groups}"
    assert len(keys) == 18, f"E02 应有 18 项，解析到 {len(keys)}"


async def test_every_feature_has_label_type_and_baseline_entry():
    """18 项都必须有中文标签、数据类型与基线登记（缺一即界面渲染不出来）。"""
    for key in FEATURE_KEYS:
        assert FEATURE_LABELS.get(key), f"{key} 缺少中文标签"
        assert FEATURE_DATA_TYPES.get(key) in ("int", "float", "bool", "str"), (
            f"{key} 的数据类型未登记或非法：{FEATURE_DATA_TYPES.get(key)}"
        )
        assert key in REFERENCE_BASELINES, f"{key} 未在静态基线表登记（无基线也要显式写）"


# ============================================================
# V-04-03 当前事件计入窗口
# ============================================================
async def test_first_ever_event_counts_itself():
    """`V-04-03` / BR-04-02：首发 `coupon_receive` 后 `coupon_cnt_1h == 1`（不是 0）。

    这条钉的是"先 ingest 再 compute"这个顺序。若实现改成先算后记，
    首发事件会得到 0——而 0 的含义是"该用户从没领过券"，正是 fail-closed
    要避免的那种"把这一次活动的证据抹掉"。
    """
    window = make_window()
    event = make_event("EVT1", ANCHOR_TS, "coupon_receive", user_id="u1")
    assert await window.ingest(event) is True

    view = window.plan(event)
    result = compute_features(event, view, ProfileData.empty())

    assert result.features["coupon_cnt_1h"] == 1, (
        "首发事件必须计入窗口（BR-04-02），否则特征会低估当次风险"
    )
    assert result.features["coupon_cnt_1h"] != 0


async def test_ingest_is_idempotent_by_event_id():
    """§3.1：同一 `event_id` 重复写入**不重复计数**。"""
    window = make_window()
    event = make_event("EVT_DUP", ANCHOR_TS, "coupon_receive", user_id="u1")
    assert await window.ingest(event) is True
    assert await window.ingest(event) is False, "重复事件应被识别"

    view = window.plan(event)
    result = compute_features(event, view, ProfileData.empty())
    assert result.features["coupon_cnt_1h"] == 1
    assert window.stats()["duplicate_cnt"] == 1


# ============================================================
# V-04-04 窗口边界（左闭右开 + 含当前笔）
# ============================================================
async def test_short_window_boundary_59_in_61_out():
    """`V-04-04` / BR-04-01/03：59 分钟前计入短窗，61 分钟前不计入。

    时间基准是**事件时间 ts**（BR-04-04），不是服务器接收时间。
    """
    window = make_window()
    now = ANCHOR_TS
    events = [
        make_event("EVT_OLD", now - 61 * 60_000, "login", user_id="u1"),
        make_event("EVT_EDGE", now - 59 * 60_000, "login", user_id="u1"),
        make_event("EVT_NOW", now, "login", user_id="u1"),
    ]
    for event in events:
        await window.ingest(event)

    view = window.plan(events[-1])
    count_1h = view.count_short("user", "login")
    assert count_1h == 2, f"59 分钟前与当前笔应计入（61 分钟前的 1 条不计），实际 {count_1h}"

    # 恰好 60 分钟前：边界值，左闭 → 计入（`[ts - 60min, ts]`）
    edge = make_event("EVT_EXACT", now - 60 * 60_000, "login", user_id="u2")
    await window.ingest(edge)
    exact_view = window.plan(edge)
    assert exact_view.count_short("user", "login") == 1, "恰好 60 分钟前应计入（左闭）"


async def test_long_window_keeps_24h_data_out_of_short_window():
    """长窗 1440 分钟内的数据进 `*_24h`，但不进 `*_1h`。"""
    window = make_window()
    now = ANCHOR_TS
    for index, offset_min in enumerate((120, 300, 1400)):
        await window.ingest(
            make_event(f"EVT{index}", now - offset_min * 60_000, "order_create", user_id="u1")
        )
    current = make_event("EVT_NOW", now, "order_create", user_id="u1")
    await window.ingest(current)

    view = window.plan(current)
    assert view.count_short("user", "order_create") == 1, "只有当前这一笔在 1h 内"
    assert view.count_long("user", "order_create") == 4, "4 笔全在 24h 内"


async def test_out_of_order_event_is_inserted_at_right_position_and_counted():
    """`V-04-04` / BR-04-04 / FEA-5004：乱序事件按 `ts` 插入正确位置并计数告警。"""
    window = make_window()
    now = ANCHOR_TS
    await window.ingest(make_event("EVT_LATE", now - 30 * 60_000, "login", user_id="u1"))
    # 更早的一条（乱序）后到：必须插到前面，否则窗口区间裁剪会算错
    await window.ingest(make_event("EVT_EARLIER", now - 50 * 60_000, "login", user_id="u1"))
    current = make_event("EVT_NOW", now, "login", user_id="u1")
    await window.ingest(current)

    assert window.out_of_order_cnt >= 1, "乱序必须被计数告警（FEA-5004）"
    view = window.plan(current)
    assert view.count_short("user", "login") == 3, "乱序插入后三条都应落在 1h 内"

    # 队列内部必须保持按 ts 升序（这是"插入正确位置"的直接证据）
    entries = window._queues["user"]["u1"].entries
    assert [e[1] for e in entries] == sorted(e[1] for e in entries)


# ============================================================
# V-04-05 分母为 0 标记缺失
# ============================================================
async def test_aftersale_rate_missing_when_no_orders():
    """`V-04-05` / BR-04-08：无订单用户发售后事件 → 退款率**缺失**且不是 0。"""
    window = make_window()
    event = make_event("EVT_AS", ANCHOR_TS, "after_sale_apply", user_id="u_new")
    await window.ingest(event)

    view = window.plan(event)
    result = compute_features(event, view, ProfileData.empty())

    assert result.features.get("order_cnt_24h") == 0, "确实没有订单（这是真实结论）"
    assert "aftersale_rate_24h" in result.missing_features, (
        "分母为 0 时必须标记缺失（BR-04-08）"
    )
    assert "aftersale_rate_24h" not in result.features, (
        "缺失项**不得**出现在 features 里（用 0 冒充会让规则误判为低风险）"
    )
    assert result.missing_reasons["aftersale_rate_24h"]


async def test_aftersale_rate_computed_when_denominator_positive():
    """分母 > 0 时按 `售后数 / max(订单数, 1)` 计算，保留 4 位小数。"""
    window = make_window()
    now = ANCHOR_TS
    await window.ingest(make_event("EVT_O1", now - 60_000, "order_create", user_id="u1"))
    await window.ingest(make_event("EVT_O2", now - 50_000, "order_create", user_id="u1"))
    await window.ingest(make_event("EVT_A1", now - 40_000, "after_sale_apply", user_id="u1"))
    current = make_event("EVT_A2", now, "after_sale_apply", user_id="u1")
    await window.ingest(current)

    view = window.plan(current)
    result = compute_features(current, view, ProfileData.empty())
    assert result.features["order_cnt_24h"] == 2
    assert result.features["aftersale_cnt_24h"] == 2
    assert result.features["aftersale_rate_24h"] == 1.0
    assert "aftersale_rate_24h" not in result.missing_features


async def test_pay_fail_count_only_counts_failures():
    """`pay_fail_cnt_24h` 只数失败笔数（`scene_extra.success=false`），不把成功算进去。"""
    window = make_window()
    now = ANCHOR_TS
    await window.ingest(make_event("EVT_P1", now - 30_000, "order_pay", user_id="u1", success=True))
    await window.ingest(make_event("EVT_P2", now - 20_000, "order_pay", user_id="u1", success=False))
    current = make_event("EVT_P3", now, "order_pay", user_id="u1", success=False)
    await window.ingest(current)

    view = window.plan(current)
    result = compute_features(current, view, ProfileData.empty())
    assert result.features["pay_fail_cnt_24h"] == 2, "三次支付里两次失败"


# ============================================================
# V-04-06 冷启动如实标记
# ============================================================
async def test_cold_start_marks_missing_without_fabricating_zeros():
    """`V-04-06` / BR-04-09/10：全新用户 + 无画像 → 缺失项非空且**不含伪造的 0**。"""
    window = make_window()
    event = make_event("EVT_NEW", ANCHOR_TS, "login", user_id="u_brand_new",
                       device_id="D_NEW", ip="203.0.113.77")
    await window.ingest(event)

    view = window.plan(event)
    result = compute_features(event, view, ProfileData.empty())

    assert result.missing_features, "冷启动必须如实标记缺失"
    # 这几项在无画像时**一定**不可计算
    for key in ("user_age_days", "user_level", "user_risk_tag_cnt",
                "ip_is_proxy", "address_aftersale_cnt", "device_user_cnt",
                "ip_user_cnt", "address_user_cnt"):
        assert key in result.missing_features, f"{key} 应标记缺失"
        assert key not in result.features, f"{key} 不得用 0/占位值冒充"

    # 而"确实发生过 0 次"的计数项应给出真实 0（这类 0 是结论，不是占位）
    assert result.features["coupon_cnt_1h"] == 0
    assert result.features["order_cnt_24h"] == 0
    assert "coupon_cnt_1h" not in result.missing_features


async def test_all_eighteen_keys_are_accounted_for():
    """18 项键名**恒在**：要么在 `features` 里，要么在 `missing_features` 里。

    这条挡住"某个特征既没算出来也没登记缺失"的静默丢项——那种情况下
    快照上看不出任何异常，而 05 求值时会拿到一个不存在的键。
    """
    window = make_window()
    event = make_event("EVT_X", ANCHOR_TS, "login", user_id="u_x")
    await window.ingest(event)
    result = compute_features(event, window.plan(event), ProfileData.empty())

    produced = set(result.features) | set(result.missing_features)
    assert produced == set(FEATURE_KEYS), (
        f"未覆盖的键：{set(FEATURE_KEYS) - produced}；多余的键：{produced - set(FEATURE_KEYS)}"
    )
    assert not (set(result.features) & set(result.missing_features)), (
        "同一个特征不得既在 features 又在 missing_features"
    )


# ============================================================
# V-04-07 聚集度向 09 取
# ============================================================
async def test_linked_counts_come_from_provider_verbatim():
    """`V-04-07` / BR-04-07：04 的三项聚集度与 09 给的值**完全相同**。"""
    provider = FakeLinkedUserCountProvider(
        {"device": 4, "ip": 9, "address": 2}
    )
    window = make_window()
    event = make_event("EVT_L", ANCHOR_TS, "order_create", user_id="u1",
                       device_id="D1", ip="198.51.100.9", address_id="A1")
    await window.ingest(event)

    view = window.plan(event)
    result = compute_features(
        event, view, FULL_PROFILE,
        linked_user_counts={"device": 4, "ip": 9, "address": 2},
    )
    assert result.features["device_user_cnt"] == 4
    assert result.features["ip_user_cnt"] == 9
    assert result.features["address_user_cnt"] == 2
    # 04 只能"问一次"，不得自己维护映射：值必须与 provider 完全相同
    assert provider.repeat == 0, "本用例直接传值，不经过 provider（provider 只在服务层用）"


async def test_linked_counts_none_marks_missing_not_zero():
    """09 返回 `None`（无法计算）时，三项进 `missing`，**不得回落成 0**。"""
    window = make_window()
    event = make_event("EVT_L2", ANCHOR_TS, "login", user_id="u1",
                       device_id="D1", ip="198.51.100.9")
    await window.ingest(event)

    result = compute_features(
        event, window.plan(event), FULL_PROFILE,
        linked_user_counts={"device": None, "ip": None, "address": None},
    )
    for key in LINKED_COUNT_FEATURES:
        assert key in result.missing_features, f"{key} 在 09 不可用时必须标记缺失"
        assert key not in result.features, f"{key} 不得用 0 冒充"


async def test_feature_compute_has_no_dedup_collection():
    """BR-04-07 的静态守卫：04 的引擎里**没有**自建账号去重集合。

    做法：扫描两个引擎文件，禁止出现"按实体聚合账号"的命名
    （`linked_users` / `seen_users` / `distinct_user` / `dedup` 等）。
    去重真源只能有一个（09 的 `get_linked_user_count`），
    若 04 自己维护一份，两处口径必然漂移，届时同一个"同设备账号数"
    在特征快照与图谱页上会显示两个不同的数字。
    """
    forbidden = re.compile(
        r"(seen_users|user_set|distinct_user|dedup|linked_users|users_by_)",
        re.IGNORECASE,
    )
    for name in ("feature_compute.py", "feature_window.py"):
        text = Path(config.ROOT, "app", "engine", name).read_text(encoding="utf-8")
        hits = [
            f"{name}:{index} {line.strip()}"
            for index, line in enumerate(text.splitlines(), 1)
            if forbidden.search(line)
        ]
        assert not hits, f"疑似自建账号去重集合：{hits}"

    # 三项聚集度的值只能来自外部注入的 `linked_user_counts`（= 09 的返回）
    compute_text = Path(
        config.ROOT, "app", "engine", "feature_compute.py"
    ).read_text(encoding="utf-8")
    assert "linked_user_counts" in compute_text
    assert "LINKED_COUNT_FEATURES" in compute_text


# ============================================================
# V-04-08 容量截断
# ============================================================
async def test_queue_capacity_truncates_and_marks():
    """`V-04-08` / BR-04-12 / FEA-5003：灌满 >10000 条 → 队列被限长且留下截断标记。"""
    window = make_window()
    user = "u_flood"
    total = QUEUE_CAPACITY + 1
    for index in range(total):
        await window.ingest(
            make_event(f"EVT_F{index}", ANCHOR_TS + index, "login", user_id=user)
        )

    queue = window._queues["user"][user]
    assert len(queue) == QUEUE_CAPACITY, (
        f"单维度队列必须被限制在 {QUEUE_CAPACITY} 条，实际 {len(queue)}"
    )
    assert queue.truncated is True, "截断必须留痕（FEA-5003：不能静默）"

    current = make_event("EVT_LAST", ANCHOR_TS + total, "login", user_id=user)
    await window.ingest(current)
    assert "user" in window.truncated_dimensions(current), (
        "涉及的维度被截断时必须能被快照标记出来"
    )
    assert window.stats()["truncated_cnt"] >= 1


async def test_truncation_marker_survives_sweep():
    """截断标记不得被 sweep 清除：数据偏差仍在，黄条提示就不能消失。"""
    window = make_window()
    user = "u_flood2"
    for index in range(QUEUE_CAPACITY + 1):
        await window.ingest(
            make_event(f"EVT_G{index}", ANCHOR_TS + index, "login", user_id=user)
        )
    await window.sweep(ANCHOR_TS + QUEUE_CAPACITY + 10)
    queue = window._queues["user"][user]
    assert queue.truncated is True


# ============================================================
# V-04-09 sweep 回收与空键清理
# ============================================================
async def test_sweep_drops_expired_and_removes_empty_keys():
    """`V-04-09` / BR-04-13：25 小时前的数据被清理，空键被移除。"""
    window = make_window()
    old_ts = ANCHOR_TS - 25 * 3_600_000
    await window.ingest(make_event("EVT_ANCIENT", old_ts, "login", user_id="u_old"))
    await window.ingest(
        make_event("EVT_FRESH", ANCHOR_TS, "login", user_id="u_fresh")
    )
    assert window.stats()["distinct_keys"] == 2

    dropped = await window.sweep(ANCHOR_TS)
    assert dropped == 1, f"应丢弃 1 条超长窗条目，实际 {dropped}"
    assert "u_old" not in window._queues["user"], "空键必须被移除（否则内存不降反升）"
    assert "u_fresh" in window._queues["user"], "窗口内的数据不得被误删"
    assert window.stats()["last_sweep_at"] == ANCHOR_TS


async def test_sweep_does_not_throw_away_in_window_data():
    """sweep 边界：`ts` 恰好等于 cutoff 的条目留在窗口（长窗闭区间语义）。

    用**真实的当前时刻**（而不是固定锚点）作为 sweep 基准：`sweep()` 的
    cutoff = `now - long_window`，若这里拿一个过去的锚点去 sweep，
    锚点本身就会落在 cutoff 之前，测的就不是"边界保留"了。
    """
    window = make_window()
    from app.utils.timeutil import now_ms as _now

    moment = _now()
    cutoff_ts = moment - 1440 * 60_000
    await window.ingest(make_event("EVT_CUT", cutoff_ts, "login", user_id="u_cut"))
    dropped = await window.sweep(moment)
    assert dropped == 0, "恰好落在长窗左端的条目应保留"


async def test_sweep_prunes_seen_ids():
    """sweep 顺带回收去重集合（否则它会只增不减，成为最大的单块内存）。"""
    window = make_window()
    await window.ingest(
        make_event("EVT_GONE", ANCHOR_TS - 25 * 3_600_000, "login", user_id="u_old")
    )
    await window.ingest(make_event("EVT_STAY", ANCHOR_TS, "login", user_id="u_new"))
    assert window.stats()["seen_ids"] == 2

    await window.sweep(ANCHOR_TS)
    assert window.stats()["seen_ids"] == 1


# ============================================================
# V-04-10 window_config 完整且可复算
# ============================================================
async def test_window_config_is_complete():
    """`V-04-10` / BR-04-05：快照里的 `window_config` 四个字段齐全。"""
    window = make_window()
    config_out = window.window_config()
    assert config_out == WINDOW_CONFIG, (
        f"window_config 与 E02 的定义不一致：{config_out}"
    )


async def test_snapshot_is_recomputable_from_window_config():
    """`V-04-10`：用快照里的 `window_config` 重算，得到**相同**的特征值。

    做法：算出第一份特征 → 用同一份 `window_config` 构造视图重算 → 逐项相等。
    这条钉的是"窗口时长来自配置而不是全局常量"——若实现读的是全局常量，
    改配置后复算就会得到不同的值，历史快照也就不可复算了。
    """
    window = make_window()
    now = ANCHOR_TS
    await window.ingest(make_event("EVT_R1", now - 30 * 60_000, "order_create", user_id="u1"))
    current = make_event("EVT_R2", now, "order_create", user_id="u1")
    await window.ingest(current)

    view = window.plan(current)
    first = compute_features(current, view, FULL_PROFILE,
                             linked_user_counts={"device": 1, "ip": 1, "address": 1})

    # 用快照里的配置重建一个视图（模拟"事后复算"）
    rebuilt = build_view(
        event_ts=view.event_ts,
        identities=view.identities,
        entries=list(view.entries.items()),
        window_config=dict(view.window_config),
    )
    second = compute_features(current, rebuilt, FULL_PROFILE,
                              linked_user_counts={"device": 1, "ip": 1, "address": 1})
    assert first.features == second.features, "用 window_config 复算必须得到相同特征值"


async def test_custom_window_config_actually_changes_the_computation():
    """窗口时长必须**真的**来自配置：改成 30 分钟短窗后，45 分钟前的事件不再计入。

    这是上一条的反向证据：只断言"复算一致"可能被"两处都读全局常量"蒙混过关，
    因此再加一条"改配置必须改变结果"。
    """
    window = FeatureWindow(short_window_min=30, long_window_min=1440)
    now = ANCHOR_TS
    await window.ingest(make_event("EVT_C1", now - 45 * 60_000, "login", user_id="u1"))
    current = make_event("EVT_C2", now, "login", user_id="u1")
    await window.ingest(current)

    view = window.plan(current)
    result = compute_features(current, view, ProfileData.empty())
    assert view.window_config["short_window_min"] == 30
    assert result.features["login_cnt_1h"] == 1, "45 分钟前的事件不该进 30 分钟短窗"


# ============================================================
# V-04-13 静态基线完整且无基线有解释
# ============================================================
async def test_static_baselines_cover_all_features_and_are_sane():
    """`V-04-13` / BR-04-16/19：每项都登记了基线（含显式"无基线"），且配置自洽。"""
    assert_baselines_are_sane()
    assert set(REFERENCE_BASELINES) == set(FEATURE_KEYS), (
        f"基线表与特征键名不一致：缺 {set(FEATURE_KEYS) - set(REFERENCE_BASELINES)}，"
        f"多 {set(REFERENCE_BASELINES) - set(FEATURE_KEYS)}"
    )
    for key in FEATURE_KEYS:
        reference = reference_of(key)
        assert reference, f"{key} 的 reference 为空"
        if not has_baseline(key):
            assert reference == NO_BASELINE
            assert REFERENCE_BASELINES[key]["baseline_reason"], (
                f"{key} 无基线但没写明理由（BR-04-19 要求可解释，不能看起来像漏填）"
            )


async def test_documented_example_baselines_are_respected():
    """Spec §4.5 的「基线配置示例」逐项落地（这些是文档给死的值）。"""
    expected = {
        "device_user_cnt": "≤1",
        "coupon_cnt_1h": "≤2",
        "ip_is_proxy": "false",
        "aftersale_rate_24h": "≤5%",
        "order_cnt_24h": "2~5",
        "user_age_days": NO_BASELINE,
    }
    for key, reference in expected.items():
        assert reference_of(key) == reference, (
            f"{key} 的基线应为 {reference}，实际 {reference_of(key)}"
        )


async def test_no_baseline_never_renders_as_normal():
    """BR-04-19：无基线的项必须能给前端一个明确的"—"信号，而不是"正常"。"""
    assert not has_baseline("user_age_days")
    assert reference_of("user_age_days") == NO_BASELINE
    assert reference_of("不存在的特征") == NO_BASELINE, "未登记特征也走 — 而不是抛异常"


# ============================================================
# 分位函数（E21 统计基线的基础）
# ============================================================
async def test_percentile_matches_known_values():
    """P50/P95 的数值正确性（与 numpy 的线性插值口径一致，但不依赖 numpy）。"""
    values = [float(i) for i in range(1, 101)]     # 1..100
    assert percentile(values, 0.5) == pytest.approx(50.5)
    assert percentile(values, 0.95) == pytest.approx(95.05)
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 100.0


async def test_percentile_returns_none_for_empty_sample():
    """空样本 → `None`（不是 0）：没有基线必须能被区分出来。"""
    assert percentile([], 0.5) is None
    assert percentile([7.0], 0.95) == 7.0


# ============================================================
# 画像/依赖故障：只影响对应特征
# ============================================================
async def test_exploding_linked_provider_marks_three_missing():
    """09 抛异常时三项聚集度缺失，但**行为频次仍然算得出来**（可用子集）。"""
    provider = ExplodingLinkedUserCountProvider()
    window = make_window()
    event = make_event("EVT_E1", ANCHOR_TS, "coupon_receive", user_id="u1",
                       device_id="D1", ip="10.0.0.1")
    await window.ingest(event)

    result = compute_features(event, window.plan(event), ProfileData.empty(),
                              linked_user_counts={})
    assert result.features["coupon_cnt_1h"] == 1, "09 挂了不该影响行为频次"
    for key in LINKED_COUNT_FEATURES:
        assert key in result.missing_features
    assert provider.calls == 0  # 本用例直接传空 counts，provider 不被触碰


async def test_profile_failure_does_not_break_frequency_features():
    """画像读取失败只让画像类特征缺失（BR-04-09 的"可用子集"）。"""
    window = make_window()
    event = make_event("EVT_E2", ANCHOR_TS, "order_create", user_id="u1",
                       device_id="D1", ip="10.0.0.2", address_id="A1")
    await window.ingest(event)

    profile = ProfileData(errors=("user:PyMongoError", "device:PyMongoError"))
    result = compute_features(event, window.plan(event), profile,
                              linked_user_counts={"device": 1, "ip": 1, "address": 1})
    assert result.features["order_cnt_1h"] == 1
    assert result.features["device_order_cnt_1h"] == 1
    assert "user_age_days" in result.missing_features
    assert "user_age_days" not in result.features
    assert result.features["device_user_cnt"] == 1, "09 给了值就该用上"


async def test_device_age_prefers_profile_over_window_fallback():
    """`device_age_hours` 优先用 E11 的 `first_seen_at`；缺失时才回落窗口内最早时间。"""
    window = make_window()
    device_id = "D_AGE"
    await window.ingest(
        make_event("EVT_A1", ANCHOR_TS - 3_600_000, "login", user_id="u1", device_id=device_id)
    )
    current = make_event("EVT_A2", ANCHOR_TS, "login", user_id="u1", device_id=device_id)
    await window.ingest(current)
    view = window.plan(current)

    from_profile = compute_features(
        current, view, ProfileData(device_first_seen_at=ANCHOR_TS - 48 * 3_600_000),
        linked_user_counts={"device": 1, "ip": None, "address": None},
    )
    assert from_profile.features["device_age_hours"] == 48.0, "应优先用 E11 的长期口径"

    fallback = compute_features(
        current, view, ProfileData.empty(),
        linked_user_counts={"device": None},
        window_first_seen={"device": view.first_ts("device")},
    )
    assert fallback.features["device_age_hours"] == 1.0, (
        "画像缺失时回落到窗口内最早出现时间（1 小时）"
    )


async def test_device_age_missing_without_any_record():
    """既无画像也无窗口记录 → 设备时长必须缺失（BR-04-09）。"""
    current = make_event("EVT_A3", ANCHOR_TS, "login", user_id="u1", device_id="D_NONE")
    view = view_with(
        event_ts=ANCHOR_TS,
        identities={"user": "u1", "device": "D_NONE", "ip": None, "address": None},
        entries={("user", "u1"): [entry("EVT_A3", ANCHOR_TS, "login")]},
    )
    result = compute_features(current, view, ProfileData.empty())
    assert "device_age_hours" in result.missing_features
    assert "device_age_hours" not in result.features


async def test_ip_is_proxy_true_is_a_real_value_not_missing():
    """`ip_is_proxy=true` 是**真实取值**（不是缺失）：代理 IP 本身就是要判的风险。"""
    current = make_event("EVT_P", ANCHOR_TS, "login", user_id="u1", ip="10.0.0.9")
    view = view_with(
        event_ts=ANCHOR_TS,
        identities={"user": "u1", "device": None, "ip": "10.0.0.9", "address": None},
        entries={("ip", "10.0.0.9"): [entry("EVT_P", ANCHOR_TS, "login")]},
    )
    result = compute_features(current, view, ProfileData(ip_is_proxy=True))
    assert result.features["ip_is_proxy"] is True
    assert "ip_is_proxy" not in result.missing_features


async def test_user_age_days_is_integer_days():
    """`user_age_days` 取整（E02 的类型是整型天数）。"""
    current = make_event("EVT_AGE", ANCHOR_TS, "login", user_id="u1")
    view = view_with(
        event_ts=ANCHOR_TS,
        identities={"user": "u1", "device": None, "ip": None, "address": None},
        entries={},
    )
    result = compute_features(
        current, view, ProfileData(user_register_at=ANCHOR_TS - 3 * 86_400_000 - 5000),
    )
    assert result.features["user_age_days"] == 3, "不足 4 天应向下取整为 3"
    assert isinstance(result.features["user_age_days"], int)


async def test_user_age_days_clamped_to_zero_when_clock_is_ahead():
    """注册时间晚于事件时间（时钟问题）时夹到 0，不产生负年龄。"""
    current = make_event("EVT_AGE2", ANCHOR_TS, "login", user_id="u1")
    view = view_with(
        event_ts=ANCHOR_TS,
        identities={"user": "u1", "device": None, "ip": None, "address": None},
        entries={},
    )
    result = compute_features(
        current, view, ProfileData(user_register_at=ANCHOR_TS + 86_400_000),
    )
    assert result.features["user_age_days"] == 0
