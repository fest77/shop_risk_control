# -*- coding: utf-8 -*-
"""名单库垂直切片：端到端集成测试（真实 MongoDB + 真实 HTTP 栈）。

覆盖：UI 背后的全部接口契约 + 模块 06 的业务规则 + fail-closed 行为。
运行：  .venv\\Scripts\\python.exe -m pytest tests -v
"""
from __future__ import annotations

import time

import pytest
from pymongo.errors import DuplicateKeyError, PyMongoError

from app import constants, db
from app.utils.mask import mask_phone
from app.repos.list_repo import ListRepo
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

LIST_URL = "/api/v1/lists"
USAGE_URL = "/api/v1/lists/scene-usage"
DAY_MS = 24 * 3600 * 1000


def body_of(resp) -> dict:
    """统一响应包结构断言，返回 data。"""
    b = resp.json()
    assert set(b.keys()) == {"ok", "code", "message", "trace_id", "data"}, b
    assert b["ok"] is (b["code"] == "OK"), "ok 必须由 code 派生，不允许两处状态打架"
    assert isinstance(b["trace_id"], str) and b["trace_id"]
    return b


async def create(client, headers=WRITER, **over):
    payload = {
        "list_type": "black",
        "entity_type": "device",
        "entity_value": "D8F2A1C4",
        "reason": "羊毛党设备聚集，已确认批量套券",
    }
    payload.update(over)
    return await client.post(LIST_URL, json=payload, headers=headers)


# ---------------------------------------------------------------- 1 主干
async def test_create_then_list_roundtrip(client):
    """新增 -> 列表可见（E2E 主干：UI 表单 -> API -> 服务 -> Mongo -> 列表返回）。"""
    r = await create(client)
    assert r.status_code == 201, r.text
    b = body_of(r)
    assert b["code"] == "OK"
    item = b["data"]
    assert item["list_type"] == "black"
    assert item["entity_value"] == "D8F2A1C4"
    assert item["source"] == "manual"
    assert item["status"] == "active"
    assert item["operator"] == "strategy01"
    assert item["expire_at"] is None          # 黑名单默认永久（BR-06-22）
    assert item["_id"].startswith("L")

    r2 = await client.get(LIST_URL, params={"list_type": "black"}, headers=WRITER)
    assert r2.status_code == 200
    d = body_of(r2)["data"]
    assert d["total"] == 1
    assert d["page"] == 1 and d["page_size"] == constants.PAGE_SIZE_DEFAULT
    assert [i["entity_value"] for i in d["items"]] == ["D8F2A1C4"]
    assert d["counts"] == {"black": 1, "white": 0, "gray": 0}
    assert isinstance(d["as_of"], int) and abs(d["as_of"] - int(time.time() * 1000)) < 10_000


async def test_health_and_usage_endpoints(client):
    """健康检查与降级状态端点可用。"""
    h = await client.get("/health")
    health = body_of(h)["data"]
    assert h.status_code == 200 and health["mongo"]["connected"] is True
    u = await client.get(USAGE_URL, headers=READER)
    assert u.status_code == 200
    d = body_of(u)["data"]
    assert d["degraded"] is False and d["degraded_since"] is None


# ---------------------------------------------------------------- 2 唯一约束
async def test_duplicate_same_list_returns_409(client):
    assert (await create(client)).status_code == 201
    r = await create(client)
    assert r.status_code == 409
    b = body_of(r)
    assert b["code"] == "CFG-4009"
    assert "已存在" in b["message"]


async def test_unique_index_blocks_race_condition(client):
    """证明唯一约束由**部分唯一索引**兜底，而不是仅靠应用层先查再插。"""
    assert (await create(client)).status_code == 201
    repo = ListRepo(db.get_db())
    with pytest.raises(DuplicateKeyError):
        await repo.insert({
            "_id": "L_RACE_TEST", "list_type": "black", "entity_type": "device",
            "entity_value": "D8F2A1C4", "reason": "并发写入", "source": "manual",
            "related_case_no": None, "effective_at": 1, "created_at": 1,
            "expire_at": None, "status": "active", "operator": "strategy01",
        })


async def test_cross_list_conflict_requires_force(client):
    """同实体已在白名单，再加黑名单 -> 409 且提示「黑名单优先」；force=true 才写入。"""
    r1 = await create(client, list_type="white", entity_value="U000128")
    assert r1.status_code == 201

    r2 = await create(client, entity_value="U000128")
    assert r2.status_code == 409
    msg = body_of(r2)["message"]
    assert "白" in msg and "黑名单优先" in msg

    r3 = await create(client, entity_value="U000128", force=True)
    assert r3.status_code == 201
    assert body_of(r3)["data"]["list_type"] == "black"


# ---------------------------------------------------------------- 3 校验与脱敏
async def test_invalid_entity_type_returns_4010(client):
    r = await create(client, entity_type="imei")
    assert r.status_code == 400
    assert body_of(r)["code"] == "CFG-4010"


async def test_invalid_list_type_rejected(client):
    r = await create(client, list_type="purple")
    assert r.status_code == 400
    assert body_of(r)["code"] == "CFG-4010"


async def test_phone_is_masked_before_storage(client):
    """BR-06-21：phone 维度入库前强制脱敏，格式与决策侧共用同一函数。"""
    r = await create(client, entity_type="phone", entity_value="13900000001")
    assert r.status_code == 201
    stored = body_of(r)["data"]["entity_value"]
    assert stored == "139****0001"
    assert stored == mask_phone("13900000001")

    doc = await db.get_db()[constants.COLL_LIST_ENTRIES].find_one({"entity_type": "phone"})
    assert doc["entity_value"] == "139****0001", "库里存的必须是脱敏值，不能是明文"


async def test_gray_default_expire_is_30_days(client):
    """BR-06-22：灰名单默认 30 天；黑白默认永久。"""
    r = await create(client, list_type="gray", entity_value="117.136.12.88",
                     entity_type="ip")
    assert r.status_code == 201
    expire_at = body_of(r)["data"]["expire_at"]
    delta = expire_at - int(time.time() * 1000)
    assert 29 * DAY_MS < delta <= 30 * DAY_MS + 60_000


async def test_explicit_expire_wins(client):
    ts = int(time.time() * 1000) + 7 * DAY_MS
    r = await create(client, list_type="gray", entity_type="ip",
                     entity_value="10.0.0.1", expire_at=ts)
    assert r.status_code == 201
    assert body_of(r)["data"]["expire_at"] == ts


async def test_missing_required_field_returns_422(client):
    """字段级校验失败属模块 00 的 COM-4001（422）。

    切片期这里错用了模块 06 的 CFG-4012，而该码的真实语义是「导入文件为空或
    格式不符」——同一个码表示两件事违反 BR-00-13，模块 00 收尾时已纠正。
    """
    r = await client.post(LIST_URL, json={"list_type": "black"}, headers=WRITER)
    assert r.status_code == 422
    assert body_of(r)["code"] == "COM-4001"


# ---------------------------------------------------------------- 4 权限
async def test_reviewer_cannot_write(client):
    """BR-06-34 / BR-06-35 + BR-01-14：reviewer 可读不可写。

    越权错误码自模块 01 起由**权限层统一给出** `AUTH-4020`（原 `CFG-4031`
    是模块 06 自建的角色判断，已由 01 §2.2 的权限矩阵取代，见决策 D26）。
    """
    r = await create(client, headers=READER)
    assert r.status_code == 403
    assert body_of(r)["code"] == "AUTH-4020"

    r_read = await client.get(LIST_URL, params={"list_type": "black"}, headers=READER)
    assert r_read.status_code == 200, "审核员必须能只读查看名单（list:read）"


async def test_admin_cannot_write_lists(client):
    """**矩阵裁定**：名单维护只给 strategist，admin 不含（决策 D26）。

    模块 06 的 `BR-06-34` 原文写的是"strategist 与 admin 可写"，而 01 §2.2
    权限矩阵（BR-01-12 声明的唯一真源）与 E19、模块 00 菜单表都把该能力只给
    strategist。三处对一处，故按矩阵执行；BR-06-34 正文已同步更正。
    """
    r = await create(client, headers=ADMIN)
    assert r.status_code == 403
    assert body_of(r)["code"] == "AUTH-4020"


async def test_strategist_can_write(client):
    """写权限正例：strategist 具备 list:write。"""
    assert (await create(client, headers=WRITER)).status_code == 201


# ---------------------------------------------------------------- 5 分页契约
async def test_pagination_contract(client):
    for i in range(3):
        await create(client, entity_value=f"U{i:06d}", entity_type="user")

    r = await client.get(LIST_URL, params={"list_type": "black", "page": 1,
                                           "page_size": 2}, headers=WRITER)
    d = body_of(r)["data"]
    assert d["total"] == 3 and len(d["items"]) == 2 and d["page_size"] == 2

    # page 超出总页数：不报错，返回空列表 + 正确 total（§3.5）
    r2 = await client.get(LIST_URL, params={"list_type": "black", "page": 99,
                                            "page_size": 2}, headers=WRITER)
    assert r2.status_code == 200
    d2 = body_of(r2)["data"]
    assert d2["items"] == [] and d2["total"] == 3


async def test_page_size_over_limit_returns_4008(client):
    r = await client.get(LIST_URL, params={"list_type": "black", "page_size": 101},
                         headers=WRITER)
    assert r.status_code == 400 and body_of(r)["code"] == "CFG-4008"

    r2 = await client.get(LIST_URL, params={"list_type": "black", "page": 201},
                          headers=WRITER)
    assert r2.status_code == 400 and body_of(r2)["code"] == "CFG-4008"


async def test_sort_whitelist_enforced(client):
    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "sort": "operator:desc"}, headers=WRITER)
    assert r.status_code == 400 and body_of(r)["code"] == "CFG-4008"

    r2 = await client.get(LIST_URL, params={"list_type": "black",
                                            "sort": "effective_at:desc"}, headers=WRITER)
    assert r2.status_code == 200


async def test_filters_and_counts(client):
    await create(client, list_type="black", entity_type="device", entity_value="D1")
    await create(client, list_type="white", entity_type="user", entity_value="U1")
    await create(client, list_type="gray", entity_type="ip", entity_value="1.1.1.1")

    r = await client.get(LIST_URL, params={"list_type": "black"}, headers=WRITER)
    d = body_of(r)["data"]
    assert d["counts"] == {"black": 1, "white": 1, "gray": 1}

    r2 = await client.get(LIST_URL, params={"list_type": "white",
                                            "entity_type": "user"}, headers=WRITER)
    d2 = body_of(r2)["data"]
    assert d2["total"] == 1 and d2["items"][0]["entity_value"] == "U1"

    # 筛选命中 0 条 -> 200 + 空列表（不是错误）
    r3 = await client.get(LIST_URL, params={"list_type": "white",
                                            "entity_type": "ip"}, headers=WRITER)
    assert r3.status_code == 200 and body_of(r3)["data"]["total"] == 0


async def test_keyword_search_on_masked_phone(client):
    await create(client, entity_type="phone", entity_value="13900000001")
    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "keyword": "139****0001"}, headers=WRITER)
    assert body_of(r)["data"]["total"] == 1


# ---------------------------------------------------------------- 6 fail-closed
async def test_write_failure_sets_degraded_and_returns_5002(client, monkeypatch):
    """BR-06-24：写入失败绝不静默吞掉 —— 返回 CFG-5002 且置降级标记。"""

    async def boom(self, doc):
        raise PyMongoError("simulated mongo failure")

    monkeypatch.setattr(ListRepo, "insert", boom)

    r = await create(client)
    assert r.status_code == 503
    assert body_of(r)["code"] == "CFG-5002"

    u = await client.get(USAGE_URL, headers=READER)
    d = body_of(u)["data"]
    assert d["degraded"] is True
    assert d["degraded_since"] is not None
    assert "simulated mongo failure" in (d["last_error"] or "")


async def test_degraded_clears_after_successful_write(client, monkeypatch):
    async def boom(self, doc):
        raise PyMongoError("simulated")

    monkeypatch.setattr(ListRepo, "insert", boom)
    assert (await create(client)).status_code == 503
    assert body_of(await client.get(USAGE_URL, headers=READER))["data"]["degraded"] is True

    monkeypatch.undo()
    assert (await create(client)).status_code == 201
    assert body_of(await client.get(USAGE_URL, headers=READER))["data"]["degraded"] is False


# ---------------------------------------------------------------- 7 影响面
async def test_impact_endpoint_for_removal_confirmation(client):
    """BR-06-26：移除前的影响面（在效名单条数）。"""
    await create(client, entity_type="device", entity_value="D9", list_type="black")
    r = await client.get("/api/v1/lists/impact",
                         params={"entity_type": "device", "entity_value": "D9"},
                         headers=WRITER)
    assert r.status_code == 200
    assert body_of(r)["data"]["active_count"] == 1
