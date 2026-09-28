# -*- coding: utf-8 -*-
"""统一响应 / 分页契约与框架级错误处理（模块 00 §3.1 / §3.2 / §5，V-00-02 / 03 / 07）。

这些用例覆盖"每个接口都会走、但很容易被某个模块漏掉"的横切约定。它们的存在
使得任何新模块只要忘记包 envelope、忘记分页字段或引错了兜底错误码，都会立刻变红。
"""
from __future__ import annotations

import pytest

from app.core.ratelimit import LIMITER
from tests.conftest import READER, WRITER

pytestmark = pytest.mark.anyio

ENVELOPE_KEYS = {"ok", "code", "message", "trace_id", "data"}

# `tolerant_client`（不重抛应用异常）由 tests/conftest.py 统一提供，
# 此处不再重复定义：两处同名夹具容易造成"某个模块用的是哪一份"的困惑。


def _assert_envelope(body: dict, *, ok: bool | None = None) -> dict:
    assert set(body) == ENVELOPE_KEYS, f"响应包结构不符：{sorted(body)}"
    assert isinstance(body["ok"], bool)
    assert isinstance(body["trace_id"], str) and body["trace_id"]
    assert isinstance(body["message"], str) and body["message"]
    if ok is not None:
        assert body["ok"] is ok, body
    return body


# ============================================================ §3.1 统一响应
async def test_all_public_endpoints_share_one_envelope(client):
    """V-00-02：抽查全部已实现接口，断言响应都含 ok 与 trace_id。"""
    probes = [
        ("GET", "/health", None),
        ("GET", "/api/v1/common/enums", None),
        ("GET", "/api/v1/common/meta", None),
        ("GET", "/api/v1/lists?list_type=black", None),
    ]
    for method, url, payload in probes:
        r = await client.request(method, url, json=payload)
        assert r.status_code < 500, f"{url} 返回 {r.status_code}"
        body = _assert_envelope(r.json())
        assert body["ok"] is (body["code"] == "OK"), f"{url} 的 ok 与 code 不一致"
        # trace_id 必须同时出现在响应头与响应体，便于前端 toast 与日志关联
        assert r.headers.get("X-Trace-Id") == body["trace_id"], f"{url} 的 trace_id 头体不一致"


# ============================================================ §3.2 分页契约
async def test_pagination_contract_fields(client):
    """V-00-03：列表接口的 items/total/page/size/pages 齐全。

    注意本项目的分页字段名是 `page_size`（模块 06 §3.5），响应里同时给出
    `page_size` 与 `pages`，前端只依赖这两者与 `total`。
    """
    r = await client.get("/api/v1/lists",
                         params={"list_type": "black", "page": 2, "page_size": 5},
                         headers=READER)
    assert r.status_code == 200, r.text
    data = _assert_envelope(r.json())["data"]
    for field in ("items", "total", "page", "page_size", "pages"):
        assert field in data, f"分页响应缺少 {field}"
    assert data["page"] == 2 and data["page_size"] == 5
    assert isinstance(data["total"], int) and isinstance(data["pages"], int)
    assert data["items"] == [] or isinstance(data["items"], list)


# ============================================================ §5 框架级错误
async def test_unknown_path_returns_com_4004(client):
    """未匹配路由必须是 COM-4004 且带 trace_id，而不是裸 404。

    **注意鉴权带来的行为变化**：鉴权中间件在路由之前执行，因此**未登录**访问
    不存在的路径会先得到 401 而不是 404。这是有意的——不向未认证者暴露
    "哪些路径存在"；已登录用户才会看到 404。两种情况都保持统一响应包。
    """
    unauth = await client.get("/api/v1/definitely-not-here")
    assert unauth.status_code == 401
    assert _assert_envelope(unauth.json(), ok=False)["code"] == "AUTH-4002"

    r = await client.get("/api/v1/definitely-not-here", headers=READER)
    assert r.status_code == 404
    body = _assert_envelope(r.json(), ok=False)
    assert body["code"] == "COM-4004"


async def test_method_not_allowed_returns_com_4005(client):
    """PUT 打到一个只支持 GET/POST 的路由 -> COM-4005。"""
    r = await client.put("/api/v1/lists", json={}, headers=WRITER)
    assert r.status_code == 405
    body = _assert_envelope(r.json(), ok=False)
    assert body["code"] == "COM-4005"


async def test_malformed_json_body_returns_com_4000(client):
    """请求体不是合法 JSON -> COM-4000（400），区别于字段级校验的 COM-4001（422）。"""
    r = await client.post(
        "/api/v1/lists",
        content=b"{not-json",
        # 必须带有效令牌：否则会先被鉴权中间件拦成 401，测不到 JSON 解析这条路径
        headers={"Content-Type": "application/json", "X-Test-Role": "strategist"},
    )
    assert r.status_code == 400, r.text
    body = _assert_envelope(r.json(), ok=False)
    assert body["code"] == "COM-4000"


async def test_unhandled_exception_returns_com_5000_with_trace_id(tolerant_client, monkeypatch):
    """V-00-07：故意触发未捕获异常，断言响应含 trace_id 且**不回显堆栈**。"""
    import app.api.common_api as common_api

    def boom(_ms=None):
        raise RuntimeError("simulated internal explosion")

    monkeypatch.setattr(common_api, "now_ms", boom)
    r = await tolerant_client.get("/api/v1/common/meta")
    assert r.status_code == 500
    body = _assert_envelope(r.json(), ok=False)
    assert body["code"] == "COM-5000"
    assert "Traceback" not in r.text and "simulated internal explosion" not in r.text
    assert r.headers["X-Trace-Id"] == body["trace_id"]


async def test_rate_limit_returns_com_4290(client):
    """COM-4290 必须有真实触发路径，否则是"定义了但走不到"的死码。"""
    original = LIMITER.max_requests
    LIMITER.reset()
    LIMITER.max_requests = 2
    try:
        headers = {"X-Operator": "ratelimit_probe", "X-Role": "reviewer"}
        first = await client.get("/api/v1/common/meta", headers=headers)
        second = await client.get("/api/v1/common/meta", headers=headers)
        third = await client.get("/api/v1/common/meta", headers=headers)
        assert first.status_code == 200 and second.status_code == 200
        assert third.status_code == 429, "超过额度必须 429"
        body = _assert_envelope(third.json(), ok=False)
        assert body["code"] == "COM-4290"
        assert third.headers.get("Retry-After"), "429 必须给出 Retry-After"
    finally:
        LIMITER.max_requests = original
        LIMITER.reset()


async def test_health_is_exempt_from_rate_limit(client):
    """探针高频调用 /health 不应把用户配额打满，否则监控自身会造成故障。"""
    original = LIMITER.max_requests
    LIMITER.reset()
    LIMITER.max_requests = 1
    try:
        for _ in range(5):
            r = await client.get("/health")
            assert r.status_code == 200, "健康检查必须豁免限流"
    finally:
        LIMITER.max_requests = original
        LIMITER.reset()


async def test_health_reports_required_fields(client):
    """§3.3：/health 必须含 status / version / mongo{connected,latency_ms,db} /
    uptime_sec / time。"""
    r = await client.get("/health")
    assert r.status_code == 200
    data = _assert_envelope(r.json(), ok=True)["data"]
    assert data["status"] in ("ok", "degraded", "down")
    assert data["version"] == "0.1.0"
    assert set(data["mongo"]) >= {"connected", "latency_ms", "db"}
    assert isinstance(data["mongo"]["latency_ms"], int)
    assert isinstance(data["uptime_sec"], int) and data["uptime_sec"] >= 0
    assert data["time"].endswith("+08:00") or "T" in data["time"]
