# -*- coding: utf-8 -*-
"""模块 03 事件接入验收测试（V-03-01 / V-03-02 / V-03-03 / V-03-10 的边界部分）。

覆盖 BR-03-01 ~ 08 与 §5 的 `EVT-4001~4007`、`EVT-4010`、`EVT-4404`，
外加幂等缓存的**纯逻辑**断言（TTL / LRU / 载荷哈希 / 占位释放）。

**为什么必填字段用表驱动逐字段删**：BR-03-03 的验收判据不是"能拒绝"，而是
"**逐字段列出缺失名**"。只测一条"少传 device_id"无法发现"少传 ip 时不报错"
这类漏判，因此这里对每类事件的**每一个**必填字段（含 `scene_extra` 的必填键）
各构造一条用例。

共享助手（样例载荷、假组件、状态复位）在 `tests/event_testlib.py`——
**不放在另一个 test_*.py 里**，否则会触发 pytest 的跨模块夹具缓存冲突
（见该文件 docstring 的说明）。
"""
from __future__ import annotations

import asyncio
import re
import uuid

import pytest

from app import db
from app.constants import (
    COLL_DECISION_HITS,
    COLL_DECISIONS,
    COLL_FEATURE_SNAPSHOTS,
    COLL_RISK_EVENTS,
)
from app.core.event_simulator import get_simulator
from app.repos.event_repo import EVENT_RETENTION_MS
from app.schemas.event_schema import AMOUNT_FIELD_BY_TYPE, DECISION_FIELDS
from app.services import event_service
from app.utils.timeutil import date_key, now_ms
from tests.conftest import READER, WRITER
from tests.event_testlib import (
    PAYLOAD_FACTORIES,
    after_sale_payload,
    component_reset,
    coupon_payload,
    install_fakes,
    login_payload,
    order_create_payload,
    order_pay_payload,
    reset_runtime_state,
    unique_event_id,
)

pytestmark = pytest.mark.anyio

EVENTS_URL = "/api/v1/events"
BATCH_URL = "/api/v1/events/batch"


@pytest.fixture(autouse=True)
async def event_state():
    """逐用例复位：进程内组件 / 幂等缓存 / 模拟器 + 收尾在途落库任务。

    夹具体只调用 `event_testlib` 的复位函数：把助手放在**非测试模块**里，
    既避免跨模块夹具缓存冲突，又不用把复位逻辑抄两份。
    """
    component_reset()
    reset_runtime_state()
    await db.get_db()[COLL_RISK_EVENTS].delete_many({})
    yield
    # 在途落库任务必须在**本用例的循环里**收尾：否则下一个用例的夹具
    # `db.close()` 会让它们写到已关闭的连接上，或被带进下一个事件循环
    await event_service.flush()
    sim = get_simulator()
    if sim.running:
        await sim.stop()
    reset_runtime_state()


# ============================================================
# 必填集与样例载荷
# ============================================================
def required_fields_for(event_type: str) -> list[str]:
    """该类型的必填字段清单（`user_id` 除外，它有独立的 `EVT-4002`）。

    由 Schema 的 `REQUIRED_BY_TYPE` / `SCENE_EXTRA_BY_TYPE` **现算**，
    不在测试里另抄一份——抄一份就会与实现同步漂移，测试反而变成
    "照着错的实现写"。
    """
    from app.schemas.event_schema import required_fields, scene_extra_spec

    scene_required, _scene_optional = scene_extra_spec(event_type)
    return [f for f in (*required_fields(event_type), *scene_required) if f != "user_id"]


async def post_event(client, payload: dict, headers=None):
    return await client.post(EVENTS_URL, json=payload, headers=headers or WRITER)


def batch_body(count: int, factory=coupon_payload, **over) -> dict:
    body = {"events": [factory() for _ in range(count)]}
    body.update(over)
    return body


# ============================================================ V-03-01 五类事件都能接入
@pytest.mark.parametrize("event_type,factory", PAYLOAD_FACTORIES)
async def test_all_five_event_types_are_accepted(client, event_type, factory):
    """V-03-01：五类各一条合法事件 → 200，且决策块 12 个字段齐全。

    模块 04 已落地、05 未落地，因此结论必然是 `review`（fail-closed）；这里断言的是
    **契约完整性**而不是结论本身（结论由 `test_event_failclosed.py` 覆盖）。

    `stage=feature` 的含义已经变了：以前是"04 不可用"，现在是真实的 04 报告
    **关键特征缺失**（`ip_is_proxy` 依赖 E12 IP 画像，画像库归尚未落地的模块 09），
    于 03 在调 05 之前短路。原因文案是判据："快照不完整"（数据缺口）而非
    "服务不可用"（04 故障）。
    """
    r = await post_event(client, factory())
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["event_id"].startswith("EVT")
    assert data["duplicate"] is False
    assert data["decision"] == "review"
    assert data["degrade"]["stage"] == "feature"
    assert "特征快照不完整" in data["degrade"]["reason"], data["degrade"]["reason"]
    for field in DECISION_FIELDS:
        assert field in data, f"响应缺少决策块字段 {field}"


async def test_event_row_is_persisted_with_ttl_field(client):
    """异步落库：`_id` 即 event_id，且带 90 天 TTL 的 `expire_at`。

    `expire_at` 缺一条就会让那条事件**永久留库**（TTL 索引对缺字段的文档
    不生效），因此在落库路径上直接断言它存在。
    """
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200
    event_id = r.json()["data"]["event_id"]
    assert await event_service.flush() is True

    doc = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id})
    assert doc is not None, "事件未落库"
    assert doc["expire_at"] == doc["received_at"] + EVENT_RETENTION_MS
    assert doc["source"] == "mock_biz"
    assert doc["ts"] is not None


async def test_phone_is_masked_before_persist(client):
    """§2.1：手机号入库前脱敏（`138****6621`）——明文留存本身就是一次泄露。"""
    r = await post_event(client, login_payload())
    event_id = r.json()["data"]["event_id"]
    await event_service.flush()
    doc = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id})
    assert doc["phone"] == "138****6621"


# ============================================================ V-03-02 按类型必填缺失
@pytest.mark.parametrize("event_type,factory", PAYLOAD_FACTORIES)
async def test_missing_required_field_reports_422_with_field_name(
    client, event_type, factory
):
    """V-03-02 / BR-03-03：逐个删除必填字段 → 422 `EVT-4004` 且响应含缺失字段名。"""
    for field in required_fields_for(event_type):
        payload = factory()
        if field in payload:
            del payload[field]
        else:
            # 必填键落在 scene_extra 里（例如 coupon_id / order_no / reason_code）
            assert field in payload["scene_extra"], f"{field} 既不在顶层也不在 scene_extra"
            del payload["scene_extra"][field]
        # `order_create.address_id` 在 Spec 表 3.1 里同时出现在「基础必填」与
        # 「scene_extra 可选键」两处；`amount` 与 scene_extra 的金额字段也是
        # 一组同义字段。两处填一处即满足（BR-03-06 的"只提供其一 → 双向补齐"），
        # 因此要让必填检测真的落到这个字段上，必须**两处都去掉**。
        scene_extra = payload.get("scene_extra") or {}
        scene_extra.pop(field, None)
        if field == "amount":
            scene_extra.pop(AMOUNT_FIELD_BY_TYPE[event_type], None)
        r = await post_event(client, payload)
        assert r.status_code == 422, f"{event_type} 删掉 {field} 后应 422，实际 {r.status_code}"
        body = r.json()
        assert body["code"] == "EVT-4004", f"{event_type}/{field} -> {body['code']}"
        assert field in body["data"]["missing"], (
            f"{event_type} 缺少 {field} 时响应未列出该字段名：{body['data']}"
        )
        assert field in body["message"]


async def test_multiple_missing_fields_are_all_listed(client):
    """BR-03-03 的文案要求逐字段列出（§5 的示例是「device_id、ip」两个）。"""
    payload = coupon_payload()
    del payload["device_id"]
    del payload["ip"]
    r = await post_event(client, payload)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "EVT-4004"
    assert set(body["data"]["missing"]) == {"device_id", "ip"}
    assert "device_id" in body["message"] and "ip" in body["message"]


async def test_missing_user_id_has_its_own_code(client):
    """§5：`user_id` 是最基础标识，缺它归 `EVT-4002`（400）而不是 `EVT-4004`。"""
    payload = login_payload()
    del payload["user_id"]
    r = await post_event(client, payload)
    assert r.status_code == 400
    assert r.json()["code"] == "EVT-4002"


# ============================================================ EVT-4003 枚举
@pytest.mark.parametrize("bad_type", ["", "pay", "LOGIN", "order"])
async def test_unknown_event_type_returns_evt_4003(client, bad_type):
    """BR-03-02：枚举判定必须在**服务层**（模型层用 Enum 会被拦成 COM-4001）。"""
    r = await post_event(client, login_payload(event_type=bad_type))
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "EVT-4003", f"非法 event_type 应得 EVT-4003，实际 {body['code']}"
    assert "login/coupon_receive/order_create/order_pay/after_sale_apply" in body["message"]


# ============================================================ EVT-4001 非法请求体
@pytest.mark.parametrize("raw", [b"", b"[1,2,3]", b"not json", b"\"str\""])
async def test_invalid_body_returns_evt_4001(client, raw):
    """BR-03-01：非 JSON 对象/非法 JSON → `EVT-4001`（不是 COM-4000、也不是 422）。"""
    r = await client.post(EVENTS_URL, content=raw,
                          headers={**WRITER, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "EVT-4001"


# ============================================================ EVT-4005 字段格式
@pytest.mark.parametrize("over,bad_field", [
    ({"ip": "117.136.12.888"}, "ip"),
    ({"ip": "not-an-ip"}, "ip"),
    ({"user_id": "ab"}, "user_id"),                 # 长度 < 3
    ({"user_id": "u 100001"}, "user_id"),           # 含空格，不在字符集内
    ({"device_id": "x"}, "device_id"),              # 长度 < 3
    ({"phone": "1380000662"}, "phone"),             # 10 位
])
async def test_field_format_violation_returns_evt_4005(client, over, bad_field):
    """BR-03-04：字段级格式校验 → `EVT-4005`（400）。"""
    r = await post_event(client, login_payload(**over))
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "EVT-4005", f"{bad_field} -> {body}"
    assert bad_field in body["message"]


@pytest.mark.parametrize("amount", [0, -1, 100.5, "2000"])
async def test_amount_must_be_positive_integer(client, amount):
    """BR-03-04：`amount` 需为正整数（分）。`0`/负数/小数/字符串全部拒绝。"""
    payload = coupon_payload(amount=amount)
    payload["scene_extra"]["face_value"] = amount
    r = await post_event(client, payload)
    assert r.status_code in (400, 422), r.text
    assert r.json()["code"] in ("EVT-4005", "EVT-4001")


async def test_ts_in_the_future_beyond_60s_is_rejected(client):
    """BR-03-07：`ts` 超前于接收时间 60s 以上视为时钟超前 → `EVT-4005`。"""
    r = await post_event(client, login_payload(ts=now_ms() + 120_000))
    assert r.status_code == 400
    assert r.json()["code"] == "EVT-4005"
    assert "ts" in r.json()["message"]


async def test_ts_omitted_is_filled_with_server_now(client):
    """决策 D8 / BR-03-07：`ts` 可省，网关补 `now`；存储层必填。"""
    r = await post_event(client, coupon_payload())
    assert r.status_code == 200
    event_id = r.json()["data"]["event_id"]
    await event_service.flush()
    doc = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id})
    assert doc["ts"] is not None
    assert abs(doc["ts"] - doc["received_at"]) < 2000, "省略 ts 时应补服务端 now"


async def test_late_arrival_is_flagged_and_forced_to_review(client):
    """BR-03-08：`ts` 早于长窗口 1440 分钟 → 仍落库、标 `late_arrival=true`、转 review。

    这里的 `stage=feature` 与"04 是否接入"无关：迟到事件在 03 内部就直接 fail-closed
    （理由是不能把 1 天前的数据塞进滑窗污染后续特征），**根本不进 04/05**，因此它
    永远是 `stage=feature`。04 落地后链路变了，但这条路径没有变。
    """
    late_ts = now_ms() - 2000 * 60_000
    r = await post_event(client, coupon_payload(ts=late_ts))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["decision"] == "review"
    assert data["degrade"]["stage"] == "feature"
    assert "迟到" in data["degrade"]["reason"]
    event_id = data["event_id"]
    await event_service.flush()
    doc = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id})
    assert doc["late_arrival"] is True, "迟到事件必须落库并标记（BR-03-08/24）"


# ============================================================ EVT-4006 scene_extra 白名单
async def test_scene_extra_key_from_another_type_is_rejected(client):
    """BR-03-05：`login` 事件不接受 `coupon_id`（防前端模板串味）。"""
    payload = login_payload()
    payload["scene_extra"]["coupon_id"] = "CP99999"
    r = await post_event(client, payload)
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "EVT-4006"
    assert "coupon_id" in body["message"]


async def test_top_level_unknown_key_is_rejected(client):
    """顶层多余键同样是模板串味，归 `EVT-4006`（而不是静默丢弃）。"""
    r = await post_event(client, login_payload(face_value=2000))
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "EVT-4006"
    assert "face_value" in body["message"]


async def test_allowed_optional_scene_keys_are_accepted(client):
    """白名单**可选键**必须被接受：把可选键误判为非法会让页面无法提交。"""
    r = await post_event(client, coupon_payload(
        scene_extra={
            "coupon_id": "CP1", "activity_id": "ACT1",
            "face_value": 2000, "batch_id": "B9",
        }
    ))
    assert r.status_code == 200


# ============================================================ EVT-4007 金额一致性
async def test_amount_mismatch_with_scene_extra_returns_evt_4007(client):
    """BR-03-06：`amount(20000)` 与 `face_value(2000)` 不一致 → `EVT-4007`。"""
    payload = coupon_payload(amount=20000)
    payload["scene_extra"]["face_value"] = 2000
    r = await post_event(client, payload)
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "EVT-4007"
    assert "20000" in body["message"] and "2000" in body["message"]


@pytest.mark.parametrize("event_type,factory,amount_field", [
    ("coupon_receive", coupon_payload, "face_value"),
    ("order_create", order_create_payload, "total_amount"),
    ("order_pay", order_pay_payload, "pay_amount"),
    ("after_sale_apply", after_sale_payload, "refund_amount"),
])
async def test_amount_is_backfilled_both_ways(client, event_type, factory,
                                              amount_field):
    """BR-03-06 的另一半：**只提供其一 → 双向补齐**（库内不允许两个口径不一致）。"""
    # 方向一：只给顶层 amount
    payload = factory()
    payload["scene_extra"].pop(amount_field, None)
    r = await post_event(client, payload)
    assert r.status_code == 200, r.text
    event_id = r.json()["data"]["event_id"]
    await event_service.flush()
    doc = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id})
    assert doc["scene_extra"][amount_field] == doc["amount"]

    # 方向二：只给 scene_extra 金额
    payload2 = factory()
    nested = payload2["scene_extra"][amount_field]
    del payload2["amount"]
    r2 = await post_event(client, payload2)
    assert r2.status_code == 200, r2.text
    event_id2 = r2.json()["data"]["event_id"]
    await event_service.flush()
    doc2 = await db.get_db()[COLL_RISK_EVENTS].find_one({"_id": event_id2})
    assert doc2["amount"] == nested


# ============================================================ 批量接口
async def test_batch_over_limit_rejects_whole_batch(client):
    """BR-03-16 / EVT-4010：> 500 条**拒绝整批**（不是静默截断前 500 条）。"""
    r = await client.post(BATCH_URL, json=batch_body(501), headers=WRITER)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "EVT-4010"
    assert "501" in body["message"]


async def test_batch_empty_is_rejected(client):
    """空批次按"参数非法"拒绝：成功返回 0 条会让调用方以为提交生效了。"""
    r = await client.post(BATCH_URL, json=batch_body(0), headers=WRITER)
    assert r.status_code == 400
    assert r.json()["code"] == "EVT-4010"


async def test_batch_success_counts(client):
    """§3.2：`total/pass_cnt/review_cnt/reject_cnt/failed_cnt/elapsed_ms` 齐备。

    05 落地前这条断言的是"5 条全部 review"（那时 05 必降级）。现在链路是完整的：
    先给这批事件共用的 IP 补一行 E12 画像（`ip_is_proxy` 是 04 的**关键特征**，
    不补就会因"快照不完整"短路在 `stage=feature`），并发的建边任务来不及在批内
    落库，因此**显式**补上，让计数不再取决于后台任务的调度时机。

    期望值因此是确定的：快照完整 + 测试库里**没有任何规则** → 0 分 → 5 条全 `pass`
    （BR-05-15：没有规则命中时 `rule_score` 就是 0）。`reject_cnt == 0` 同时证明
    没有读到任何名单条目（夹具逐用例清空了 E07）。
    """
    from app.constants import COLL_IP_POOL

    await db.get_db()[COLL_IP_POOL].insert_one(
        {"_id": "203.0.113.10", "is_proxy": False, "is_idc": False}
    )
    r = await client.post(BATCH_URL, json=batch_body(5), headers=WRITER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 5
    assert data["pass_cnt"] == 5, (
        "快照完整、无规则命中 → 5 条全放行；出现 review 说明链路又在某处降级了"
    )
    assert data["review_cnt"] == 0
    assert data["reject_cnt"] == 0
    assert data["failed_cnt"] == 0
    assert data["elapsed_ms"] >= 0
    assert data["results"] is None, "未开启 verbose 时不应回传逐条结果"


async def test_batch_single_failure_does_not_break_the_rest(client):
    """BR-03-16：任一条校验失败**只计入 failed_cnt**，不影响其余条目。"""
    from app.constants import COLL_IP_POOL

    await db.get_db()[COLL_IP_POOL].insert_one(
        {"_id": "203.0.113.10", "is_proxy": False, "is_idc": False}
    )
    good = coupon_payload()
    bad = coupon_payload()
    del bad["device_id"]
    body = {"events": [good, bad, good], "verbose": True}
    r = await client.post(BATCH_URL, json=body, headers=WRITER)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 3
    assert data["failed_cnt"] == 1
    assert data["pass_cnt"] == 2, "两条合法事件都走完了完整决策链路（无规则 → pass）"
    assert data["review_cnt"] == 0
    assert data["reject_cnt"] == 0
    failed = [x for x in data["results"] if not x["ok"]]
    assert len(failed) == 1
    assert failed[0]["code"] == "EVT-4004"
    assert "device_id" in failed[0]["missing"]


async def test_batch_non_array_events_is_rejected(client):
    """`events` 不是数组 → `EVT-4001`（报文格式非法）。"""
    r = await client.post(BATCH_URL, json={"events": "oops"}, headers=WRITER)
    assert r.status_code == 400
    assert r.json()["code"] == "EVT-4001"


async def test_batch_uses_the_same_decision_path(client):
    """批量走的是**同一条决策链路**（BR-03-15/16），因此单条与批量的决策一致。"""
    feature, decision = install_fakes()
    single = await post_event(client, coupon_payload())
    batch = await client.post(BATCH_URL, json={"events": [coupon_payload()]},
                              headers=WRITER)
    assert single.status_code == 200 and batch.status_code == 200
    assert batch.json()["data"]["review_cnt"] == 1
    assert single.json()["data"]["rule_score"] == 55
    assert decision.calls == 2, "批量必须也调用 05（不能绕过决策）"


# ============================================================ V-03-03 编号
async def test_generated_event_ids_are_unique_and_well_formed(client):
    """V-03-03：连发 100 条不带 `event_id`，全部匹配格式且互不重复。"""
    ids = []
    for _ in range(100):
        r = await post_event(client, coupon_payload())
        assert r.status_code == 200, r.text
        ids.append(r.json()["data"]["event_id"])
    assert len(set(ids)) == 100, "当日序列出现重号"
    for event_id in ids:
        assert re.fullmatch(r"EVT\d{20}", event_id), event_id
    assert all(event_id.startswith(f"EVT{date_key(now_ms())}") for event_id in ids)


async def test_client_supplied_event_id_is_used(client):
    """BR-03-09：客户端可传 `event_id` 作为幂等键；未传则由网关注册表生成。"""
    custom = unique_event_id()
    r = await post_event(client, coupon_payload(event_id=custom))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["event_id"] == custom


async def test_client_event_id_alias_is_accepted(client):
    """决策 D3：入参侧 `client_event_id` 与 `event_id` 互为别名，两个都认。"""
    custom = unique_event_id()
    r = await post_event(client, coupon_payload(client_event_id=custom))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["event_id"] == custom


async def test_malformed_client_event_id_is_rejected(client):
    """`event_id` 提供了就必须匹配 `^EVT\\d{8}\\d{12}$`，否则 `EVT-4005`。"""
    r = await post_event(client, coupon_payload(event_id="EVT-1"))
    assert r.status_code == 400
    assert r.json()["code"] == "EVT-4005"


# ============================================================ 详情接口
async def test_detail_after_persist_returns_four_sections(client):
    """V-03-14 / §3.3：落库后详情返回 `event` + `snapshot` + `decision` + `hits`。"""
    r = await post_event(client, coupon_payload())
    event_id = r.json()["data"]["event_id"]
    await event_service.flush()

    d = await client.get(f"{EVENTS_URL}/{event_id}", headers=READER)
    assert d.status_code == 200, d.text
    data = d.json()["data"]
    assert set(data.keys()) == {"event", "snapshot", "decision", "hits"}
    assert data["event"]["_id"] == event_id
    assert data["decision"]["decision"] == "review"
    assert data["hits"] == []
    # 事件原文里不应再嵌一份决策（否则详情页有两个口径）
    assert "decision" not in data["event"]
    assert "feature_snapshot" not in data["event"]


async def test_detail_before_persist_returns_evt_4404(client):
    """§3.3：异步落库未完成时 → `404 EVT-4404` + 重试建议。

    用**不存在**的编号构造该状态（真实竞态窗口只有几十毫秒，测试里无法稳定
    触发；而"查不到就必须给重试建议"这条契约可以稳定断言）。
    """
    missing = unique_event_id()
    r = await client.get(f"{EVENTS_URL}/{missing}", headers=READER)
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "EVT-4404"
    assert "重试" in body["message"]
    assert body["data"]["retry_after_ms"] == 500


# ============================================================ 权限（本模块的假设）
async def test_ingest_requires_sim_run_permission(client, bearer):
    """接入类接口用 `sim:run`（reviewer + strategist）；admin 不在矩阵内 → 403。

    这是本模块对 Spec 未规定处的**刻意假设**（见 `event_api.py` 的模块 docstring）。
    写成测试是为了让"将来有人改权限"立刻可见，而不是静默放宽。
    """
    r = await post_event(client, coupon_payload(), headers=await bearer("admin"))
    assert r.status_code == 403
    assert r.json()["code"] == "AUTH-4020"


async def test_ingest_without_token_is_rejected(client):
    r = await client.post(EVENTS_URL, json=coupon_payload())
    assert r.status_code == 401


async def test_detail_is_readable_by_all_three_roles(client, bearer):
    """详情用 `dashboard:read`（三角色可读）：研判需要回看事件原文。"""
    r = await post_event(client, coupon_payload())
    event_id = r.json()["data"]["event_id"]
    await event_service.flush()
    for role in ("reviewer", "strategist", "admin"):
        d = await client.get(f"{EVENTS_URL}/{event_id}", headers=await bearer(role))
        assert d.status_code == 200, f"{role} 读详情失败：{d.text}"


# ============================================================ 边界：自己只写 risk_events
async def test_only_risk_events_is_written_by_module_03(client):
    """V-03-10 的轻量版：本模块**自己**只写 `risk_events`；E02 是下游 04 写的。

    全量版（Mongo 命令监听）已在验收清单中登记为后续工作；这里断言可静态验证的边界：

    - `risk_events` 有且只有本用例的那一行（03 的产出）；
    - `feature_snapshots`（E02）现在**有 04 落下的 18 项契约快照**——默认装配里 04
      已经接上，它顺着同一条链路把快照写进 E02。旧断言"E02 必须为空"的前提是
      "04 还没落地"，那个前提已经不成立；这不是 03 越界，03 自己不碰 E02，只是它
      调用的下游会写。（只判定"存在 04 形状的行"，不绑定"恰好一行 / 必须是本用例的
      那一行"：E02 不归本模块的夹具清理，且快照编号来自**逐用例复位**的计数器，
      跨用例重号时后台落库会走重试队列并记 `FEA-5002`——那是既有的旁路重试行为，
      与"03 是否越界"无关。）
    - `decisions`/`decision_hits`（E03/E04）仍必须为空：它们归尚未落地的 05，
      而 03 **从不代 05 造结论**（这条边界才是本用例真正要守的东西）。
    """
    from app.engine.feature_compute import FEATURE_KEYS
    from app.services import feature_service

    r = await post_event(client, coupon_payload())
    assert r.status_code == 200, r.text
    await event_service.flush()
    # 04 的落库是它自己的后台任务：不等它收尾，下面就是一条竞态断言
    await feature_service.flush()

    assert await db.get_db()[COLL_RISK_EVENTS].count_documents({}) == 1
    rows = await db.get_db()[COLL_FEATURE_SNAPSHOTS].find({}).to_list(length=50)
    shaped = [
        row for row in rows
        if set(row.get("features") or {}) | set(row.get("missing_features") or [])
        == set(FEATURE_KEYS)
    ]
    assert shaped, "默认装配里 04 已接上：跑完整条链路后 E02 必须有 04 的 18 项契约快照"
    for row in shaped:
        assert str(row["snapshot_id"]).startswith("SNP"), "E02 的编号由 04 生成"
    for coll in (COLL_DECISIONS, COLL_DECISION_HITS):
        assert await db.get_db()[coll].count_documents({}) == 0, (
            f"{coll} 不该被写入：它归尚未落地的 05（边界：00 §3.3）"
        )


async def test_concurrent_distinct_events_are_all_accepted(client):
    """并发接入不同事件：编号互不重复、全部 200（幂等缓存不误伤）。"""
    responses = await asyncio.gather(*[
        post_event(client, coupon_payload(user_id=f"u{uuid.uuid4().hex[:8]}"))
        for _ in range(8)
    ])
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    ids = {r.json()["data"]["event_id"] for r in responses}
    assert len(ids) == 8


# ============================================================
# single-flight 的单元级验证
# ------------------------------------------------------------
# 这一条留在本文件（要用事件循环创建 Future），其余缓存的纯逻辑断言在
# `tests/test_event_store_logic.py`：那里是**不与接口耦合**的确定性推演
# （TTL / LRU / 载荷哈希），单独成文读起来边界更清楚。
# ============================================================
async def test_inflight_follower_waits_instead_of_recomputing():
    """single-flight 的单元级验证：跟随者不会自己再算一遍。"""
    from app.services.idempotency import IdempotencyStore, payload_digest

    store = IdempotencyStore(clock=now_ms)
    digest = payload_digest({"x": 1})
    assert store.reserve("EVT1", digest)[0] == "reserved"

    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    assert store.attach_waiter("EVT1", waiter) is True
    assert store.check("EVT1", digest)[0] == "inflight"

    body = {"event_id": "EVT1", "decision": "review"}
    store.finish("EVT1", body)
    assert await asyncio.wait_for(waiter, timeout=1) is body
    assert store.check("EVT1", digest)[0] == "hit"
