# -*- coding: utf-8 -*-
"""依赖故障时的 fail-closed：**降级也必须落库建案**（RUL-5001 / RUL-5002 / D5）。

## 这个文件的中心命题（决策 D5）

fail-closed 的意义是"不确定就交给人工"。若降级只体现在响应里、不写
`decisions`，那么模块 08 看不到案件、07 的列表里没有这一条、11 的统计里也没有
——这些请求将**无人处理**，等于变相丢弃。而这恰恰是 fail-closed 最不该出现的
结果：我们明明识别出了"这里不确定"，却让它静默消失。

因此这里每一条用例都是**两段式断言**：

1. **决策面**：结论必须是 `review` + `degraded=true`，**绝不 `pass`**（V-05-08）；
2. **落库面**：`decisions` 里必须真的多出一条 `decision=review`、`degraded=true`
   的记录（D5），否则 08 无从建案。

第 2 条是"响应看起来对、实际上请求丢了"的唯一判别手段——只断言响应的话，
一个不落库的实现同样能全绿。

## 与 03 的边界（已知缺口 N-03-3，**本模块不补**）

03 在 `stage="feature"` 的短路（决策 D44：快照不完整、04 不可用、超时）
**不写** `decisions`——那时 05 根本没被调用，"没写"是 03 自己的缺口。
本文件只覆盖"**05 被调用且判定了降级**"这一种情形，绝不替 03 补那一条。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import COLL_DECISIONS, COLL_DECISION_HITS
from app.engine.decision import decide
from app.errors import RUL_NOTICE

from tests.engine_testlib import (
    ExplodingListRepo,
    ExplodingRuleRepo,
    flush_decisions,
    install_rules,
    leaf,
    make_event,
    rule_doc,
)

pytestmark = pytest.mark.anyio


async def test_list_dependency_failure_degrades_to_review_and_persists():
    """V-05-08 + D5：名单依赖不可用 → `review` + `degraded=true`，**且落库**。

    断言 `!= "pass"` 而不是 `== "review"` 是不够的（`reject` 也不等于 pass），
    因此两条都写。真正关键的是第二条：这条降级决策必须在 `decisions` 里，
    08 才有案子可建。
    """
    outcome = await decide(
        make_event(user_id="U_DEGRADE_1"), {}, list_repo=ExplodingListRepo()
    )

    # —— 决策面 ——
    assert outcome.degraded is True
    assert outcome.degrade_code == "RUL-5001"
    assert outcome.block["decision"] == "review"
    assert outcome.block["decision"] != "pass", "fail-closed 绝不返回 pass"
    assert outcome.block["rule_score"] == 0 and outcome.block["hits"] == []
    assert outcome.block["engine_version"] == "rule-engine-v1"
    assert outcome.decision is not None

    # —— 落库面（D5：降级不落库 = 请求无人处理） ——
    assert await flush_decisions() is True
    doc = await db.get_db()[COLL_DECISIONS].find_one({"_id": outcome.decision_id})
    assert doc is not None, "D5：降级也必须写入一条 decisions"
    assert doc["decision"] == "review"
    assert doc["degraded"] is True
    assert doc["degrade_code"] == "RUL-5001"
    assert doc["degrade_reason"], "降级原因必须落库，否则人工复核无从下手"
    assert doc["hit_rule_count"] == 0
    assert doc["event_id"] == outcome.event_id
    # 命中明细则一条都不该有（没有规则参与）
    assert await db.get_db()[COLL_DECISION_HITS].count_documents(
        {"decision_id": outcome.decision_id}
    ) == 0


async def test_rule_set_failure_degrades_to_review_and_persists():
    """RUL-5002：`rules` 读不出来 → 同样是 fail-closed 的降级并落库。

    "读不到规则"**不是**"没有规则命中"：前者是"不知道"，后者是"结论"。
    两者在数据上都表现为零条规则，若返回 `pass`，一次 Mongo 抖动就等于把
    全部规则停用。
    """
    repo = ExplodingRuleRepo()
    outcome = await decide(make_event(event_type="login"), {"login_cnt_1h": 1},
                           rule_repo=repo)

    assert repo.calls == 1, "必须真的去读了规则集（而不是跳过它直接判 pass）"
    assert outcome.degraded is True
    assert outcome.degrade_code == "RUL-5002"
    assert outcome.block["decision"] == "review"
    assert outcome.block["decision"] != "pass"

    assert await flush_decisions() is True
    doc = await db.get_db()[COLL_DECISIONS].find_one({"_id": outcome.decision_id})
    assert doc is not None and doc["decision"] == "review" and doc["degraded"] is True
    assert doc["degrade_code"] == "RUL-5002"


async def test_degraded_decision_is_not_marked_as_a_real_conclusion():
    """降级块必须**可辨识**：`degraded=true` 且 `risk_level=high`。

    "05 判了 review"与"05 的依赖挂了、只能转人工"是两件不同的事，处置与追责
    也不同。`engine_version` 保持 `rule-engine-v1`（块确实由本引擎产出），
    区分靠 `degraded` 这一位——E03 就是为此专门加了它（D5）。

    `risk_level` 取 `high` 而不是 `low`：0 分在这里的含义是"没算出来"，
    用 high 才能让 08 建案与 07 的列表按高风险优先处理。
    """
    outcome = await decide(make_event(), {}, list_repo=ExplodingListRepo())
    assert outcome.block["risk_level"] == "high"
    assert outcome.block["final_score"] == 0
    assert outcome.block["engine_version"] == "rule-engine-v1"
    assert outcome.degraded is True


async def test_degraded_warning_is_reported_to_the_caller():
    """降级原因必须能被调用方看到（`warnings`），否则页面只能显示一个"人审"。"""
    outcome = await decide(make_event(), {}, list_repo=ExplodingListRepo())
    codes = {w["code"] for w in outcome.warnings}
    assert "RUL-5001" in codes
    assert any("名单" in w["message"] for w in outcome.warnings)


async def test_feature_shortage_fails_closed_instead_of_scoring_zero():
    """特征不完整时**不能**照常求值：那会让缺失条件恒假 → 分数偏低 → 很可能 pass。

    04 只把算得出来的特征放进 `features`（算不出来的进 `missing_features`）。
    `decide()` 在**特征侧**的入口（`/engine/evaluate` 那条路）必须据此 fail-closed
    ——"因为不知道所以放行"与风控目标正好相反。

    这里用"空特征"模拟：它等价于"所有字段都缺失"，若照常求值会得到 0 分 → pass。
    """
    class EmptyFeatures:
        available = True

        async def compute(self, event):
            return {"snapshot_id": None, "features": {}, "degrade_suggested": True,
                    "degrade_reasons": ["关键特征不可用：ip_is_proxy"]}

    outcome = await decide(make_event(), None, feature_provider=EmptyFeatures())

    assert outcome.block["decision"] == "review", "缺特征必须转人工，绝不能 pass"
    assert outcome.degraded is True
    assert outcome.block["rule_score"] == 0
    assert any("特征" in w["message"] for w in outcome.warnings)

    assert await flush_decisions() is True
    doc = await db.get_db()[COLL_DECISIONS].find_one({"_id": outcome.decision_id})
    assert doc is not None and doc["degraded"] is True, (
        "特征缺口同样要落库建案（否则这些请求也无人处理）"
    )


async def test_invalid_condition_tree_isolates_to_that_rule():
    """V-05-07 / BR-05-13：一条非法条件树**不影响其余规则**，且产生告警。

    为什么不整次失败：一条坏配置（06 存了一棵引用不存在算子的树）不该让**所有**
    事件的决策消失——那会把"一条规则写错"升级成"全站规则失效"，而真正的问题
    只是那一行数据。
    """
    await install_rules(
        rule_doc("RBROKEN", {"field": "login_cnt_1h", "op": "between", "value": 5},
                 score=60, scene_code="login"),
        rule_doc("RGOOD1", leaf("login_cnt_1h", "gte", 3), score=40, scene_code="login",
                 priority=20),
        rule_doc("RGOOD2", leaf("device_user_cnt", "gte", 2), score=35, scene_code="login",
                 priority=30),
    )
    outcome = await decide(make_event(event_type="login"),
                           {"login_cnt_1h": 5, "device_user_cnt": 3})

    assert outcome.block["rule_score"] == 75, "两条正常规则必须照常累加（40+35）"
    assert outcome.block["hit_rule_count"] == 2
    assert outcome.block["decision"] == "review"
    codes = [h["rule_code"] for h in outcome.block["hits"]]
    assert codes == ["RGOOD1", "RGOOD2"]
    assert "RBROKEN" not in codes
    # 告警（RUL-5003 的 200 面）
    warned = [w for w in outcome.warnings if w["code"] == "RUL-5003"]
    assert len(warned) == 1
    assert "RBROKEN" in warned[0]["message"]
    assert RUL_NOTICE["RUL-5003"][:6] in warned[0]["message"]


async def test_failed_rule_is_recorded_in_trace_when_requested():
    """`trace=true` 时失败规则必须留下 `evaluated=false` 的痕迹（可排障）。"""
    await install_rules(
        rule_doc("RBROKEN", leaf("login_cnt_1h", "gte"), score=50, scene_code="login"),
        rule_doc("ROK", leaf("login_cnt_1h", "gte", 1), score=10, scene_code="login",
                 priority=20),
    )
    outcome = await decide(make_event(event_type="login"), {"login_cnt_1h": 1}, trace=True)
    trace = {row["rule_code"]: row for row in (outcome.trace or [])}
    assert trace["RBROKEN"]["evaluated"] is False
    assert trace["RBROKEN"]["matched"] is False
    assert "求值失败" in trace["RBROKEN"]["reason"]
    assert trace["ROK"]["evaluated"] is True and trace["ROK"]["matched"] is True
    assert outcome.block["rule_score"] == 10
