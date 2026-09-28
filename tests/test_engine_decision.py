# -*- coding: utf-8 -*-
"""规则决策编排、E03/E04 落库与 V-05-01 ~ V-05-13 的验收证据。

## 这些用例为什么都直接调 `decide()`

`decide()` 是**唯一的判定实现**（03 的事件链路经 `RuleDecisionProvider`、
10 的仿真链路经 `/engine/evaluate`，两条都落到这里）。直接调它可以用显式的
`features` 把"特征是什么"钉死，从而让分值断言完全确定——走 HTTP 的话，
特征由 04 按真实窗口/画像算出来，断言就得跟着变，测的也不再是 05 的计分逻辑。

HTTP 面（入参校验、状态码、trace）在 `test_engine_api.py`；依赖故障在
`test_engine_failclosed.py`。
"""
from __future__ import annotations

import time

import pytest

from app import db
from app.constants import COLL_DECISIONS, COLL_DECISION_HITS
from app.engine.decision import ENGINE_VERSION, STATS, decide
from app.engine.rule_engine import scene_for
from app.repos.rule_repo import RuleRepo

from tests.engine_testlib import (
    branch,
    flush_decisions,
    install_lists,
    install_rules,
    leaf,
    list_doc,
    make_event,
    rule_doc,
)

pytestmark = pytest.mark.anyio


# ============================================================
# V-05-01：预设规则集下的三档决策
# ============================================================
async def test_v05_01_three_bands_from_the_preset_rule_set():
    """V-05-01：同一套规则、四个不同用户 → `pass` / `review` / `reject`。

    规则集与 `scripts/seed.py` 的 `login` 场景同构（**45 / 20 / 25** 分），
    因此这条用例同时是"种子规则真的能构造出三档"的证据（任务书 §8 的要求）。

    | 特征 | 命中 | 分值 | 决策 |
    |---|---|---|---|
    | 干净用户 | 无 | 0 | pass |
    | 同设备 5+ 账号 + 高频登录（用到窗口） | 001+002 | 65 | review |
    | **同设备 5+ 账号 + 代理 IP 且账号很新（不用窗口）** | 001+003 | **70** | review |
    | 三条全中 | 001+002+003 | 90 | reject |

    第三行就是 `scripts/seed.py` 里 `SCORING_USERS`（团伙 B，`U000132`）的档位：
    **名单干净、却由规则实打实累加出 2 条命中**——前端在真实数据上能看到
    "计分 + 命中明细表"，靠的正是它（见
    `test_seed_scoring_group_is_clean_and_scores_seventy`）。
    """
    await install_rules(
        rule_doc("RLOGIN001", leaf("device_user_cnt", "gte", 5), score=45,
                 scene_code="login", name="同设备聚集登录", priority=10),
        rule_doc("RLOGIN002", leaf("login_cnt_1h", "gte", 10), score=20,
                 scene_code="login", name="高频登录", priority=20),
        rule_doc("RLOGIN003",
                 branch("and", leaf("ip_is_proxy", "eq", True),
                        branch("or", leaf("user_age_days", "lte", 7),
                               leaf("user_risk_tag_cnt", "gte", 2))),
                 score=25, scene_code="login", name="代理IP新账号登录", priority=30),
    )

    clean = await decide(make_event(), {"device_user_cnt": 1, "login_cnt_1h": 2,
                                        "ip_is_proxy": False, "user_age_days": 400,
                                        "user_risk_tag_cnt": 0}, dry_run=True)
    assert clean.block["rule_score"] == 0
    assert clean.block["hit_rule_count"] == 0
    assert clean.block["decision"] == "pass"
    assert clean.block["risk_level"] == "low"

    frequent = await decide(make_event(), {"device_user_cnt": 12, "login_cnt_1h": 15,
                                           "ip_is_proxy": False, "user_age_days": 400,
                                           "user_risk_tag_cnt": 0}, dry_run=True)
    assert frequent.block["rule_score"] == 65
    assert frequent.block["hit_rule_count"] == 2
    assert frequent.block["decision"] == "review"
    assert frequent.block["risk_level"] == "medium"

    # 演示主路径：**不依赖滑动窗口**（`login_cnt_1h` 只有 1）
    demo = await decide(make_event(), {"device_user_cnt": 6, "login_cnt_1h": 1,
                                       "ip_is_proxy": True, "user_age_days": 2,
                                       "user_risk_tag_cnt": 1}, dry_run=True)
    assert demo.block["rule_score"] == 70
    assert demo.block["hit_rule_count"] == 2
    assert demo.block["decision"] == "review"
    assert [h["rule_code"] for h in demo.block["hits"]] == ["RLOGIN001", "RLOGIN003"]

    high = await decide(make_event(), {"device_user_cnt": 12, "login_cnt_1h": 15,
                                       "ip_is_proxy": True, "user_age_days": 3,
                                       "user_risk_tag_cnt": 0}, dry_run=True)
    assert high.block["rule_score"] == 90
    assert high.block["hit_rule_count"] == 3
    assert high.block["decision"] == "reject"
    assert high.block["risk_level"] == "high"


async def test_seed_scoring_group_is_clean_and_scores_seventy():
    """**种子完备性门禁**（对齐决策 D51）：必须存在一个"名单干净却会命中规则"的演示用户。

    ## 这条用例为什么必须存在

    名单种子原先覆盖太广（`U000128` 在黑名单、`U000129` 与它同设备/IP/地址、
    `U009999` 在白名单），于是**任何演示事件都先撞名单直通**：按 BR-05-02/03
    直通时 `hits=[]`、`rule_score=0`、不求值任何规则——页面上**永远看不到
    "规则累计计分 + 命中明细表"**。功能实现了、别的测试也全绿，但那一格
    在真实数据上从未被走到（前端用真实数据实测才发现）。

    这与 09 的"孤立账号分支不可达"是同一类缺陷：**种子让分支永远走不到**。
    因此这里把它钉成一条**纯计算**（不碰库、不依赖用例顺序）的门禁：
    任何人改动了 `SEED_RULES` 的分值、或给演示用户多挂一个风险标签，
    这条会立刻变红，而不是等到演示当天才发现"这页怎么只有直通"。
    """
    from app.engine import rule_engine
    from scripts.seed import (
        CLUSTER_ADDRESS,
        CLUSTER_DEVICE,
        CLUSTER_IP_MAIN,
        SCORING_ADDRESS,
        SCORING_DEMO_USER,
        SCORING_DEVICE,
        SCORING_IP,
        SCORING_USERS,
        SEED_LISTS,
        SEED_RULES,
    )

    # 1) 不在任何名单里（否则又会撞直通）
    listed = {(row["entity_type"], row["entity_value"]) for row in SEED_LISTS}
    assert ("user", SCORING_DEMO_USER) not in listed
    assert ("device", SCORING_DEVICE) not in listed
    assert ("ip", SCORING_IP) not in listed
    assert ("address", SCORING_ADDRESS) not in listed
    # 2) 不与 U000128 共用设备/IP/地址（共用会间接撞上那几条黑名单）
    assert SCORING_DEVICE != CLUSTER_DEVICE
    assert SCORING_IP != CLUSTER_IP_MAIN
    assert SCORING_ADDRESS != CLUSTER_ADDRESS
    # 3) 6 个账号共用设备 → `device_user_cnt = 6 > 阈值 5`（留一格余量）
    assert len(SCORING_USERS) == 6
    # 4) 恰好一个风险标签：多一个就会命中 `RCOMMON001`（+20 → 90 → reject）
    assert len(SCORING_USERS[0][5]) == 1

    # 5) 用**种子里的真实规则**算出 70 分 / 2 条命中 / medium/review
    rules = [row for row in SEED_RULES
             if row["status"] == "enabled" and row["scene_code"] in ("login", "common")]
    evaluation = rule_engine.evaluate_rules(rules, {
        "device_user_cnt": 6,     # E11 的 linked_user_cnt（6 条 used_device 边）
        "login_cnt_1h": 1,        # **刻意只有 1**：证明不依赖滑动窗口（G-02）
        "ip_is_proxy": True,      # E12 的 is_proxy（手工标注为机房出口）
        "user_age_days": 2,       # E10 的 register_at（注册 3 天 → 向下取整 2 天）
        "user_risk_tag_cnt": 1,   # E10 的 risk_tags 长度
    })
    assert [h.rule_code for h in evaluation.hits] == ["RLOGIN001", "RLOGIN003"]
    assert evaluation.total == 70
    assert rule_engine.finalize(evaluation) == (70, "medium", "review")


# ============================================================
# V-05-02 / V-05-03：名单直通（且**不求值任何规则**）
# ============================================================
class CountingRuleRepo:
    """包一层规则仓储，统计"到底有没有去取规则"。

    V-05-02 要求直通时"**不求值任何规则**"。只断言 `hits=[]`/`rule_score=0`
    证明不了这一点（没有规则命中时也是这两个值），必须证明取数这一步根本没发生。
    """

    def __init__(self) -> None:
        self.inner = RuleRepo(db.get_db())
        self.calls = 0

    async def list_enabled_rules(self, scene_code: str):
        self.calls += 1
        return await self.inner.list_enabled_rules(scene_code)


@pytest.mark.parametrize("list_type,expected", [("white", "pass"), ("black", "reject")])
async def test_v05_02_list_direct_pass_never_evaluates_rules(list_type, expected):
    """V-05-02：白名单直接放行、黑名单直接拦截，且**一次规则都不取**。"""
    await install_rules(
        # 一条"必然命中"的高分规则：若直通路径还去求值，它一定会让分数不为 0
        rule_doc("RMUSTHIT", leaf("login_cnt_1h", "gte", 1), score=100,
                 scene_code="login"),
    )
    await install_lists(list_doc(list_type, "user", "U000001"))
    repo = CountingRuleRepo()
    outcome = await decide(make_event(user_id="U000001"), {"login_cnt_1h": 99},
                           rule_repo=repo, dry_run=True)

    assert outcome.block["decision"] == expected
    assert outcome.block["rule_score"] == 0
    assert outcome.block["hit_rule_count"] == 0
    assert outcome.block["hits"] == []
    assert outcome.block["rule_versions"] == {}, "没有规则参与，版本快照必须是空的"
    assert repo.calls == 0, "名单直通**不得**去取规则（V-05-02 的字面要求）"
    assert outcome.block["list_hit"] == {
        "hit": True, "list_type": list_type, "entity_type": "user",
        "entity_value": "U000001",
    }


async def test_v05_03_black_list_wins_when_both_match():
    """V-05-03 / BR-05-04：同一实体黑白同中 → 黑优先。"""
    await install_lists(
        list_doc("white", "device", "D8F2A1C4", entry_id="LW2"),
        list_doc("black", "device", "D8F2A1C4", entry_id="LB2"),
    )
    outcome = await decide(make_event(device_id="D8F2A1C4"), {}, dry_run=True)
    assert outcome.block["decision"] == "reject"
    assert outcome.block["list_hit"]["list_type"] == "black"
    assert outcome.block["rule_score"] == 0


# ============================================================
# V-05-04：灰名单不影响决策结果
# ============================================================
async def test_v05_04_gray_list_does_not_change_anything():
    """V-05-04 / BR-05-06：灰名单实体的决策与**不加名单**时逐字段一致。

    比较的是**整个决策块**（除耗时），而不是只看 `decision`——后者可能因为
    "恰好没有规则命中"而碰巧相等，证明不了灰名单真的没参与。
    """
    await install_rules(
        rule_doc("RLOGIN001", leaf("device_user_cnt", "gte", 5), score=40,
                 scene_code="login"),
    )
    event = make_event(ip="117.136.12.88")
    features = {"device_user_cnt": 12}

    baseline = await decide(event, features, dry_run=True)
    await install_lists(list_doc("gray", "ip", "117.136.12.88"))
    with_gray = await decide(event, features, dry_run=True)

    def strip_elapsed(block: dict) -> dict:
        return {k: v for k, v in block.items() if k != "elapsed_ms"}

    assert strip_elapsed(with_gray.block) == strip_elapsed(baseline.block)
    assert with_gray.block["rule_score"] == 40, "灰名单没有把分值抹平（也没加过分）"
    assert with_gray.block["decision"] == "pass"        # 40 分属低档
    assert with_gray.block["list_hit"]["hit"] is False, "灰名单不产生直通"


# ============================================================
# V-05-05：累加超 100 截断
# ============================================================
async def test_v05_05_score_over_100_is_truncated():
    """V-05-05 / BR-05-16：三条 40 分规则全命中 → 100（**不是 120**）。"""
    await install_rules(*[
        rule_doc(f"RTRUNC{i}", leaf("login_cnt_1h", "gte", 1), score=40,
                 scene_code="login", priority=i)
        for i in range(1, 4)
    ])
    outcome = await decide(make_event(), {"login_cnt_1h": 5}, dry_run=True)
    assert outcome.block["rule_score"] == 100
    assert outcome.block["final_score"] == 100
    assert outcome.block["decision"] == "reject"
    # 三条都命中（截断只作用于总分，不影响命中明细——否则"命中 3 条却只显示 1 条"）
    assert outcome.block["hit_rule_count"] == 3
    assert sum(h["score"] for h in outcome.block["hits"]) == 120


# ============================================================
# V-05-06：特征缺失不抛异常、不计分
# ============================================================
async def test_v05_06_missing_features_do_not_raise_or_score():
    """V-05-06 / BR-05-12：没有 `device_user_cnt` 的事件 → 规则不命中、无 500。"""
    await install_rules(
        rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40, scene_code="login"),
        rule_doc("REXISTS", leaf("device_user_cnt", "exists"), score=10,
                 scene_code="login", priority=20),
        rule_doc("RLT", leaf("device_user_cnt", "lt", 5), score=10,
                 scene_code="login", priority=30),
    )
    outcome = await decide(make_event(), {"login_cnt_1h": 1}, dry_run=True)

    # `gte` 与 `lt` 在缺失时都是 false（不是"不满足 gte 就等于满足 lt"）
    assert outcome.block["rule_score"] == 0
    assert outcome.block["hits"] == []
    assert outcome.block["decision"] == "pass"
    assert not [w for w in outcome.warnings if w["code"] == "RUL-5003"], (
        "特征缺失是常态（BR-05-12），不是规则求值失败，不该产生 RUL-5003 告警"
    )


# ============================================================
# BR-05-14：priority 只排序、不短路
# ============================================================
async def test_priority_sorts_but_never_short_circuits():
    """BR-05-14：`priority` 只用于稳定排序与展示，**不做短路**。

    若实现成"高优先级命中即停"，总分就变成"第一条命中规则的分值"，
    `rule_score`、`hit_rule_count`、`rule_versions` 三处同时失真。
    这里三条规则全部命中，断言总分是三者之和、且输出顺序按 priority 稳定。
    """
    await install_rules(
        rule_doc("RC", leaf("a", "gte", 1), score=50, scene_code="login", priority=30),
        rule_doc("RA", leaf("b", "gte", 1), score=10, scene_code="login", priority=10),
        rule_doc("RB", leaf("c", "gte", 1), score=20, scene_code="login", priority=20),
    )
    outcome = await decide(make_event(), {"a": 1, "b": 1, "c": 1}, trace=True, dry_run=True)
    assert outcome.block["rule_score"] == 80
    assert outcome.block["hit_rule_count"] == 3
    assert [row["rule_code"] for row in outcome.trace or []] == ["RA", "RB", "RC"], (
        "trace 的顺序必须是 priority 升序（稳定、可复现）"
    )
    # `hits` 按 score 降序（Spec §2.2 的展示要求，后端先排一次让三处消费方一致）
    assert [h["rule_code"] for h in outcome.block["hits"]] == ["RC", "RB", "RA"]


# ============================================================
# BR-05-08（D12）：`order_pay → pay`
# ============================================================
async def test_order_pay_maps_to_pay_scene_so_pay_rules_are_reachable():
    """决策 D12：`order_pay` 必须映射到 `pay` 场景。

    原实现把它映射到 `order`，于是 E06 定义的 `pay` 场景**永远不可达**——
    数据行存在、页面下拉里有它、配了规则却一条都不会被求值，且没有任何报错。
    这条用例就是那个坑的守门人：`pay` 场景的规则必须真的能命中支付事件。
    """
    assert scene_for("order_pay") == "pay"
    assert scene_for("order_create") == "order"
    assert scene_for("after_sale_apply") == "aftersale"
    assert scene_for("coupon_receive") == "coupon"
    assert scene_for("login") == "login"

    await install_rules(
        rule_doc("RPAY001", leaf("pay_fail_cnt_24h", "gte", 3), score=30,
                 scene_code="pay", name="支付失败试探"),
        rule_doc("RORDER001", leaf("order_cnt_1h", "gte", 3), score=40,
                 scene_code="order", name="地址聚集刷单"),
    )
    pay = await decide(make_event(event_type="order_pay"),
                       {"pay_fail_cnt_24h": 5, "order_cnt_1h": 9}, dry_run=True)
    assert pay.block["rule_score"] == 30, "`pay` 场景的规则必须可达"
    assert [h["rule_code"] for h in pay.block["hits"]] == ["RPAY001"]

    order = await decide(make_event(event_type="order_create"),
                         {"pay_fail_cnt_24h": 5, "order_cnt_1h": 9}, dry_run=True)
    assert [h["rule_code"] for h in order.block["hits"]] == ["RORDER001"], (
        "`order` 场景不该取到 `pay` 的规则（反之亦然）"
    )


async def test_common_scene_rules_apply_to_every_scene():
    """BR-05-09 / D10：`common` 是**数据行**，对全部场景生效。

    这条与上一条配对：场景专属规则只作用于自己的场景，而 `common` 规则
    在每个场景都会参与——通用性来自数据（`scene_code='common'` 的那一行），
    不是代码里的 `if`。
    """
    await install_rules(
        rule_doc("RCOMMON001", leaf("user_risk_tag_cnt", "gte", 2), score=20,
                 scene_code="common", name="多风险标签"),
    )
    for event_type in ("login", "coupon_receive", "order_create", "order_pay",
                       "after_sale_apply"):
        outcome = await decide(make_event(event_type=event_type),
                               {"user_risk_tag_cnt": 3}, dry_run=True)
        assert [h["rule_code"] for h in outcome.block["hits"]] == ["RCOMMON001"], (
            f"{event_type} 场景没有取到通用规则"
        )


async def test_disabled_rules_never_participate():
    """BR-05-09：`status != enabled` 的规则不参与求值。

    分值刻意给 100 且条件"一碰就中"：状态过滤一旦失效，每个事件都会变成 reject。
    """
    await install_rules(
        rule_doc("RLOGIN901", leaf("login_cnt_1h", "gte", 1), score=100,
                 scene_code="login", status="disabled"),
        rule_doc("RLOGIN001", leaf("login_cnt_1h", "gte", 1), score=40,
                 scene_code="login", priority=20),
    )
    outcome = await decide(make_event(), {"login_cnt_1h": 9}, dry_run=True)
    assert [h["rule_code"] for h in outcome.block["hits"]] == ["RLOGIN001"]
    assert outcome.block["rule_score"] == 40
    assert "RLOGIN901" not in outcome.block["rule_versions"]


# ============================================================
# BR-05-19 ~ 05-21：落库（E03 / E04）
# ============================================================
async def test_decision_and_hits_are_persisted_with_redundant_snapshots():
    """BR-05-19/20/21：`decisions` + `decision_hits` 落库，且明细带**冗余快照**。

    `decision_hits` 必须冗余存 `rule_name` 与 `score`，不能只存 `rule_code`：
    否则规则改名/改分之后，历史决策的解释会跟着变——那等于篡改了历史证据。
    """
    await install_rules(
        rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40,
                 scene_code="login", name="同设备聚集登录", version=7),
    )
    outcome = await decide(make_event(user_id="U_PERSIST"),
                           {"device_user_cnt": 12})
    assert outcome.decision_id, "非 dry_run 必须取到决策编号"
    assert await flush_decisions() is True

    doc = await db.get_db()[COLL_DECISIONS].find_one({"_id": outcome.decision_id})
    assert doc is not None
    assert doc["event_id"] == outcome.event_id
    # 40 分 → 低档 → pass（分档口径见 BR-05-17）
    assert doc["decision"] == "pass" and doc["risk_level"] == "low"
    assert doc["rule_score"] == 40 and doc["final_score"] == 40
    assert doc["model_score"] is None, "AD-09：模型分恒为 null"
    assert doc["engine_version"] == ENGINE_VERSION
    assert doc["rule_versions"] == {"RDEV": 7}, "BR-05-20：版本快照必须落库"
    assert doc["hit_rule_count"] == 1
    assert doc["degraded"] is False
    assert isinstance(doc["decided_at"], int) and doc["decided_at"] > 0
    # 名单未命中时也是**对象**（契约裁定：任何情况下都不是 bool）
    assert doc["list_hit"] == {"hit": False, "list_type": None,
                               "entity_type": None, "entity_value": None}

    hits = await db.get_db()[COLL_DECISION_HITS].find(
        {"decision_id": outcome.decision_id}
    ).to_list(length=10)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["rule_code"] == "RDEV"
    assert hit["rule_name"] == "同设备聚集登录", "BR-05-21：冗余规则名快照"
    assert hit["score"] == 40, "BR-05-21：冗余分值快照"
    assert hit["rule_version"] == 7
    assert hit["event_id"] == outcome.event_id
    assert hit["matched_facts"] == {"device_user_cnt": 12}
    assert isinstance(hit["hit_at"], int)


async def test_dry_run_writes_nothing():
    """Spec §3.1：`dry_run=true` 时**不写** `decisions` / `decision_hits`。

    不是可选优化：仿真页会对同一批事件反复求值，每次都落库会污染 07 的案件
    列表与 11 的统计，演示变得不可复现（与决策 D11 同一考虑）。
    """
    await install_rules(
        rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40, scene_code="login"),
    )
    before = await db.get_db()[COLL_DECISIONS].count_documents({})
    outcome = await decide(make_event(), {"device_user_cnt": 12}, dry_run=True)
    assert await flush_decisions() is True
    assert outcome.decision_id is None
    assert outcome.block["rule_score"] == 40, "不落库不影响结论（40 分照常算出）"
    assert await db.get_db()[COLL_DECISIONS].count_documents({}) == before
    assert await db.get_db()[COLL_DECISION_HITS].count_documents({}) == 0


async def test_decision_block_has_exactly_the_frozen_contract_fields():
    """契约冻结：决策块就是 `protocols.py` 注释里的那几项，**多一个少一个都不行**。

    这里同时钉住 `snapshot_id` 的**缺席**：03 的 `evaluate(event, features)` 签名
    里没有快照编号，因此 05 不知道时**不写这个键**，由 03 的 `normalize_decision`
    用真实编号补上。写 `None` 会让 `setdefault` 失效，响应里的 `snapshot_id`
    会永久为 null——那比少一个键糟得多。
    """
    expected = {
        "list_hit", "rule_score", "model_score", "final_score", "risk_level",
        "decision", "hit_rule_count", "hits", "rule_versions", "engine_version",
        "elapsed_ms",
    }
    outcome = await decide(make_event(), {}, dry_run=True)
    assert set(outcome.block) == expected

    with_snapshot = await decide(make_event(), {}, snapshot_id="SNP000000000001",
                                 dry_run=True)
    assert set(with_snapshot.block) == expected | {"snapshot_id"}
    assert with_snapshot.block["snapshot_id"] == "SNP000000000001"


async def test_snapshot_id_from_the_event_is_used_and_persisted():
    """03 把 04 的快照挂在事件上时，`snapshot_id` 必须跟着落进 E03。

    E03 的 `snapshot_id` 是**必填列**（"关联特征快照"）。05 的调用契约里没有这个
    参数，若 03 不带过来、05 也不取，E03 里就会写 null —— "这条决策当时看的是
    哪份特征"从此断链，07 的复核页拿不到证据。

    ⚠️ 这个缺陷**只看接口响应发现不了**：`normalize_decision` 的 `setdefault`
    会用真实编号补上响应字段，所以 HTTP 面看起来完全正常，只有库里是 null。
    因此这里断言的是**落库文档**。
    """
    event = make_event(user_id="U_SNAP")
    event["feature_snapshot"] = {"snapshot_id": "SNP20240101000000000042"}
    outcome = await decide(event, {})
    assert outcome.block["snapshot_id"] == "SNP20240101000000000042"

    assert await flush_decisions() is True
    doc = await db.get_db()[COLL_DECISIONS].find_one({"_id": outcome.decision_id})
    assert doc is not None
    assert doc["snapshot_id"] == "SNP20240101000000000042", (
        "E03 的快照关联断了：07 的复核页将拿不到'当时看到的特征'"
    )


# ============================================================
# V-05-09：决策可重放（rule_versions 的作用）
# ============================================================
async def test_v05_09_rule_versions_detect_and_enable_replay():
    """V-05-09 / BR-05-20：用 `decisions.rule_versions` 重算，分数一致。

    ## 这条契约的**准确**边界（如实登记）

    E03 只存 `{rule_code: version}`，**不存规则正文**。因此"重放"的完整语义是：

    1. 能**发现**规则集在决策之后被改过（版本快照与当前库不一致）——这是
       审计上最有价值的一半，因为它标出了"哪些历史决策的解释可能已经失真"；
    2. 在规则正文可得的条件下（回滚到当时的版本、或从版本历史取回），
       用当时的特征重算能复现同一分数。

    这里两条都验：先改规则（分值 40→90、version 1→2），确认版本快照能识别出
    变化且分数确实变了；再把规则正文恢复成当时的样子，确认分数与版本快照
    都回到原值。
    """
    original = rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40,
                        scene_code="login", version=1)
    await install_rules(original)
    features = {"device_user_cnt": 12}

    first = await decide(make_event(), features)
    assert await flush_decisions() is True
    recorded = await db.get_db()[COLL_DECISIONS].find_one({"_id": first.decision_id})
    assert recorded["rule_versions"] == {"RDEV": 1}
    assert recorded["rule_score"] == 40

    # —— 规则被改分并升版 ——
    await install_rules(rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=90,
                                 scene_code="login", version=2))
    second = await decide(make_event(), features, dry_run=True)
    assert second.block["rule_score"] == 90, "改分之后分数必须变（否则说明规则没生效）"
    assert second.block["rule_versions"] == {"RDEV": 2}
    assert second.block["rule_versions"] != recorded["rule_versions"], (
        "版本快照必须能识别出规则集已变——这正是它存在的理由"
    )

    # —— 恢复当时的规则正文 → 重放得到同一分数 ——
    await install_rules(original)
    replay = await decide(make_event(), features, dry_run=True)
    assert replay.block["rule_versions"] == recorded["rule_versions"]
    assert replay.block["rule_score"] == recorded["rule_score"], "重放分数必须一致"


# ============================================================
# V-05-10：规则改名后历史命中明细不失真
# ============================================================
async def test_v05_10_renaming_a_rule_does_not_rewrite_history():
    """V-05-10 / BR-05-21：改规则名后，**旧决策**的命中明细仍是旧名。

    这是"冗余快照"存在的全部理由：若明细只存 `rule_code`、展示时联查 `rules`，
    规则改名之后历史案件里的证据会跟着变——等于篡改了历史。
    """
    await install_rules(rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40,
                                 scene_code="login", name="旧名字", version=1))
    old = await decide(make_event(user_id="U_OLD"), {"device_user_cnt": 12})
    assert await flush_decisions() is True

    # 改名 + 升版（06 的正常操作）
    await install_rules(rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40,
                                 scene_code="login", name="新名字", version=2))
    rows = await db.get_db()[COLL_DECISION_HITS].find(
        {"decision_id": old.decision_id}
    ).to_list(length=10)
    assert rows[0]["rule_name"] == "旧名字", (
        "历史命中明细被规则改名改写了——冗余快照失效"
    )

    # 新决策则用新名字（说明改名确实生效了，上一条不是因为改名失败）
    new = await decide(make_event(user_id="U_NEW"), {"device_user_cnt": 12})
    assert await flush_decisions() is True
    rows = await db.get_db()[COLL_DECISION_HITS].find(
        {"decision_id": new.decision_id}
    ).to_list(length=10)
    assert rows[0]["rule_name"] == "新名字"


# ============================================================
# V-05-11：reason 含实际特征值
# ============================================================
async def test_v05_11_reason_contains_actual_feature_values():
    """V-05-11 / BR-05-22：`reason` 必须含**具体数值**，供人工复核直接看证据。"""
    await install_rules(
        rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40,
                 scene_code="login", name="同设备聚集登录"),
        rule_doc("RIP", leaf("ip_is_proxy", "eq", True), score=20,
                 scene_code="login", name="代理IP登录", priority=20),
    )
    outcome = await decide(make_event(), {"device_user_cnt": 12, "ip_is_proxy": True},
                           dry_run=True)
    hits = {h["rule_code"]: h for h in outcome.block["hits"]}
    assert "12" in hits["RDEV"]["reason"], hits["RDEV"]["reason"]
    assert "5" in hits["RDEV"]["reason"], "阈值也要写出来（否则无从判断差多少）"
    assert "true" in hits["RIP"]["reason"], hits["RIP"]["reason"]
    assert hits["RDEV"]["matched_facts"] == {"device_user_cnt": 12}


async def test_reason_is_persisted_with_the_same_wording():
    """落库的 `reason` 与响应里的**逐字一致**（避免两处各生成一遍而慢慢分叉）。

    页面展示的是**库里那份**（07 读 `decision_hits`），而响应是 03 当场透传的
    那份。两处若各自生成一次文案，迟早出现"页面上与接口返回的对不上"。
    """
    await install_rules(rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40,
                                 scene_code="login"))
    outcome = await decide(make_event(), {"device_user_cnt": 12})
    assert await flush_decisions() is True
    rows = await db.get_db()[COLL_DECISION_HITS].find(
        {"decision_id": outcome.decision_id}
    ).to_list(length=10)
    assert rows[0]["reason"] == outcome.block["hits"][0]["reason"]
    assert "12" in rows[0]["reason"]


# ============================================================
# V-05-12：性能（**有界规模**，如实声明）
# ============================================================
async def test_v05_12_bounded_latency_measurement():
    """V-05-12：P95 决策延迟 < 50ms —— **有界规模**测量，并如实声明未做 1000 条压测。

    ## 规模与口径（不夸大）

    - 样本 **200 次** `decide()`（`dry_run=True`：不含落库，测的是判定本身）；
    - 另测 **20 次** `dry_run=False`（含取决策编号的一次 Mongo 往返），因为
      真实高频路径是这一条；
    - P95 取**最近秩**（`ceil(0.95n)-1`），不做插值——插值会给出一个没有
      任何一次真实请求达到过的数字。

    **未做**：Spec §7 要求的"灌 1000 条事件压测"。本机单进程 + 本地 Mongo，
    1000 条串行会跑掉几分钟并显著拉长门禁时间；这里如实声明**未做该规模**，
    而不是拿一个小样本声称"已达标"。50ms 预算针对的是**纯判定**（名单命中缓存、
    规则已取到），因此上面的 200 次样本是它的合理量级。
    """
    await install_rules(
        rule_doc("RDEV", leaf("device_user_cnt", "gte", 5), score=40, scene_code="login"),
        rule_doc("RIP", leaf("ip_is_proxy", "eq", True), score=20, scene_code="login",
                 priority=20),
        rule_doc("RAGE", leaf("user_age_days", "lte", 7), score=10, scene_code="login",
                 priority=30),
    )
    features = {"device_user_cnt": 12, "ip_is_proxy": True, "user_age_days": 3}
    event = make_event(user_id="U_PERF")

    await decide(event, features, dry_run=True)     # 预热（首次会建缓存与连接）

    samples: list[float] = []
    for _ in range(200):
        started = time.perf_counter()
        outcome = await decide(event, features, dry_run=True)
        samples.append((time.perf_counter() - started) * 1000.0)
    assert outcome.block["elapsed_ms"] >= 0, "含耗时的决策块必须记录 elapsed_ms"

    samples.sort()
    p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
    assert p95 < 50.0, f"P95={p95:.2f}ms 超过 50ms 预算（样本 {len(samples)}）"

    # 真实路径（含取决策编号）也不能离预算太远
    heavy: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        await decide(event, features)
        heavy.append((time.perf_counter() - started) * 1000.0)
    await flush_decisions()
    heavy.sort()
    assert heavy[-1] < 200.0, f"最慢一次 {heavy[-1]:.2f}ms 超过了 200ms 的同步预算"
    assert STATS["decided"] >= 220


# ============================================================
# V-05-13 的**链路级**证据在 `test_engine_api.py`
# ============================================================
# 那份用例比较的是两条真实入口（03 调的 `RuleDecisionProvider` 与
# `POST /engine/evaluate`）对**同一份特征**给出的决策块，需要 HTTP 客户端与
# 真实 04 的快照，因此放在接口测试里。这里只需要钉住"两条入口共用同一个
# `decide()`"这条结构事实。
async def test_both_entry_points_call_the_single_orchestrator():
    """结构事实：03 的 provider 与本模块的接口都只经过 `decision.decide()`。

    这是 V-05-13「仿真链路复用同一引擎」在**代码结构**上的保证：只要没有第二份
    判定实现，两条链路就不可能给出不同的结论。Spec §3.1 要求模块 10 必须调用
    `/engine/evaluate`、禁止另写判定逻辑，正是为了维持这一点。
    """
    import inspect

    from app.engine import decision_provider
    from app.api import engine_api

    for module, target in ((decision_provider, "decide"), (engine_api, "decide")):
        source = inspect.getsource(module)
        assert f"{target}(" in source, f"{module.__name__} 没有调用 {target}()"
    # provider 的实现只做转交，不含任何判定逻辑
    provider_source = inspect.getsource(decision_provider.RuleDecisionProvider.evaluate)
    assert "decision_engine.decide" in provider_source
