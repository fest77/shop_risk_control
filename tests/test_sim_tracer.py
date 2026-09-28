# -*- coding: utf-8 -*-
"""模块 10 的**纯逻辑**单测：链路追踪器与批量扰动（Spec §6 的"可单测"两格）。

Spec §6 的文件规划把 `engine/tracer.py` 与 `services/sim_service.py` 标成
"✔ 可单测"。本文件就是那两格——它**不依赖 Mongo、不依赖事件循环**，
因此可以在毫秒级跑完，也让"步骤记录/耗时统计"和"扰动可复现"这两条
与 HTTP、数据库无关的规则有一个**独立的**守卫（`test_sim_api.py` 里的
端到端用例失败时，先看这里能立刻分清"是逻辑错还是链路错"）。

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_sim_tracer.py -q
"""
from __future__ import annotations

import pytest

from app.engine.tracer import (
    NOT_EXECUTED_DETAIL,
    ORCHESTRATION_WARN_RATIO,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    SimTracer,
)
from app.schemas.sim_schema import STEP_LABELS, STEP_NAMES, parse_scene_extra
from app.services.sim_service import (
    WINDOW_PREMISE,
    WINDOW_PREMISE_AFFECTED,
    ripple_event,
    rule_block,
)

#: 本文件的用例都是**纯函数**（没有一行 `await`），但必须写成 `async def`。
#:
#: ## 为什么（一次真实的收集期踩坑：18 条用例全灭）
#:
#: `tests/conftest.py` 有一个 **async autouse 夹具** `prep_db`（探测 Mongo、
#: 切测试库、建索引、清数据）。它对本目录下**每一个**用例都生效，而
#: pytest **原生不支持 async 夹具**，必须由 anyio 的钩子包装。
#:
#: 而 anyio 的钩子**只包装 async 用例**：写成同步 `def test_xxx()` 时 anyio 不
#: 接管，那个 `prep_db` 就成了"没人处理的 async 夹具"，报错是
#: `requested an async fixture 'prep_db' ... with no plugin or hook that handled
#: it`——**报错信息完全没有指向真正的原因**（看起来像 anyio 坏了）。
#:
#: 同理 `pytest.mark.anyio(False)` 也不是可用的"取消标记"写法：`pytestmark` 是
#: 打在模块上的标记，它只会**叠加第二个** anyio 标记，而 anyio 只看"有没有这个
#: 标记"，于是同步用例照样进不了它的包装流程。
#:
#: 因此本文件与其余 20 多个测试文件保持同一写法。函数体里没有 `await` 无妨——
#: 它换来的是"与其它文件完全一致的夹具语义"。
pytestmark = pytest.mark.anyio


# ============================================================
# engine/tracer.py
# ============================================================
async def test_tracer_records_steps_in_order_with_contract_shape():
    """BR-10-10：每步独立记录五个属性，且 `to_dict` 的键就是 Spec §3.3 的那七个。"""
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    tracer.start(1)
    tracer.finish(1, "通过", payload={"a": 1})
    tracer.start(2)
    tracer.finish(2, "完成", status=STATUS_FAILED)

    steps = tracer.steps()
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0]["name"] == "event_validate"
    assert steps[0]["label"] == "事件校验"
    assert steps[0]["status"] == STATUS_OK
    assert steps[0]["detail"] == "通过"
    assert steps[0]["payload"] == {"a": 1}
    assert set(steps[0]) == {"seq", "name", "label", "status", "detail",
                            "elapsed_ms", "payload"}
    assert steps[1]["status"] == STATUS_FAILED


async def test_tracer_does_not_precreate_steps():
    """BR-10-14 的后端前提：五步**按顺序**产生，不预建。

    若一次性把五步都塞进去，"哪些真的跑了"就看不出来了；前端也无法实现
    "按顺序逐个点亮"（它必须能区分"还没到"与"已跳过"）。
    """
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    assert tracer.steps() == []
    tracer.start(1)
    tracer.finish(1, "ok")
    assert [s["name"] for s in tracer.steps()] == ["event_validate"]


async def test_tracer_mark_not_executed_fills_the_rest():
    """BR-10-15：失败步骤之后全部标成"未执行"（`skipped`）。"""
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    tracer.start(1)
    tracer.fail(1, "事件参数不合法")
    tracer.mark_not_executed(1)

    steps = tracer.steps()
    assert [s["name"] for s in steps] == list(STEP_NAMES)
    assert steps[0]["status"] == STATUS_FAILED
    for step in steps[1:]:
        assert step["status"] == STATUS_SKIPPED
        assert step["detail"] == NOT_EXECUTED_DETAIL
        assert step["elapsed_ms"] == 0


async def test_tracer_mark_not_executed_does_not_overwrite_reasons():
    """`mark_not_executed` **不覆盖**已有的记录。

    名单直通时步骤 4 会被显式记成 `skipped` 并带原因（"名单直通，未求值规则"）。
    后面的步骤若再调一次 `mark_not_executed`，不能把那条**有效信息**改写成
    一句笼统的"前序未通过"——两者的原因不同，抹平就是丢失证据。
    """
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    tracer.start(1)
    tracer.finish(1, "ok")
    tracer.start(3)
    tracer.skip(3, "未执行（名单直通）")
    tracer.mark_not_executed(1)

    step = tracer.step(3)
    assert step.status == STATUS_SKIPPED
    assert step.detail == "未执行（名单直通）", "已有原因不得被覆盖"


async def test_tracer_orchestration_ms_is_never_negative():
    """BR-10-12：编排开销 = 总耗时 − 各步之和，**可以为 0 但绝不为负**。

    为负说明"各步之和 > 总耗时"，那是计时口径错（例如某一步用了墙上时钟而
    系统时间被回拨）。这条断言把那个错误挡在渲染之前——负数耗时会被前端
    显示成"其余为编排开销 -3ms"，没人能解释。
    """
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    for seq in range(1, 6):
        tracer.start(seq)
        tracer.finish(seq, "ok", elapsed_ms=10)
    assert tracer.step_sum_ms == 50
    assert tracer.orchestration_ms(40) == 0, "总耗时小于各步之和时必须夹到 0"
    assert tracer.orchestration_ms(100) == 50

    summary = tracer.summary(100)
    assert summary["steps_sum_ms"] == 50
    assert summary["orchestration_ms"] == 50
    assert summary["orchestration_ratio"] == 0.5
    assert summary["over_threshold"] is False, "0.5 不算超过 0.5（严格大于）"
    assert summary["threshold_ratio"] == ORCHESTRATION_WARN_RATIO

    over = tracer.summary(200)
    assert over["orchestration_ratio"] == 0.75
    assert over["over_threshold"] is True


async def test_tracer_summary_handles_zero_total():
    """总耗时为 0 时不除零（BR-10-12 的边界）。"""
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    summary = tracer.summary(0)
    assert summary["orchestration_ms"] == 0
    assert summary["orchestration_ratio"] == 0.0
    assert summary["over_threshold"] is False


async def test_tracer_elapsed_since_reads_started_step():
    """`elapsed_since`：`finish` 之前取"已过去多久"，`finish` 之后取落定值。

    它是步骤 1 结论行「通过 · 耗时 1ms」的唯一数据来源——`finish` 的 `detail`
    是它的入参，因此那个数字必须在 `finish` **之前**就能读到。
    """
    tracer = SimTracer(STEP_NAMES, STEP_LABELS)
    assert tracer.elapsed_since(1) == 0, "没 start 过的步骤是 0"
    tracer.start(1)
    assert tracer.elapsed_since(1) >= 0
    tracer.finish(1, "ok", elapsed_ms=7)
    assert tracer.elapsed_since(1) == 7


# ============================================================
# services/sim_service.ripple_event（BR-10-18 的可复现基础）
# ============================================================
async def test_ripple_event_is_deterministic_and_bounded():
    """同 seed 完全一致、幅度有界、不动非金额字段。"""
    event = {
        "event_type": "order_pay", "user_id": "U-1", "amount": 10000,
        "scene_extra": {"order_no": "SO-1", "pay_channel": "alipay",
                        "pay_amount": 10000},
    }
    a = ripple_event(event, 42)
    b = ripple_event(event, 42)
    assert a == b, "同一 seed 必须产生完全相同的事件"
    assert a["event_type"] == event["event_type"]
    assert a["user_id"] == event["user_id"]
    assert a["scene_extra"]["order_no"] == "SO-1"
    assert a["scene_extra"]["pay_amount"] == a["amount"], (
        "顶层 amount 与 scene_extra.pay_amount 必须同步扰动，"
        "否则会触发 EVT-4007（双口径金额矛盾）")
    assert 8000 <= a["amount"] <= 12000, a["amount"]
    # 原事件**不被就地修改**：就地改会让同一模板在下一轮带上上一轮的扰动，
    # 于是"同 seed 结果一致"就依赖于调用顺序
    assert event["amount"] == 10000
    assert event["scene_extra"]["pay_amount"] == 10000


async def test_ripple_event_does_not_use_global_random():
    """**不碰全局 `random`**：同 seed 在任意调用顺序下结果一致。

    全局 `random` 会被同进程的其它模块（模拟器、测试夹具）共享，一旦使用它，
    "同一 seed 结果一致"（BR-10-18）就依赖于"期间没有别人抽过随机数"——
    那是个不可能被验证的假设。这里用"先跑别的 seed 再跑目标 seed"来验证。
    """
    event = {"event_type": "order_pay", "user_id": "U-1", "amount": 10000}
    first = ripple_event(event, 7)
    for other in (1, 2, 3, 999, 12345):
        ripple_event(event, other)
    assert ripple_event(event, 7) == first


async def test_ripple_event_leaves_non_amount_events_alone():
    """没有金额的事件原样返回（不为了让统计好看而凭空造假字段）。"""
    event = {"event_type": "login", "user_id": "U-1",
             "scene_extra": {"login_type": "pwd"}}
    assert ripple_event(event, 99) == event


async def test_ripple_event_clamps_to_positive_amount():
    """极小金额扰动后仍是正整数（0 会被 03 的 `EVT-4005` 拒掉）。"""
    for seed in range(50):
        out = ripple_event({"event_type": "order_pay", "user_id": "U-1",
                            "amount": 1}, seed)
        assert isinstance(out["amount"], int) and out["amount"] >= 1, out


async def test_ripple_event_passes_through_non_dict():
    """非 dict 原样返回（那属于 03 的 `EVT-4001`，扰动函数不越界报错）。"""
    assert ripple_event("not-a-dict", 1) == "not-a-dict"
    assert ripple_event(None, 1) is None


# ============================================================
# sim_schema.parse_scene_extra（SIM-4002 的判定）
# ============================================================
async def test_parse_scene_extra_accepts_object_and_json_string():
    """对象原样通过（且返回**同一个对象**，零成本）；字符串解析成对象。"""
    event = {"scene_extra": {"login_type": "pwd"}}
    parsed, reason = parse_scene_extra(event)
    assert reason is None
    assert parsed is event, "已经是对象时不应复制"

    parsed, reason = parse_scene_extra({"scene_extra": '{"login_type": "pwd"}'})
    assert reason is None
    assert parsed["scene_extra"] == {"login_type": "pwd"}


async def test_parse_scene_extra_reports_bad_json_and_non_object():
    """非法 JSON / 合法但非对象的 JSON 都给出可读原因（`SIM-4002`）。"""
    _, reason = parse_scene_extra({"scene_extra": "{bad"})
    assert reason and "不是合法 JSON" in reason

    _, reason = parse_scene_extra({"scene_extra": "[1, 2]"})
    assert reason and "必须是 JSON 对象" in reason

    _, reason = parse_scene_extra({"scene_extra": 123})
    assert reason and "JSON 对象或 JSON 字符串" in reason


async def test_parse_scene_extra_treats_blank_string_as_absent():
    """空串/空白串视为**未提供**（仿真页不填时提交的就是空串）。

    把它当成"非法 JSON"会让每一次不带扩展参数的表单提交都红一次，
    而 `login` 之外的事件类型本来就允许 `scene_extra` 缺失——
    缺的必填键该由 `EVT-4004` 报，那个提示才指得对地方。
    """
    for blank in ("", "   ", "\n"):
        parsed, reason = parse_scene_extra({"scene_extra": blank})
        assert reason is None, blank
        assert "scene_extra" not in parsed, blank


# ============================================================
# sim_service.rule_block（块形状与 05 的冻结契约对齐）
# ============================================================
async def test_rule_block_matches_frozen_contract_fields():
    """`rule_block` 的键集合与 Spec 05 §3.1 的 12 字段一致（含 `model_score=None`）。"""
    block = rule_block(
        list_hit={"hit": False, "list_type": None, "entity_type": None,
                  "entity_value": None},
        score=70, level="medium", decision="review",
        rule_versions={"R1": 1}, engine_version="rule-engine-v1",
        elapsed_ms=6,
        hits=[{"rule_code": "R1", "score": 70}],
        snapshot_id="SNP20260101000000000001",
    )
    assert set(block) == {
        "list_hit", "rule_score", "model_score", "final_score", "risk_level",
        "decision", "hit_rule_count", "hits", "rule_versions", "engine_version",
        "elapsed_ms", "snapshot_id",
    }
    assert block["model_score"] is None, (
        "AD-09：`model_score` 是**结论**'本次没有模型分'，不是'字段没填'")
    assert block["hit_rule_count"] == 1
    assert block["final_score"] == block["rule_score"] == 70


async def test_rule_block_omits_snapshot_id_when_unknown():
    """快照编号未知时**不写这个键**（写 `None` 会让消费方的 setdefault 失效）。"""
    block = rule_block(
        list_hit={}, score=0, level="low", decision="pass",
        rule_versions={}, engine_version="v", elapsed_ms=0,
    )
    assert "snapshot_id" not in block


# ============================================================
# 前提声明文案（BR-10-07）
# ============================================================
async def test_window_premise_texts_are_distinct():
    """两种窗口前提必须是**不同的**文案：用户有权知道这次有没有动真实窗口。"""
    assert WINDOW_PREMISE != WINDOW_PREMISE_AFFECTED
    assert "未写入本次事件" in WINDOW_PREMISE
    assert "affect_window=true" in WINDOW_PREMISE_AFFECTED
