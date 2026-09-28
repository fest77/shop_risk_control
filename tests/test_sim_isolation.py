# -*- coding: utf-8 -*-
"""模块 10「数据隔离」验收测试（Spec §4.2 / BR-10-05 ~ BR-10-09）。

**任务书 §2 把这条列为"重点，必须给实测数字"**，原话是：
「跑一次仿真前后，**真实**的特征窗口计数与 **E15 指标桶不得变化**；
`dry_run` 只挡住 `decisions`/`decision_hits`，**窗口写入你要自己解决**。
**不测就等于没做。**」

因此本文件的每个用例都是"**仿真前后各读一次真实数据**"，然后比对：

    快照 A（真实数据）→ 跑仿真 N 次 → 快照 B（真实数据）→ assert A == B

快照覆盖六个集合与 04 窗口的全部计数（见 `tests/sim_testlib.snapshot_real_state`）：
`risk_events` / `feature_snapshots` / `decisions` / `decision_hits` / `risk_cases` /
**`metric_buckets`（E15，D66）** + 04 的 `total_entries`/`distinct_keys`/`seen_ids`/
四个计数器。

## 为什么"比条数"不够

仿真若用 `replace_one` 覆盖了一条既有记录，条数不变而内容变了。
因此快照里还带一份按 `_id` 排序的**内容指纹**——`diff_real_state` 会把
"哪一条新增/移除"打出来。这是"不测就等于没做"的下一层：
**只测数量等于只做了一半**。

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_sim_isolation.py -q
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import (
    COLL_DECISIONS,
    COLL_DECISION_HITS,
    COLL_METRIC_BUCKETS,
    COLL_RISK_CASES,
    COLL_RISK_EVENTS,
    COLL_SEQ_COUNTERS,
    COLL_SIM_CASES,
    COLL_SIM_RUNS,
)
from app.engine.decision import flush as flush_decisions
from app.services import feature_service
from app.utils.timeutil import now_ms
from tests.conftest import READER, WRITER
from tests.engine_testlib import (
    branch,
    install_rule_scenes,
    install_rules,
    leaf,
    reset_engine_state,
    rule_doc,
)
from tests.event_testlib import component_reset, login_payload, reset_runtime_state
from tests.sim_testlib import (
    REAL_STATE_COLLECTIONS,
    diff_real_state,
    format_real_state,
    install_sim_cases,
    snapshot_real_state,
    unique_event_id,
)

pytestmark = pytest.mark.anyio

SIM_RUN_URL = "/api/v1/sim/run"
SIM_BATCH_URL = "/api/v1/sim/batch"
EVENTS_URL = "/api/v1/events"


@pytest.fixture(autouse=True)
async def iso_state():
    """逐用例复位（理由见 `test_sim_consistency.py` 的同名夹具）。"""
    component_reset()
    reset_runtime_state()
    reset_engine_state()
    service = feature_service.get_feature_service()
    service.window.reset()
    service.stats.update({
        "computed": 0, "ingested": 0, "duplicate": 0, "errors": 0,
        "degraded": 0, "persisted": 0, "persist_failed": 0, "requeued": 0,
        "read_only": 0, "window_affected": 0, "skipped_persist": 0,
    })
    # 清空序列计数器与 E17/E18（理由见 `test_sim_consistency.py` 的同名夹具：
    # 逐用例复位的编号必须与逐用例复位的集合成对出现，否则跨用例重号）
    await db.get_db()[COLL_SEQ_COUNTERS].delete_many({})
    await db.get_db()[COLL_SIM_RUNS].delete_many({})
    await db.get_db()[COLL_SIM_CASES].delete_many({})
    yield
    await service.flush()
    await flush_decisions()
    service.window.reset()
    reset_engine_state()
    component_reset()
    await db.get_db()[COLL_SIM_RUNS].delete_many({})


@pytest.fixture
async def sim_inputs():
    """灌入规则（只用持久化画像特征，让"窗口差异"不影响结论）+ 4 条演示用例。"""
    await install_rule_scenes("login", "coupon", "order", "pay", "aftersale", "common")
    await install_rules(rule_doc(
        # 60 分 = `arbiter` 的 review 下界（BR-05-16）。用 60 而不是 45 是刻意的：
        # 本条用例要证明的正是"**会触发建案钩子**的那一档决策没有被仿真写进
        # `risk_cases`"，因此决策必须真的落在 review/reject 上——
        # 45 分只会得到 pass，那样"没建案"就无法证明任何事。
        "RLOGIN001", branch("and", leaf("device_user_cnt", "gte", 5)),
        score=60, scene_code="login",
    ))
    return await install_sim_cases()


async def _install_profiles(device_cnt: int = 6, is_proxy: bool = True) -> None:
    """给 04 装画像与 09 的关联账号数（真实 04 会读它们）。"""
    from app.engine.feature_compute import ProfileData
    from tests.feature_testlib import FakeLinkedUserCountProvider, FakeProfileReader

    service = feature_service.get_feature_service()
    service._profile_reader = FakeProfileReader(ProfileData(
        user_register_at=now_ms() - 2 * 86_400_000, user_level="normal", user_risk_tag_cnt=0,
        ip_is_proxy=is_proxy, address_aftersale_cnt=0,
    ))
    service._linked_provider = FakeLinkedUserCountProvider(
        {"device": device_cnt, "ip": device_cnt, "address": device_cnt}
    )


def sim_payload(**over) -> dict:
    payload = login_payload(user_id="U000132", device_id="DMULE0001", ip="203.0.113.88")
    payload["scene_extra"] = {"login_type": "pwd", "success": True}
    payload["event_id"] = unique_event_id()
    payload["ts"] = now_ms() - 1000
    payload.update(over)
    return payload


# ============================================================
# 交付报告用的"实测数字"（每跑一次 pytest 都会打印，`-s` 可见）
# ============================================================
async def test_print_isolation_numbers_for_report(client, sim_inputs, capsys):
    """把**仿真前后各读一次真实数据**的对比数字打印出来（交付报告的证据）。

    ## 为什么做成一条用例，而不是一个外部探针脚本

    本模块最关键的证据就是"跑仿真前后，**真实**的特征窗口与 E15 指标桶有没有变"。
    最自然的想法是写一个独立探针脚本，但**04 的滑窗是进程内状态**
    （悬空点 G-02）——外部脚本读不到服务进程里的那个窗口，只能自己去
    `import app.services.feature_service`，而那读出的是**它自己进程里**的一个
    空窗口，与真实服务毫无关系。那种"探针"只会给出一份看起来正确的假证据。

    放进 pytest 则天然正确：测试进程**就是**应用进程（`ASGITransport` 直接
    调 app），因此 `feature_service.get_feature_service().window` 读到的正是
    刚刚处理完那 20 次仿真的那个窗口。

    它同时是**断言**而不是纯打印：`diff_real_state` 为空才算通过，
    因此数字不可能与"隔离通过"这件事脱钩。
    """
    import sys

    await _install_profiles()
    case_id = sim_inputs[0]
    before = await snapshot_real_state()

    for _ in range(20):
        r = await client.post(SIM_RUN_URL, json={"event": sim_payload()}, headers=READER)
        assert r.status_code == 200, r.text
    # 再加上一次批量回放（上限 200 条，这里取 20 条）
    batch = await client.post(SIM_BATCH_URL, json={
        "case_id": case_id, "repeat": 20, "seed": 42}, headers=READER)
    assert batch.status_code == 200, batch.text

    after = await snapshot_real_state()
    problems = diff_real_state(before, after)

    out = sys.stdout
    print("\n" + "=" * 74, file=out)
    print("模块 10 交付实测：数据隔离（仿真 20 次 + 批量回放 20 条，前后各读一次真实数据）",
          file=out)
    print("=" * 74, file=out)
    print(f"{'读取项':<28}{'仿真前':>12}{'仿真后':>12}{'变化':>10}", file=out)
    rows = [("04窗口 total_entries", "total_entries"),
            ("04窗口 distinct_keys", "distinct_keys"),
            ("04窗口 seen_ids", "seen_ids"),
            ("04窗口 duplicate_cnt", "duplicate_cnt"),
            ("04窗口 truncated_cnt", "truncated_cnt")]
    for label, key in rows:
        was, got = before["window"][key], after["window"][key]
        print(f"{label:<28}{was:>12}{got:>12}{'未变' if was == got else '变了!':>10}",
              file=out)
    for name in REAL_STATE_COLLECTIONS:
        was = before["collections"][name]["count"]
        got = after["collections"][name]["count"]
        print(f"{name:<28}{was:>12}{got:>12}{'未变' if was == got else '变了!':>10}",
              file=out)
    stats = feature_service.get_feature_service().stats
    print(f"\n04 记账：read_only={stats['read_only']} window_affected={stats['window_affected']} "
          f"persisted={stats['persisted']}", file=out)
    print(f"批量回放：total={batch.json()['data']['total']} "
          f"matched={batch.json()['data']['matched']} "
          f"mismatched={batch.json()['data']['mismatched']} "
          f"false_positive={batch.json()['data']['false_positive']} "
          f"affects_window={batch.json()['data']['affects_window']}", file=out)
    print("=" * 74, file=out)

    assert not problems, "隔离未通过：\n  - " + "\n  - ".join(problems)
    # 20 次单条 + 20 条批量 = 40 次只读特征计算，一次都没有写窗口
    assert stats["read_only"] == 40, stats["read_only"]
    assert stats["window_affected"] == 0, stats["window_affected"]
    assert stats["persisted"] == 0, stats["persisted"]


async def test_twenty_simulations_do_not_touch_window(client, sim_inputs):
    """连续仿真 20 次同一设备事件，真实窗口计数**一个都不变**（V-10-05）。

    这条正是 Spec V-10-05 的验收方式（"连续仿真 20 次同设备事件，断言
    `device_order_cnt_1h` 不变"）。这里把它加强成**窗口的每一个计数**都不变：
    `total_entries`（含本次的条目总数）、`distinct_keys`、`seen_ids`（去重集合）
    与四个异常计数器（乱序/截断/重复/丢弃）。

    为什么连 `seen_ids` 也要比：`ingest` 会把 `event_id` 塞进去重集合。
    若仿真 ingest 了但不落窗口条目，`total_entries` 不变而 `seen_ids` 会涨——
    这类"半污染"会通过去重机制**静默吞掉后续真实事件的计数**，比直接涨计数更难发现。
    """
    await _install_profiles()
    before = await snapshot_real_state()

    for _ in range(20):
        r = await client.post(SIM_RUN_URL, json={"event": sim_payload()}, headers=READER)
        assert r.status_code == 200, r.text

    after = await snapshot_real_state()
    problems = diff_real_state(before, after)
    assert not problems, (
        "仿真污染了真实数据（BR-10-06 / BR-10-05）：\n  - "
        + "\n  - ".join(problems)
        + "\n\n前后快照：\n"
        + format_real_state(before, "仿真前")
        + "\n"
        + format_real_state(after, "仿真后")
    )

    # 04 自己也要如实记账：20 次全是只读，一次窗口写入都没有
    stats = feature_service.get_feature_service().stats
    assert stats["read_only"] == 20, f"应有 20 次只读特征计算，实际 {stats['read_only']}"
    assert stats["window_affected"] == 0, (
        f"仿真路径不得写窗口，实际写了 {stats['window_affected']} 次")
    assert stats["persisted"] == 0, "仿真不得落 E02 快照"

    # E18 是**唯一**被写入的集合（BR-10-08）：20 次仿真 → 恰好 20 条记录
    assert await db.get_db()[COLL_SIM_RUNS].count_documents({}) == 20


async def test_simulation_does_not_add_risk_events_or_cases(client, sim_inputs):
    """仿真不写 `risk_events` / `risk_cases`（BR-10-05）。

    为什么单列一条：`/engine/evaluate` 与 `/sim/run` 都不写 E01，
    而 `reject`/`review` 会经 D5 的建案钩子建案——那条钩子挂在 `_finish()` 的
    **非 dry_run** 分支上，因此它是否真的被挡住，只有查库能证明。
    """
    await _install_profiles()
    before = await snapshot_real_state()
    r = await client.post(SIM_RUN_URL, json={"event": sim_payload()}, headers=READER)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["decision"] == "review", "本条用例应判 review（会触发建案钩子）"
    await flush_decisions()

    after = await snapshot_real_state()
    # ⚠️ 断言必须是**相对**的（`after == before`），不能写 `== 0`：
    # `conftest.prep_db` **没有清 `risk_events`**（它是 03 的集合，清库列表里
    # 只有 E02/E03/E04/E05/E08/E09/E10~E14/E15/E16/E17/E18），因此整套测试跑下来
    # 它里面本来就有别的用例留下的事件。写死 0 会让这条用例在单独跑时绿、
    # 与其它文件一起跑时红——那正是"幽灵缺陷"的典型症状。
    # `snapshot_real_state` 的"前后各读一次"在这里正好是对的解法。
    assert after["collections"][COLL_RISK_EVENTS]["count"] == \
        before["collections"][COLL_RISK_EVENTS]["count"], (
            f"仿真写了 risk_events：{before['collections'][COLL_RISK_EVENTS]['count']}"
            f" → {after['collections'][COLL_RISK_EVENTS]['count']}")
    assert after["collections"][COLL_RISK_CASES]["count"] == 0, "仿真建了案件"
    assert after["collections"][COLL_DECISIONS]["count"] == 0
    assert after["collections"][COLL_DECISION_HITS]["count"] == 0


# ============================================================
# D66：E15 指标桶**绝不**被仿真触发
# ============================================================
async def test_simulation_does_not_write_metric_buckets(client, sim_inputs):
    """仿真不写 E15 `metric_buckets`（**决策 D66** 的隔离要求）。

    D66 的原话：「仿真（`dry_run=true`）路径**绝不写入**（否则仿真会污染真实
    指标，与模块 10 §4.2「数据隔离」直接冲突）」。

    ## 这条用例为什么必须"同时验真实路径"

    只断言"仿真后桶为空"是**弱断言**：如果指标写入整体坏掉了（D66 复发——
    真实路径也不写），仿真当然也不会写，用例依然全绿，而它本该证明的
    "仿真与真实的区别"完全没有被验证。

    因此这里先跑一次**真实入口**（`/engine/evaluate` 的 `dry_run=false`
    等价物是 `POST /events`）确认指标桶**真的会涨**，再跑仿真确认它**不涨**。
    两边一对比，"隔离生效"才是被证明的而不是被假设的。

    真实路径经 `POST /events` 而不是 `/engine/evaluate`：后者的 `dry_run`
    默认为 true，且它的定位就是"调试/仿真"，用真实入口才是对的口径。
    """
    await _install_profiles()
    metrics_col = db.get_db()[COLL_METRIC_BUCKETS]

    # ---------- ① 真实入口：指标桶必须涨（否则下面的断言证明不了任何事） ----------
    real_event = {
        "event_type": "login", "user_id": "U-REAL-01", "device_id": "DREAL0001",
        "ip": "203.0.113.200",
        "scene_extra": {"login_type": "pwd", "success": True},
        "event_id": unique_event_id(), "ts": now_ms() - 1000,
    }
    r = await client.post(EVENTS_URL, json=real_event, headers=WRITER)
    assert r.status_code == 200, r.text
    await flush_decisions()
    real_count = await metrics_col.count_documents({})
    assert real_count > 0, (
        "真实入口没有写指标桶——D66 的调用链断了（模块 05 的 `_record_metrics` "
        "只在 dry_run=false 时执行）。这条断言是下面'仿真不写'的前提："
        "它不成立时，仿真不写指标桶这件事就无法被证明。"
    )
    real_state = await snapshot_real_state()

    # ---------- ② 仿真：指标桶必须**一个桶都不新增** ----------
    for _ in range(3):
        resp = await client.post(SIM_RUN_URL, json={"event": sim_payload()}, headers=READER)
        assert resp.status_code == 200, resp.text
    await flush_decisions()

    sim_state = await snapshot_real_state()
    problems = diff_real_state(real_state, sim_state)
    assert not problems, (
        "仿真污染了真实数据（D66 / BR-10-05）：\n  - " + "\n  - ".join(problems)
    )
    assert sim_state["collections"][COLL_METRIC_BUCKETS]["count"] == real_count, (
        f"E15 指标桶被仿真写入了：真实路径后 {real_count} → 仿真后 "
        f"{sim_state['collections'][COLL_METRIC_BUCKETS]['count']}"
    )


# ============================================================
# 批量回放：D11 的"强制 false"
# ============================================================
async def test_batch_replay_never_touches_window(client, sim_inputs):
    """批量回放 20 条**同样不碰**窗口（D11：批量回放强制 `affect_window=false`）。

    批量的风险比单条大一个量级：一次 20 条（上限 200 条）足以把
    `device_order_cnt_1h` 顶过一个阈值，从而**永久**改变后续真实事件的判定
    （窗口只活在进程内存里，没有回滚手段）。
    """
    await _install_profiles()
    case_id = sim_inputs[0]
    before = await snapshot_real_state()

    r = await client.post(SIM_BATCH_URL, json={"case_id": case_id, "repeat": 20, "seed": 42},
                          headers=READER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 20
    assert data["affects_window"] is False

    after = await snapshot_real_state()
    problems = diff_real_state(before, after)
    assert not problems, "批量回放污染了真实数据：\n  - " + "\n  - ".join(problems)
    assert after["window"]["total_entries"] == before["window"]["total_entries"]
    assert after["window"]["seen_ids"] == before["window"]["seen_ids"]
    assert await db.get_db()[COLL_SIM_RUNS].count_documents({}) == 20


# ============================================================
# D11 的开关本身：affect_window=true 时**确实**会写（否则开关是假的）
# ============================================================
async def test_affect_window_true_really_writes_the_window(client, sim_inputs):
    """`affect_window=true` 时**真的**写窗口（D11 的开关必须是真的）。

    ## 为什么必须验这一面

    一个"永远不写窗口"的实现能通过上面所有隔离用例，但它**不是** D11 要的东西：
    D11 要求"默认不写 + 提供 `affect_window` 开关"。若开关是死的，
    排障场景（"我想看看把这个事件塞进窗口会怎样"）就没有手段，
    而更糟的是**上面那些隔离用例会变成"恒真断言"**——它们不再证明"我们选择
    不写"，只证明"这段代码根本不具备写的能力"。

    因此这里显式验一次"开关打开后窗口确实涨"，让隔离用例具备**区分能力**：
    真正的证据是"同一个接口，开关 off 时不涨、on 时涨"，而不是"它从来不涨"。
    """
    await _install_profiles()
    before = await snapshot_real_state()
    r = await client.post(
        SIM_RUN_URL,
        json={"event": sim_payload(), "affect_window": True},
        headers=READER,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["affects_window"] is True
    assert "已按 affect_window=true" in data["window_premise"], (
        "开了开关就必须在前提声明里如实告知（BR-10-07 的口径）")

    after = await snapshot_real_state()
    assert after["window"]["total_entries"] > before["window"]["total_entries"], (
        "affect_window=true 时窗口必须真的被写入，否则这个开关是装饰品")
    assert after["window"]["seen_ids"] > before["window"]["seen_ids"]

    # 但即使开了开关，**业务库仍然不许被写**：BR-10-09 的 `dry_run` 是服务端强制的，
    # 与 `affect_window` 是两件独立的事（前者挡 decisions，后者决定要不要进窗口）
    assert after["collections"][COLL_DECISIONS]["count"] == 0
    assert after["collections"][COLL_DECISION_HITS]["count"] == 0
    assert after["collections"][COLL_RISK_CASES]["count"] == 0
    assert after["collections"][COLL_METRIC_BUCKETS]["count"] == 0


# ============================================================
# BR-10-08：`sim_runs` 是唯一被写入的集合
# ============================================================
async def test_sim_runs_is_the_only_written_collection(client, sim_inputs):
    """跑一次仿真：六个真实集合全部不变，`sim_runs` **恰好 +1**。

    这条把 BR-10-08 的两个方向同时钉住：① 该写的写了（否则"回看"无从谈起）；
    ② 不该写的一个都没写。
    """
    await _install_profiles()
    before = await snapshot_real_state()
    assert await db.get_db()[COLL_SIM_RUNS].count_documents({}) == 0

    r = await client.post(SIM_RUN_URL, json={"event": sim_payload()}, headers=READER)
    assert r.status_code == 200, r.text
    run_id = r.json()["data"]["run_id"]
    await flush_decisions()

    after = await snapshot_real_state()
    assert not diff_real_state(before, after), diff_real_state(before, after)

    runs = await db.get_db()[COLL_SIM_RUNS].find({}).to_list(length=10)
    assert len(runs) == 1, f"应恰好 1 条 sim_runs，实际 {len(runs)}"
    assert runs[0]["_id"] == run_id
    # E18 的字段（Spec §1 的实体表）：一个都不能少
    for field in ("case_id", "input_event", "trace", "final_decision", "final_score",
                  "elapsed_ms", "run_by", "run_at"):
        assert field in runs[0], f"E18 缺少字段 {field}"


async def test_simulation_does_not_persist_feature_snapshots(client, sim_inputs):
    """仿真不落 E02 `feature_snapshots`（`persist=False`）。

    为什么这条不能省：`compute()` 的落库是**异步**的，因此"跑完就查"可能查不到，
    而"查不到"与"没写"看起来一样。这里显式 `flush()` 之后才断言，
    并把 `stats["persisted"] == 0` 一起验（后者是同步计数的，不受时序影响）。
    """
    await _install_profiles()
    await feature_service.get_feature_service().flush()
    r = await client.post(SIM_RUN_URL, json={"event": sim_payload()}, headers=READER)
    assert r.status_code == 200, r.text
    # 等一切异步落库收尾
    await feature_service.get_feature_service().flush()
    await flush_decisions()

    assert await db.get_db()["feature_snapshots"].count_documents({}) == 0, (
        "仿真写了 E02 快照——仿真不是一次接入，不产出'没有对应事件'的快照")
    stats = feature_service.get_feature_service().stats
    assert stats["persisted"] == 0
    assert stats["skipped_persist"] == 1, (
        f"应有 1 次'跳过落库'的记账，实际 {stats['skipped_persist']}")
