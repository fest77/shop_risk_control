# -*- coding: utf-8 -*-
"""深度自审：针对边界、契约细节与可维护性写的新探针。

与 test_list_slice.py 的分工：那份验证「主干功能对不对」，这份专门找「主干之外哪里会炸」。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from pymongo.errors import PyMongoError

from app import constants, db
from app.repos.list_repo import ListRepo
from tests.conftest import WRITER

pytestmark = pytest.mark.anyio
LIST_URL = "/api/v1/lists"
DAY_MS = 24 * 3600 * 1000


def data_of(resp) -> dict:
    b = resp.json()
    assert set(b.keys()) == {"ok", "code", "message", "trace_id", "data"}, b
    return b


async def create(client, headers=WRITER, **over):
    payload = {"list_type": "black", "entity_type": "device",
               "entity_value": "D8F2A1C4", "reason": "自审用例"}
    payload.update(over)
    return await client.post(LIST_URL, json=payload, headers=headers)


# ============================================================ 契约细节
async def test_get_without_token_is_rejected(client):
    """模块 01 起**默认拒绝**：白名单之外的所有路径都必须带有效令牌。

    阶段一允许无身份只读（当时还没有登录），现在由鉴权中间件统一要求令牌——
    这条用例反向验证了"新增接口天然受保护"，不依赖作者记得加权限依赖。
    """
    r = await client.get(LIST_URL, params={"list_type": "black"})
    assert r.status_code == 401, r.text
    assert data_of(r)["code"] == "AUTH-4002"


async def test_get_missing_required_list_type(client):
    r = await client.get(LIST_URL, headers=WRITER)
    assert r.status_code == 422
    assert data_of(r)["code"] == "COM-4001"


async def test_status_all_returns_every_status(client):
    await create(client, entity_value="A1")
    await db.get_db()[constants.COLL_LIST_ENTRIES].update_one(
        {"entity_value": "A1"}, {"$set": {"status": "expired"}})
    await create(client, entity_value="A2")

    r_active = await client.get(LIST_URL, params={"list_type": "black"}, headers=WRITER)
    assert data_of(r_active)["data"]["total"] == 1

    r_all = await client.get(LIST_URL, params={"list_type": "black", "status": "all"},
                             headers=WRITER)
    assert data_of(r_all)["data"]["total"] == 2


async def test_invalid_status_value(client):
    r = await client.get(LIST_URL, params={"list_type": "black", "status": "whatever"},
                         headers=WRITER)
    assert r.status_code == 400 and data_of(r)["code"] == "CFG-4008"


async def test_expire_before_filter(client):
    soon = int(time.time() * 1000) + DAY_MS
    far = int(time.time() * 1000) + 60 * DAY_MS
    await create(client, entity_type="ip", entity_value="1.1.1.1", expire_at=soon)
    await create(client, entity_type="ip", entity_value="2.2.2.2", expire_at=far)
    await create(client, entity_type="ip", entity_value="3.3.3.3")  # 永久

    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "expire_before": soon + 1000}, headers=WRITER)
    vals = [i["entity_value"] for i in data_of(r)["data"]["items"]]
    assert vals == ["1.1.1.1"], f"永久条目不应被「即将过期」筛出，实际 {vals}"


async def test_page_boundaries(client):
    for i in range(3):
        await create(client, entity_value=f"B{i}", entity_type="user")

    # page=0 属于「分页参数越界」，契约要求 CFG-4008（不是通用校验错）
    r0 = await client.get(LIST_URL, params={"list_type": "black", "page": 0}, headers=WRITER)
    assert r0.status_code == 400, r0.text
    assert data_of(r0)["code"] == "CFG-4008", f"page=0 的错误码应为 CFG-4008，实际 {data_of(r0)['code']}"

    # 边界合法值
    r1 = await client.get(LIST_URL, params={"list_type": "black", "page_size": 1},
                          headers=WRITER)
    assert r1.status_code == 200 and len(data_of(r1)["data"]["items"]) == 1

    r100 = await client.get(LIST_URL, params={"list_type": "black", "page_size": 100},
                            headers=WRITER)
    assert r100.status_code == 200


async def test_multi_level_sort(client):
    await create(client, entity_value="S1", entity_type="user")
    await create(client, entity_value="S2", entity_type="user")
    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "sort": "list_type:asc,effective_at:desc"},
                         headers=WRITER)
    assert r.status_code == 200
    r2 = await client.get(LIST_URL, params={"list_type": "black",
                                            "sort": "effective_at:sideways"}, headers=WRITER)
    assert r2.status_code == 400 and data_of(r2)["code"] == "CFG-4008"


# ============================================================ 脱敏契约（BR-06-21）
async def test_keyword_search_with_plaintext_phone(client):
    """BR-06-21 明确要求「搜索时同样先脱敏再匹配」。

    用户手里拿的是明文手机号，粘贴搜索必须能命中库里脱敏后的值。
    """
    await create(client, entity_type="phone", entity_value="13900000001")
    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "keyword": "13900000001"}, headers=WRITER)
    assert data_of(r)["data"]["total"] == 1, "用明文手机号搜索必须能命中脱敏后的存储值"


async def test_keyword_search_with_masked_phone(client):
    await create(client, entity_type="phone", entity_value="13900000001")
    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "keyword": "139****0001"}, headers=WRITER)
    assert data_of(r)["data"]["total"] == 1


async def test_keyword_with_regex_metacharacters_is_safe(client):
    """关键词含正则元字符时不得被当作正则执行，也不得抛 500。"""
    await create(client, entity_value="D8F2A1C4")
    for kw in [".*", "(", "[a-z]+", "\\", "^D8", "D8.*C4"]:
        r = await client.get(LIST_URL, params={"list_type": "black", "keyword": kw},
                             headers=WRITER)
        assert r.status_code == 200, f"keyword={kw!r} 应安全处理，实际 {r.status_code}"


async def test_keyword_does_not_match_across_fields(client):
    await create(client, entity_value="E1", reason="zzz-marker")
    r = await client.get(LIST_URL, params={"list_type": "black",
                                           "keyword": "zzz-marker"}, headers=WRITER)
    assert data_of(r)["data"]["total"] == 0, "keyword 只应匹配 entity_value，不应匹配 reason"


# ============================================================ 输入边界
async def test_entity_value_length_boundary(client):
    ok = "X" * 128
    r = await create(client, entity_value=ok)
    assert r.status_code == 201

    too_long = "Y" * 129
    r2 = await create(client, entity_value=too_long)
    # 字段级校验失败 -> 模块 00 的 COM-4001（422），不再是切片期误用的 400/CFG-4012
    assert r2.status_code == 422 and data_of(r2)["code"] == "COM-4001"


async def test_reason_length_and_blank(client):
    r = await create(client, entity_value="R1", reason="   ")
    assert r.status_code == 422, "纯空白 reason 不应通过"
    assert data_of(r)["code"] == "COM-4001"

    r2 = await create(client, entity_value="R2", reason="Z" * 201)
    assert r2.status_code == 422 and data_of(r2)["code"] == "COM-4001"


async def test_unicode_values_roundtrip(client):
    r = await create(client, entity_type="user", entity_value="张三-测试",
                     reason="含中文与符号：①②③ ✓")
    assert r.status_code == 201
    got = data_of(r)["data"]
    assert got["entity_value"] == "张三-测试"
    assert "①" in got["reason"]


async def test_whitespace_is_trimmed(client):
    r = await create(client, entity_value="  TRIM-ME  ", reason="  前后有空格  ")
    assert r.status_code == 201
    assert data_of(r)["data"]["entity_value"] == "TRIM-ME"
    assert data_of(r)["data"]["reason"] == "前后有空格"


async def test_expire_at_in_the_past_is_accepted_but_flagged(client):
    """PRD/Spec 未禁止过去时间。当前行为：允许写入（status 仍为 active）。

    这是一条**行为契约**测试：把现状钉住，避免以后无意改变。
    若后续要改为拒绝，应新增错误码并更新此用例。
    """
    past = int(time.time() * 1000) - DAY_MS
    r = await create(client, entity_type="ip", entity_value="9.9.9.9", expire_at=past)
    assert r.status_code == 201
    assert data_of(r)["data"]["expire_at"] == past


async def test_force_without_conflict_still_creates(client):
    r = await create(client, entity_value="F1", force=True)
    assert r.status_code == 201


async def test_forged_token_rejected(client):
    """伪造令牌必须被拒：格式错 -> AUTH-4002，签名错 -> AUTH-4003。

    角色不再来自请求头，因此"自称 hacker"这种攻击面已消失；现在要防的是
    **伪造/篡改 JWT**，这条用例覆盖该路径。
    """
    r = await client.get(LIST_URL, params={"list_type": "black"},
                         headers={"Authorization": "Bearer abc.def.ghi"})
    assert r.status_code == 401
    assert data_of(r)["code"] in ("AUTH-4002", "AUTH-4003")

    # 头部声明 alg=none 的经典绕过手法：必须被拒（BR-01-09）
    import base64
    import json as _json

    def b64(obj: dict) -> str:
        raw = _json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'admin01', 'role': 'admin'})}."
    r2 = await client.get(LIST_URL, params={"list_type": "black"},
                          headers={"Authorization": f"Bearer {forged}"})
    assert r2.status_code == 401, "alg=none 的令牌绝不能被接受"
    assert data_of(r2)["code"] in ("AUTH-4002", "AUTH-4003")


async def test_write_without_token_rejected(client):
    """没有令牌的写请求必须在**鉴权层**就被拒（401），而不是走到业务层再判权限。"""
    r = await client.post(LIST_URL, json={
        "list_type": "black", "entity_type": "device",
        "entity_value": "N1", "reason": "无身份"})
    assert r.status_code == 401
    assert data_of(r)["code"] == "AUTH-4002"


# ============================================================ 并发
async def test_concurrent_duplicate_create_yields_exactly_one_success(client):
    """真并发：应用层先查再插会双漏，必须由部分唯一索引兜底成「一成一败」。"""
    results = await asyncio.gather(*[
        create(client, entity_value="RACE-1") for _ in range(5)
    ])
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1, f"应恰好 1 个成功，实际 {codes}"
    assert codes.count(409) == 4, f"其余应为 409，实际 {codes}"


# ============================================================ 降级路径
async def test_read_failure_when_mongo_down_should_be_503(client, monkeypatch):
    """概要设计 §5.3：Mongo 不可用时管理操作应返回 503，而不是 500。"""

    async def boom(*a, **k):
        raise PyMongoError("simulated mongo down")

    monkeypatch.setattr(ListRepo, "count", boom)
    r = await client.get(LIST_URL, params={"list_type": "black"}, headers=WRITER)
    assert r.status_code == 503, f"Mongo 读失败应为 503，实际 {r.status_code}"
    assert data_of(r)["code"] == "COM-5001"


async def test_health_reports_degraded_when_mongo_down(client, monkeypatch):
    from app import db as dbmod

    class FakeAdmin:
        async def command(self, *a, **k):
            raise PyMongoError("down")

    class FakeClient:
        admin = FakeAdmin()

    monkeypatch.setattr(dbmod, "get_client", lambda: FakeClient())
    r = await client.get("/health")
    assert r.status_code == 200
    health = data_of(r)["data"]
    assert health["mongo"]["connected"] is False
    assert health["status"] == "degraded"
