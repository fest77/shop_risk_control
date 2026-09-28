# -*- coding: utf-8 -*-
"""模块 07 读侧：案件列表与详情聚合（Spec 07 §3.1/§3.2/§3.4、BR-07-01~27）。

## 这一组用例钉住的三条硬约束

1. **`total` 来自接口**（BR-07-05b）：断言的是**接口返回的 `total`** 与库里
   真实条数一致，且越界页仍是"空 items + 正确 total"。前端是否自行计数是前端
   的事，后端要把正确的那个数**给出来**；
2. **三栏一次请求**（BR-07-06）：详情响应里 `case`/`event`/`snapshot`/`decision`/
   `hits`/`profile`/`baseline`/`graph` 同时到达——测试直接断言这一份响应里的
   案件号与判定数据**同属一个案件**（串档是这条规则要防的唯一事故）；
3. **分区降级不整页失败**（BR-07-12 / CASE-5001~5004）：往依赖里注入必失败的
   替身，断言只有对应块为 `null` 且出现在 `degraded_parts` 里，其余块照常。

## 索引（BR-07-27 / V-07-21）

`test_list_query_plan_uses_status_level_created_index` 用 **explain** 证明列表
查询走的是 `ix_status_level_created` 而不是全集合扫描，并顺带断言列表路径
**没有**出现 `decision_hits` / `metric_buckets` 的跨集合聚合（查询计划里不会
出现这两个集合的 `$lookup`，源码级则断言服务文件里不出现这两个集合名）。
"""
from __future__ import annotations

import pytest

from app import db
from app.constants import (
    COLL_CASE_ACTIONS,
    COLL_DECISIONS,
    COLL_DECISION_HITS,
    COLL_FEATURE_SNAPSHOTS,
    COLL_RISK_CASES,
    COLL_RISK_EVENTS,
)
from app.enums import CaseStatus
from app.services import case_query_service as case_query_mod
from app.services import feature_service, graph_service
from app.services.case_service import CASE_DECISIONS
from app.utils.timeutil import now_ms

from tests.case_testlib import insert_action, insert_case, make_case
from tests.conftest import ADMIN, READER, WRITER
from tests.profile_testlib import (
    DEMO_DEVICE,
    DEMO_IP,
    DEMO_USER,
    edge_doc,
    insert_edges,
    insert_profile,
)

pytestmark = pytest.mark.anyio

CASES = "/api/v1/cases"
DAY_MS = 86_400_000


# ============================================================
# 夹具与小工具
# ============================================================
@pytest.fixture(autouse=True)
def _reset_query_service():
    """逐用例复位查询服务的注入依赖（跨用例污染是本项目的幽灵缺陷来源）。

    `configure()` 是"非 None 才替换"的语义，**没有**办法把依赖恢复成默认，
    因此收尾必须显式 `reset_dependencies()`——否则注入了假 profile 的用例
    会让后面所有用例都用那个假实现（与 `ProfileService` 同一处理）。

    ⚠️ 必须是**同步**夹具：异步夹具在 anyio 下会与逐用例的事件循环纠缠，
    而这里要做的只是一次纯同步的属性复位。
    """
    service = case_query_mod.get_case_query_service()
    service.reset_dependencies()
    yield
    service.reset_dependencies()


@pytest.fixture(autouse=True)
async def _clean_risk_events():
    """逐用例清 `risk_events`。

    ⚠️ 这是本测试文件**必需**的一步，原因不是"洁癖"：`conftest.prep_db`
    会逐用例清空业务集合，但它清的清单里**没有** `COLL_RISK_EVENTS`
    （E01 由 03 负责，03 的用例自己造事件）。本文件多个详情用例复用同一个
    案件号（因而复用同一个事件编号），不清就会撞 `risk_events._id` 主键——
    那不是被测代码的缺陷，而是夹具在制造一个真实系统里不可能出现的状态。
    """
    await db.get_db()[COLL_RISK_EVENTS].delete_many({})
    yield


def data_of(response) -> dict:
    payload = response.json()
    assert payload["ok"] is True, payload
    return payload["data"]


def code_of(response) -> str:
    return response.json()["code"]


def event_id_for(case_no: str) -> str:
    """由案件号派生一个**合法格式**的事件编号 `EVT{yyyyMMdd}{12位}`。

    ⚠️ 不能直接用 `EVT-{case_no}`：04 的 `get_snapshot` 会按
    `^EVT\\d{20}$` 校验事件编号（FEA-4001），带连字符的编号会被**直接拒绝**，
    于是快照块永远降级——测试会以为"降级逻辑生效了"，其实是从没查成功过。
    这里用案件号的字符和凑出 12 位数字，保证同一案件号稳定得到同一编号
    （快照与决策都按它插入），不同案件号之间也几乎不会撞。
    """
    suffix = sum(ord(ch) for ch in str(case_no)) * 7919
    return f"EVT20260101{suffix % 10 ** 12:012d}"


async def insert_decision(db_, *, decision_id: str, event_id: str, user_id: str,
                          score: int = 86, level: str = "high",
                          verdict: str = "reject", hit_rule_count: int = 2,
                          decided_at: int | None = None, **over) -> dict:
    """插一条 E03 决策文档（07 只读它，格式对齐 `build_decision_doc`）。"""
    moment = decided_at if decided_at is not None else now_ms()
    doc = {
        "_id": decision_id,
        "event_id": event_id,
        "snapshot_id": f"SNP-{decision_id}",
        "list_hit": {"hit": False, "list_type": None,
                     "entity_type": None, "entity_value": None},
        "rule_score": score,
        "model_score": None,
        "final_score": score,
        "risk_level": level,
        "decision": verdict,
        "hit_rule_count": hit_rule_count,
        "rule_versions": {"R1": "v1", "R2": "v1"},
        "engine_version": "rule-engine-v1",
        "degraded": False,
        "degrade_code": None,
        "degrade_reason": None,
        "elapsed_ms": 12,
        "decided_at": moment,
        "user_id": user_id,
        "scene_code": "login",
        "event_type": "login",
        "source": "rule_engine",
    }
    doc.update(over)
    await db.get_db()[COLL_DECISIONS].insert_one(doc)
    return doc


async def insert_hits(db_, decision_id: str, rows: list[tuple[str, str, int]]) -> None:
    """插 E04 命中明细（`(rule_code, rule_name, score)`），**故意乱序插入**。"""
    docs = [
        {"_id": f"{decision_id}-{index:03d}", "decision_id": decision_id,
         "event_id": f"EVT-{decision_id}", "rule_code": code, "rule_name": name,
         "rule_version": "v1", "score": score, "reason": f"{name} 触发",
         "matched_facts": {"f": index}, "hit_at": now_ms()}
        for index, (code, name, score) in enumerate(rows, start=1)
    ]
    if docs:
        await db.get_db()[COLL_DECISION_HITS].insert_many(docs, ordered=False)


async def insert_snapshot(db_, event_id: str, *, user_id: str = DEMO_USER,
                          features: dict | None = None) -> dict:
    doc = {
        "_id": f"SNP-{event_id}",
        "snapshot_id": f"SNP-{event_id}",
        "event_id": event_id,
        "user_id": user_id,
        "features": features if features is not None else {
            "device_user_cnt": 12, "ip_is_proxy": True, "user_age_days": 3,
        },
        "missing_features": [],
        "window_config": {"short_window_min": 60, "long_window_min": 1440,
                          "agg_mode": "in_memory_sliding", "config_version": "w1"},
        "computed_at": now_ms(),
        "compute_ms": 8,
    }
    await db.get_db()[COLL_FEATURE_SNAPSHOTS].insert_one(doc)
    return doc


async def seed_three_cases() -> list[str]:
    """造 3 条案件：让"筛选/排序/分页"都有可分辨的结果。

    | case_no | status | risk_level | risk_score | 说明 |
    |---|---|---|---|---|
    | CASE-A | pending | high | 90 | 默认排序第一 |
    | CASE-B | pending | medium | 70 | 默认排序第二 |
    | CASE-C | reviewing | high | 80 | 按 status 筛才有 |
    """
    moment = now_ms()
    for case_no, status, level, score, created in (
        ("CASE-A", CaseStatus.PENDING.value, "high", 90, moment),
        ("CASE-B", CaseStatus.PENDING.value, "medium", 70, moment - 1000),
        ("CASE-C", CaseStatus.REVIEWING.value, "high", 80, moment - 2000),
    ):
        doc = make_case(case_no=case_no, status=status, risk_level=level,
                        risk_score=score)
        doc["created_at"] = created
        await db.get_db()[COLL_RISK_CASES].insert_one(doc)
    return ["CASE-A", "CASE-B", "CASE-C"]


# ============================================================
# 列表：契约与 total
# ============================================================
async def test_list_returns_pagination_contract_with_total_from_api(client):
    """§3.1 / §3.4 / BR-07-05b：`items/total/page/page_size/pages` 全在，`total` 是真总数。"""
    await seed_three_cases()
    r = await client.get(CASES, headers=READER)
    assert r.status_code == 200, r.text
    data = data_of(r)

    assert set(data) >= {"items", "total", "page", "page_size", "pages", "as_of"}
    assert data["total"] == 3, "total 必须是满足筛选条件的全部条数"
    assert data["page"] == 1 and data["page_size"] == 20
    assert data["pages"] == 1
    assert isinstance(data["as_of"], int) and data["as_of"] > 0
    # 顶部「共 N 条待审」的 N 就取这个数（BR-07-05b），前端不得自行计数
    assert len(data["items"]) == data["total"]


async def test_list_default_sort_is_risk_level_then_created_at(client):
    """BR-07-03：默认 `risk_level desc → created_at desc`（high 在 medium 前）。"""
    await seed_three_cases()
    data = data_of(await client.get(CASES, headers=READER))
    order = [(i["risk_level"], i["case_no"]) for i in data["items"]]
    assert order[0][0] == "high" and order[-1][0] == "medium"
    # 同档内按 created_at 倒序：A（最新）在 C 之前
    highs = [i["case_no"] for i in data["items"] if i["risk_level"] == "high"]
    assert highs == ["CASE-A", "CASE-C"]


async def test_list_item_fields_match_spec(client):
    """§3.1 的 `items[]` 字段逐项存在（缺一个前端就少一列）。"""
    await insert_case(db.get_db(), case_no="CASE-F", risk_score=88,
                      risk_level="high", user_id=DEMO_USER)
    item = data_of(await client.get(CASES, headers=READER))["items"][0]
    for field in ("case_no", "user_id", "scene_code", "scene_name", "risk_score",
                  "risk_level", "decision", "risk_tags", "status", "assignee",
                  "claimed_at", "disposed_at", "created_at", "estimated_loss"):
        assert field in item, f"列表项缺少契约字段 {field}"
    assert item["case_no"] == "CASE-F"
    # 中文标签由后端给出（BR-00-18：前端不得硬编码）
    assert item["status_label"] == "待审核"
    assert item["scene_name"], "场景中文名必须由后端下发"


async def test_list_supports_repeated_and_comma_separated_filters(client):
    """BR-07-01：同字段多值 OR，重复参数与逗号分隔两种写法都要认。"""
    await seed_three_cases()
    repeated = data_of(await client.get(
        f"{CASES}?risk_level=high&risk_level=medium", headers=READER))
    assert repeated["total"] == 3
    single = data_of(await client.get(f"{CASES}?risk_level=high", headers=READER))
    assert single["total"] == 2
    assert {i["case_no"] for i in single["items"]} == {"CASE-A", "CASE-C"}
    comma = data_of(await client.get(f"{CASES}?risk_level=high,medium", headers=READER))
    assert comma["total"] == repeated["total"]


async def test_list_four_dimension_compound_filter_is_intersection(client):
    """BR-07-01 / V-07-02：四个筛选条件之间是 AND（结果是四者的交集）。"""
    moment = now_ms()
    for case_no, status, level, scene, created in (
        ("CASE-X1", "pending", "high", "login", moment),
        ("CASE-X2", "pending", "high", "order", moment),
        ("CASE-X3", "disposed", "high", "login", moment),
        ("CASE-X4", "pending", "medium", "login", moment),
        ("CASE-X5", "pending", "high", "login", moment - 10 * DAY_MS),
    ):
        doc = make_case(case_no=case_no, status=status, risk_level=level)
        doc["scene_code"] = scene
        doc["created_at"] = created
        await db.get_db()[COLL_RISK_CASES].insert_one(doc)

    query = (f"{CASES}?risk_level=high&scene_code=login&status=pending"
             f"&created_from={moment - DAY_MS}&created_to={moment}")
    data = data_of(await client.get(query, headers=READER))
    assert data["total"] == 1
    assert [i["case_no"] for i in data["items"]] == ["CASE-X1"]


async def test_list_time_range_is_inclusive_on_both_ends(client):
    """§3.4：时间区间**含头含尾**。"""
    moment = now_ms()
    for case_no, created in (("CASE-T1", moment - 5000), ("CASE-T2", moment),
                             ("CASE-T3", moment + 5000)):
        doc = make_case(case_no=case_no)
        doc["created_at"] = created
        await db.get_db()[COLL_RISK_CASES].insert_one(doc)

    data = data_of(await client.get(
        f"{CASES}?created_from={moment - 5000}&created_to={moment}", headers=READER))
    assert {i["case_no"] for i in data["items"]} == {"CASE-T1", "CASE-T2"}


async def test_list_keyword_matches_case_no_and_user_id(client):
    """§3.1 的 `keyword`：匹配 `_id` 或 `user_id`。"""
    await insert_case(db.get_db(), case_no="CASE-K1", user_id="U900001")
    await insert_case(db.get_db(), case_no="CASE-K2", user_id="U900002")
    by_case = data_of(await client.get(f"{CASES}?keyword=K1", headers=READER))
    assert [i["case_no"] for i in by_case["items"]] == ["CASE-K1"]
    by_user = data_of(await client.get(f"{CASES}?keyword=U900002", headers=READER))
    assert [i["case_no"] for i in by_user["items"]] == ["CASE-K2"]


async def test_list_keyword_regex_meta_characters_are_escaped(client):
    """`keyword` 里的正则元字符必须被转义（否则 `.*` 会退化成全表扫描）。"""
    await seed_three_cases()
    data = data_of(await client.get(f"{CASES}?keyword=.*", headers=READER))
    assert data["total"] == 0, "元字符必须被转义：`.*` 不该匹配任何案件号"
    data = data_of(await client.get(f"{CASES}?keyword=(unclosed", headers=READER))
    assert data["total"] == 0, "非法正则不该让接口 500"


async def test_list_assignee_filter(client):
    """§3.1 的 `assignee`（「我的案件」）。"""
    await insert_case(db.get_db(), case_no="CASE-M1", status="reviewing",
                      assignee="reviewer01")
    await insert_case(db.get_db(), case_no="CASE-M2", status="reviewing",
                      assignee="reviewer02")
    data = data_of(await client.get(f"{CASES}?assignee=reviewer01", headers=READER))
    assert [i["case_no"] for i in data["items"]] == ["CASE-M1"]


# ============================================================
# 列表：分页边界
# ============================================================
async def test_pagination_overrun_returns_empty_items_with_correct_total(client):
    """V-07-04 / §3.4：`page` 超过总页数返回空 `items` + 正确 `total`（HTTP 200）。"""
    for index in range(43):
        await insert_case(db.get_db(), case_no=f"CASE-P{index:03d}")
    first = data_of(await client.get(f"{CASES}?page=1&page_size=20", headers=READER))
    assert first["total"] == 43 and first["pages"] == 3 and len(first["items"]) == 20

    third = data_of(await client.get(f"{CASES}?page=3&page_size=20", headers=READER))
    assert len(third["items"]) == 3

    over = await client.get(f"{CASES}?page=4&page_size=20", headers=READER)
    assert over.status_code == 200, "越界页**不报错**"
    payload = data_of(over)
    assert payload["items"] == [] and payload["total"] == 43


async def test_pagination_pages_do_not_overlap_or_skip(client):
    """逐组分页必须稳定：两页拼起来既不漏行也不重行（`_collect_page` 的职责）。"""
    for index in range(7):
        await insert_case(db.get_db(), case_no=f"CASE-S{index:03d}")
    first = data_of(await client.get(f"{CASES}?page=1&page_size=3", headers=READER))
    second = data_of(await client.get(f"{CASES}?page=2&page_size=3", headers=READER))
    names = [i["case_no"] for i in first["items"]] + [i["case_no"] for i in second["items"]]
    assert len(names) == len(set(names)) == 6, "两页之间不得重复"


async def test_list_rejects_page_beyond_max_and_bad_page_size(client):
    """§3.4：`page > 200` 与 `page_size` 越界都是 `400 CASE-4004`。"""
    for query in ("page=201", "page=0", "page_size=0", "page_size=101", "page=abc"):
        r = await client.get(f"{CASES}?{query}", headers=READER)
        assert r.status_code == 400, query
        assert code_of(r) == "CASE-4004", query


async def test_list_rejects_illegal_sort_field_and_direction(client):
    """§3.4：排序字段白名单 + `asc|desc`，其他一律 `CASE-4004`。"""
    for query in ("sort=user_id:desc", "sort=risk_score:sideways", "sort=:desc"):
        r = await client.get(f"{CASES}?{query}", headers=READER)
        assert r.status_code == 400, query
        assert code_of(r) == "CASE-4004", query


async def test_list_accepts_sort_whitelist(client):
    """白名单内的排序必须生效（否则前端列头排序全是 400）。"""
    await seed_three_cases()
    data = data_of(await client.get(f"{CASES}?sort=risk_score:asc", headers=READER))
    scores = [i["risk_score"] for i in data["items"]]
    assert scores == sorted(scores), scores


async def test_list_rejects_illegal_time_range(client):
    """§3.4 / CASE-4004：`from > to`、跨度 > 90 天、非法枚举值。"""
    moment = now_ms()
    for query in (
        f"created_from={moment}&created_to={moment - 1000}",
        f"created_from={moment - 91 * DAY_MS}&created_to={moment}",
        "risk_level=super_high",
        "status=unknown_state",
    ):
        r = await client.get(f"{CASES}?{query}", headers=READER)
        assert r.status_code == 400, query
        assert code_of(r) == "CASE-4004", query


async def test_list_empty_state_has_zero_total_and_zero_pages(client):
    """§2.2.1 空态：`total=0`、`pages=0`、`items=[]`（页面据此区分两种空态）。"""
    data = data_of(await client.get(CASES, headers=READER))
    assert data["items"] == []
    assert data["total"] == 0 and data["pages"] == 0


# ============================================================
# 列表：索引与"不得跨集合聚合"（BR-07-27 / V-07-21）
# ============================================================
async def test_list_query_plan_uses_status_level_created_index(client):
    """BR-07-27 / V-07-21：列表查询必须命中 `ix_status_level_created`。

    两件事一起断言：

    1. **工作台默认筛选形态**（`status=pending` + `risk_level=high`，即大盘
       下钻带过来的那个组合）**没有 `SORT` 阶段**——这是索引前缀真正生效的样子；
    2. 只给 `status`（四筛选器里最常见的一种）时仍必须是 **`IXSCAN`** 而不是
       `COLLSCAN`。它退化成"索引内排序"（`SORT` type=default）是可接受的：
       `status` 命中了索引前导列，扫描范围被收窄，而不是把整个集合拉回来再排。
    """
    await seed_three_cases()
    col = db.get_db()[COLL_RISK_CASES]
    order = [("risk_level", -1), ("created_at", -1)]

    plan = await col.find({"status": "pending", "risk_level": "high"}).sort(order).explain()
    text = str(plan["queryPlanner"]["winningPlan"])
    assert "ix_status_level_created" in text, text[:2000]
    assert "COLLSCAN" not in text, f"不得全集合扫描：{text[:2000]}"
    assert "'stage': 'SORT'" not in text, (
        f"status+risk_level 等值时应当直接走索引、不需要 SORT 阶段：{text[:2000]}")

    plan2 = await col.find({"status": "pending"}).sort(order).explain()
    text2 = str(plan2["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in text2 and "COLLSCAN" not in text2, text2[:2000]
    assert "ix_status_level_created" in text2, text2[:2000]


async def test_list_total_is_counted_through_the_index(client):
    """`total` 走**逐 status 分组计数**（索引前导列等值），不是拉回来数一数。"""
    await seed_three_cases()
    want = await db.get_db()[COLL_RISK_CASES].count_documents(
        {"status": "pending", "risk_level": "high"})
    data = data_of(await client.get(
        f"{CASES}?status=pending&risk_level=high", headers=READER))
    assert data["total"] == want == 1


async def test_list_path_never_touches_other_collections():
    """BR-07-27 的源码级守卫：读侧不直连别人的集合、也不做跨集合聚合。

    ⚠️ 断言的是**真实会被违反的写法**，不是文档里的名词：
    `decision_hits` / `metric_buckets` 这两个名字在 docstring 里**必须出现**
    （它们正是"不许聚合"这条规则的宾语），拿它们当禁词会把正确的注释判成违规。
    真正该禁的是"直连集合"与"聚合管道"。
    """
    from pathlib import Path

    from app import config as app_config

    source = Path(app_config.ROOT, "app", "services",
                  "case_query_service.py").read_text(encoding="utf-8")
    # ① 不得用聚合管道（实时跨集合聚合的唯一实现方式）
    for banned in ("aggregate(", "$lookup", "$group", "$unionWith", "$facet"):
        assert banned not in source, f"读侧不得出现聚合管道：{banned}"
    # ② 不得直连集合：只允许通过各模块自己的仓储/服务取数
    for foreign in ("db_module.get_db()[", "COLL_DECISION_HITS", "COLL_METRIC_BUCKETS",
                    "COLL_RISK_EVENTS"):
        assert foreign not in source, f"读侧不得直连其它集合：{foreign}"
    # ③ 唯一允许出现的集合名是 `risk_cases`——而且它只作为**注明出处**的注释存在，
    #    真正取数走 `CaseRepo.col`（08 的仓储）
    assert "risk_cases" in source
    assert source.count("COLL_RISK_CASES") == 0, "集合句柄一律从仓储取，不在本文件拼"


async def test_only_indexed_filter_fields_are_used(client):
    """列表筛选只允许**索引友好且属于 E08 快照**的字段（BR-07-02）。"""
    await seed_three_cases()
    flt = case_query_mod.CaseQueryService._build_filter(
        levels=["high"], scenes=["login"], start=1, end=2,
        assignee="reviewer01", keyword="CASE",
    )
    assert set(flt) == {"risk_level", "scene_code", "created_at", "assignee", "$or"}
    # BR-07-02：等级筛选用案件上的**冗余快照**，绝不用当前规则集重算
    assert flt["risk_level"] == "high"
    assert "risk_tags" not in flt, "risk_tags 在 Spec 里标为预留，先不进查询条件"


# ============================================================
# 详情：一次响应驱动三栏
# ============================================================
async def _seed_full_detail(case_no: str = "CASE-DETAIL") -> str:
    """造一份"八块齐全"的案件，返回案件号。

    `case_no` 可指定：详情用例之间共用同一个案件号是安全的（`_clean_risk_events`
    逐用例清了 E01，而 E08/E03/E04/E02 由 `prep_db` 清），但**必须**让
    `event_id` 走 `event_id_for` 得到合法格式，否则 04 的 `get_snapshot`
    会按 FEA-4001 直接拒绝，快照块永远降级。
    """
    doc = make_case(case_no=case_no, status="reviewing", risk_level="high",
                    risk_score=86, decision="reject",
                    event_id=event_id_for(case_no))
    doc["user_id"] = DEMO_USER
    # 只有在库的决策也是"会建案"的那两种结论时，这份夹具才自洽
    # （`review` / `reject`；`pass` 不建案——BR-08-01）
    assert doc["decision"] in CASE_DECISIONS
    event = {
        "_id": doc["event_id"], "event_type": "coupon_receive", "user_id": DEMO_USER,
        "biz_no": "CP12345", "device_id": DEMO_DEVICE, "ip": DEMO_IP,
        "phone": "139****0001", "address_id": "ADDR-7712", "amount": 19800,
        "scene_extra": {"coupon_id": "CP12345", "face_value": 20000},
        "ts": doc["created_at"], "received_at": doc["created_at"],
    }
    db_ = db.get_db()
    await db_[COLL_RISK_CASES].insert_one(doc)
    await db_[COLL_RISK_EVENTS].insert_one(event)
    await insert_snapshot(db_, doc["event_id"], user_id=DEMO_USER)
    await insert_decision(db_, decision_id=doc["decision_id"], event_id=doc["event_id"],
                          user_id=DEMO_USER, score=86, level="high", verdict="reject")
    await insert_hits(db_, doc["decision_id"],
                      [("R-ORDER", "同设备下单聚集", 20),
                       ("R-REJECT", "黑名单历史", 40)])
    await insert_profile(db_)
    await insert_edges(db_, [
        edge_doc("user", DEMO_USER, "device", DEMO_DEVICE, "used_device",
                 weight=3, risk_flag=True),
    ])
    return case_no


async def test_detail_returns_all_three_columns_in_one_response(client):
    """BR-07-06 / V-07-06：一次请求同时拿到三栏所需的全部数据。"""
    case_no = await _seed_full_detail()
    r = await client.get(f"{CASES}/{case_no}", headers=READER)
    assert r.status_code == 200, r.text
    data = data_of(r)

    for key in ("case", "event", "snapshot", "decision", "hits", "profile",
                "baseline", "graph", "degraded_parts"):
        assert key in data, f"详情响应缺少 {key}"
    assert data["degraded_parts"] == []

    # —— 左栏 ——
    assert data["case"]["case_no"] == case_no
    assert data["case"]["status"] == "reviewing"
    # —— 中栏：事件 + 快照 + 画像 + 图谱 ——
    assert data["event"]["event_id"] == data["case"]["event_id"]
    assert data["event"]["event_type"] == "coupon_receive"
    assert data["snapshot"]["features"]["device_user_cnt"] == 12
    assert data["profile"]["user"]["user_id"] == DEMO_USER
    assert data["graph"]["center"]["id"] == DEMO_USER
    assert data["graph"]["nodes"], "图谱必须带节点（2 跳查询的最小形态）"
    # —— 右栏：判定摘要 + 命中明细 ——
    assert data["decision"]["decision_id"] == data["case"]["decision_id"]
    assert data["decision"]["final_score"] == 86
    assert data["decision"]["decision"] == "reject"

    # **串档兜底**（BR-07-09）：三栏数据必须同属一个案件
    assert data["event"]["user_id"] == data["case"]["user_id"]
    assert data["decision"]["event_id"] == data["case"]["event_id"]


async def test_detail_hits_are_sorted_by_score_desc(client):
    """§2.4.2：命中明细按 `score desc`（乱序插入也要有序返回）。"""
    case_no = await _seed_full_detail()
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    scores = [h["score"] for h in data["hits"]]
    assert scores == sorted(scores, reverse=True) == [40, 20]
    assert data["hits"][0]["rule_name"] == "黑名单历史"
    assert "rule_version" in data["hits"][0]


async def test_detail_baseline_reuses_feature_meta_reference(client):
    """D68：基线列复用 04 的 `/features/meta` 静态参照，**不新增 09 聚合**。"""
    case_no = await _seed_full_detail()
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    meta = {item["key"]: item for item in feature_service.build_feature_meta()}

    assert set(data["baseline"]) == set(data["snapshot"]["features"])
    for key, block in data["baseline"].items():
        # 与 `/features/meta` **逐字一致**（同一个纯函数，不可能漂移）
        assert block["baseline"] == meta[key]["baseline"], key
        assert block["direction_hint"] == meta[key]["direction_hint"], key
        assert block["has_baseline"] == meta[key]["has_baseline"], key
    # 无基线项必须显式标出（BR-04-19：显示 `—` 而不是"正常"）
    assert data["baseline"]["user_age_days"]["has_baseline"] is False


async def test_detail_does_not_recompute_score(client):
    """§1.1 / V-07-14：页面不重算分值——库里改成 77，详情必须显示 77。"""
    case_no = await _seed_full_detail()
    await db.get_db()[COLL_DECISIONS].update_one(
        {"_id": f"DEC-{case_no}"}, {"$set": {"final_score": 77, "risk_level": "medium"}})
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    assert data["decision"]["final_score"] == 77
    assert data["decision"]["risk_level"] == "medium"
    # 左栏的 `case` 块读的是 E08 上的冗余快照（BR-07-02），它与 E03 各自独立
    assert data["case"]["risk_score"] == 86


async def test_detail_missing_case_returns_case_4001(client):
    """§3.2 状态码：案件不存在 → `404 CASE-4001`（不是 08 的 `DSP-4040`）。"""
    r = await client.get(f"{CASES}/CASE-NOT-EXIST", headers=READER)
    assert r.status_code == 404
    assert code_of(r) == "CASE-4001"


async def test_detail_passes_max_hop_two_to_graph(client):
    """AD-06：图谱请求必须带 `max_hop=2`（由详情聚合传出，不是前端各自拼）。"""
    case_no = await _seed_full_detail()
    calls: list[tuple] = []
    real = graph_service.get_graph_service()

    class SpyGraph:
        async def query(self, entity_type, entity_id, **kwargs):
            calls.append((entity_type, entity_id, kwargs))
            return await real.query(entity_type, entity_id, **kwargs)

    case_query_mod.get_case_query_service().configure(graph_service=SpyGraph())
    await client.get(f"{CASES}/{case_no}", headers=READER)
    assert calls and calls[0][0] == "user" and calls[0][1] == DEMO_USER
    assert calls[0][2].get("max_hop") == 2


# ============================================================
# 详情：分区降级（BR-07-12 / CASE-5001~5004）
# ============================================================
class _Exploding:
    """任何方法调用都抛异常的替身（模拟"该分区依赖不可用"）。"""

    def __init__(self, message: str = "simulated dependency down"):
        self.message = message

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            raise RuntimeError(self.message)

        return _boom


async def test_detail_degrades_graph_only(client):
    """V-07-17 / BR-07-12：图谱失败只降级图谱，其余卡片与左栏照常可用。"""
    case_no = await _seed_full_detail()
    case_query_mod.get_case_query_service().configure(graph_service=_Exploding())
    r = await client.get(f"{CASES}/{case_no}", headers=READER)
    assert r.status_code == 200, "分区失败**不得**整页失败"
    data = data_of(r)
    assert data["graph"] is None
    assert "graph" in data["degraded_parts"]
    # 其余块必须仍然可用（"部分失败不隐瞒，但也不能牵连别人"）
    assert data["decision"]["final_score"] == 86
    assert data["profile"]["user"]["user_id"] == DEMO_USER
    assert data["snapshot"] is not None and data["event"] is not None


async def test_detail_degrades_profile_snapshot_and_graph_together(client):
    """BR-07-12：多个分区各自降级（不是"一个坏了整页坏"）。"""
    case_no = await _seed_full_detail()
    case_query_mod.get_case_query_service().configure(
        graph_service=_Exploding(), profile_service=_Exploding("profile down"),
        feature_service=_Exploding("feature down"),
    )
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    assert set(data["degraded_parts"]) >= {"graph", "profile", "snapshot"}
    assert data["graph"] is None and data["profile"] is None
    assert data["snapshot"] is None
    assert data["baseline"] == {}, "没有快照就没有基线列"
    assert data["decision"]["final_score"] == 86, "判定块不受其它分区影响"


async def test_detail_degrades_decision_and_hits_together(client):
    """CASE-5004：判定数据取不到时 `decision=null` 且进 `degraded_parts`。

    页面据此显示「未取到系统判定数据，请勿据此放行」并置灰——
    **绝不显示 0 分或"放行"**（缺数据 ≠ 放行）。这条断言钉的就是"不给 0 分"。
    """
    case_no = await _seed_full_detail()
    case_query_mod.get_case_query_service().configure(decision_repo=_Exploding())
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    assert data["decision"] is None, "取不到判定就必须是 null，绝不能补一个 0 分结论"
    assert data["hits"] == []
    assert "decision" in data["degraded_parts"]
    # 其余块照常
    assert data["profile"]["user"]["user_id"] == DEMO_USER


async def test_detail_degrades_event_when_event_row_is_gone(client):
    """事件有 90 天 TTL：行不见了属正常降级（不是故障，但必须如实标记）。"""
    case_no = await _seed_full_detail()
    await db.get_db()["risk_events"].delete_many({})
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    assert data["event"] is None and "event" in data["degraded_parts"]
    assert data["case"]["case_no"] == case_no


async def test_detail_reports_hits_anomaly_when_rows_are_missing(client):
    """`hit_rule_count > 0` 却查不到明细行 = 数据异常，如实进 `degraded_parts`。"""
    case_no = await _seed_full_detail()
    await db.get_db()[COLL_DECISION_HITS].delete_many({})
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    assert data["hits"] == []
    assert "hits" in data["degraded_parts"]
    assert data["decision"]["hit_rule_count"] == 2


async def test_detail_pass_case_without_hits_is_not_degraded(client):
    """对照组：`hit_rule_count=0` 时没有明细是**正常**的（不得误报降级）。"""
    case_no = "CASE-PASS"
    doc = make_case(case_no=case_no, status="disposed", decision="pass",
                    risk_score=0, risk_level="low")
    await db.get_db()[COLL_RISK_CASES].insert_one(doc)
    await insert_decision(db.get_db(), decision_id=doc["decision_id"],
                          event_id=doc["event_id"],
                          user_id=DEMO_USER, score=0, level="low", verdict="pass",
                          hit_rule_count=0)
    data = data_of(await client.get(f"{CASES}/{case_no}", headers=READER))
    assert data["hits"] == []
    assert "hits" not in data["degraded_parts"], "没有命中不是故障"


# ============================================================
# 权限（D69：`case:read` 仅 reviewer）
# ============================================================
async def test_case_read_is_reviewer_only(client):
    """D69：`case:read` 只给 reviewer；admin / strategist 一律 `403 AUTH-4020`。

    这里**如实断言**与 Spec BR-07-24（"admin 只读列表与详情"）的偏离：
    冻结矩阵是唯一真源，本轮不实现 admin 只读（见交付报告）。
    """
    await seed_three_cases()
    for headers in (ADMIN, WRITER):
        r = await client.get(CASES, headers=headers)
        assert r.status_code == 403, r.text
        assert code_of(r) == "AUTH-4020"
        r2 = await client.get(f"{CASES}/CASE-A", headers=headers)
        assert r2.status_code == 403
        assert code_of(r2) == "AUTH-4020"


async def test_case_read_requires_authentication(client):
    """未登录一律 401（读接口也不例外）。"""
    r = await client.get(CASES)
    assert r.status_code == 401


# ============================================================
# 与 08 的处置侧共存（D67：07 只做加法）
# ============================================================
async def test_disposal_endpoints_are_untouched(client):
    """D67：07 的新增不得改变 08 已交付端点（同一个文件、两个编号各自登记）。"""
    from app.api import MODULE_ROUTERS

    assert "07" in MODULE_ROUTERS and "08" in MODULE_ROUTERS
    paths_07 = {route.path for route in MODULE_ROUTERS["07"].routes}
    paths_08 = {route.path for route in MODULE_ROUTERS["08"].routes}
    assert paths_07 == {"/cases", "/cases/{case_no}"}
    assert "/cases/{case_no}/claim" in paths_08
    assert "/cases/{case_no}/dispose" in paths_08
    # 处置流水（07 详情页复用）仍在 08 名下，07 不重复实现
    assert "/cases/{case_no}/actions" in paths_08


async def test_action_rows_are_still_readable_for_detail(client):
    """08 的 `GET /cases/{no}/actions` 仍可用（07 的详情页复用，不重复实现）。"""
    await insert_case(db.get_db(), case_no="CASE-ACT", status="disposed")
    await insert_action(db.get_db(), "CASE-ACT")
    r = await client.get(f"{CASES}/CASE-ACT/actions", headers=READER)
    assert r.status_code == 200, r.text
    assert data_of(r)["total"] == 1
    assert db.get_db()[COLL_CASE_ACTIONS].name == COLL_CASE_ACTIONS


# ============================================================
# 纯函数单测（不依赖数据库）
# ============================================================
async def test_parse_sort_defaults_and_errors():
    """`parse_sort`：空 → 默认；白名单外/方向非法 → `CASE-4004`。"""
    from app.errors import AppError

    assert case_query_mod.parse_sort(None) == list(case_query_mod.DEFAULT_SORT)
    assert case_query_mod.parse_sort("") == list(case_query_mod.DEFAULT_SORT)
    assert case_query_mod.parse_sort("risk_score:asc") == [("risk_score", 1)]
    assert case_query_mod.parse_sort("risk_level:desc,created_at:desc") == [
        ("risk_level", -1), ("created_at", -1)]
    assert case_query_mod.parse_sort("created_at") == [("created_at", 1)]
    for bad in ("user_id:desc", "risk_score:up", ":desc"):
        with pytest.raises(AppError) as info:
            case_query_mod.parse_sort(bad)
        assert info.value.code == "CASE-4004"


async def test_normalize_multi_accepts_both_writings():
    """多值筛选：重复参数与逗号分隔都归一成列表，且去重保序。"""
    assert case_query_mod.normalize_multi(None) == []
    assert case_query_mod.normalize_multi("high") == ["high"]
    assert case_query_mod.normalize_multi(["high", "medium"]) == ["high", "medium"]
    assert case_query_mod.normalize_multi("high,medium") == ["high", "medium"]
    assert case_query_mod.normalize_multi(["high,medium", " high "]) == ["high", "medium"]


async def test_degradable_parts_names_are_the_response_keys():
    """`degraded_parts` 的取值必须是响应里真实存在的键（前端按它切卡片）。"""
    assert set(case_query_mod.DEGRADABLE_PARTS) == {
        "event", "snapshot", "decision", "profile", "baseline", "graph",
    }


async def test_service_never_writes():
    """读侧的硬边界（只读）：源码级断言——服务里不得出现任何写方法。"""
    from pathlib import Path

    from app import config as app_config

    source = Path(app_config.ROOT, "app", "services",
                  "case_query_service.py").read_text(encoding="utf-8")
    for banned in ("insert_one", "update_one", "delete_one", "find_one_and_update",
                   "bulk_write", "replace_one"):
        assert banned not in source, f"读侧不得写库：{banned}"


async def test_mongo_constants_come_from_the_single_source():
    """轻量守卫：集合名取自 `constants` 的唯一真源（测试夹具不另造一张表）。"""
    from app.constants import COLL_RISK_CASES as CANONICAL

    assert COLL_RISK_CASES == CANONICAL == "risk_cases"
    assert {s.value for s in CaseStatus} == {"pending", "reviewing",
                                            "disposed", "archived"}
    # 决策块必须复用 05 的结论语义（`pass` 不建案，BR-08-01）
    assert set(CASE_DECISIONS) == {"review", "reject"}
    # 「会建案」的两种结论：列表里那三种之外的取值不应出现在真实案件上
    assert COLL_FEATURE_SNAPSHOTS == "feature_snapshots"
