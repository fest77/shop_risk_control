# -*- coding: utf-8 -*-
"""模块 01 登录与权限鉴权验收测试（V-01-01 ~ V-01-14）。

覆盖三类东西：
1. **验收项**：登录、错误统一、停用、bcrypt 存储、令牌过期、改密失效、越权 403、
   越权留痕、职责分离、依赖故障 fail-closed
2. **安全回归**：`X-Operator`/`X-Role` 自称身份的后门必须已关闭
3. **边界**：超长口令、恒定提示文案、锁定与解锁、审计哈希链连续性

V-01-08（无权限菜单不渲染）与 V-01-13（已登录访问登录页被重定向）属浏览器行为，
由工作区的 `check_frontend_e2e.js` 用真实浏览器验证。
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest
from pymongo.errors import PyMongoError

from app import config, db
from app.constants import COLL_AUDIT_LOGS, COLL_SYS_USERS
from app.engine.audit_hash import GENESIS_HASH
from app.security.jwt import create_access_token
from app.security.password import MAX_PASSWORD_BYTES, hash_password, verify_password
from app.security.permissions import (
    ALL_PERMISSIONS,
    MENU_PERMISSIONS,
    PERMISSION_MATRIX,
    SHARED_PERMISSIONS,
    menu_for,
    permissions_for,
    roles_for,
)
from app.services import audit_service
from tests.conftest import READER, TEST_ROLE_USER, WRITER

pytestmark = pytest.mark.anyio

LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
PASSWORD_URL = "/api/v1/auth/password"
LOGOUT_URL = "/api/v1/auth/logout"
LISTS_URL = "/api/v1/lists"

STRATEGIST = TEST_ROLE_USER["strategist"][:2]
REVIEWER = TEST_ROLE_USER["reviewer"][:2]
ADMIN = TEST_ROLE_USER["admin"][:2]


# ============================================================ V-01-01 正常登录
async def test_login_success_returns_token_and_user(login):
    status, body = await login(*STRATEGIST)
    assert status == 200, body
    assert body["ok"] is True and body["code"] == "OK"
    data = body["data"]
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == config.JWT_EXPIRE_MINUTES * 60
    assert data["access_token"].count(".") == 2, "应为三段式 JWT"
    user = data["user"]
    assert user["username"] == STRATEGIST[0]
    assert user["role"] == "strategist"
    assert user["role_label"] == "风控策略师"
    assert "password_str" not in str(user) and "password_hash" not in user


async def test_login_updates_last_login_at(login):
    """BR-01-04：登录成功更新 last_login_at。"""
    await login(*STRATEGIST)
    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": STRATEGIST[0]})
    assert isinstance(doc.get("last_login_at"), int) and doc["last_login_at"] > 0


async def test_me_returns_permissions_and_session_time(client, bearer):
    """§3.2：/auth/me 下发 permissions 与本次会话登录时间。"""
    headers = await bearer("strategist")
    r = await client.get(ME_URL, headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["permissions"] == permissions_for("strategist")
    assert isinstance(data["login_at"], int) and data["login_at"] > 0
    assert data["token_expires_at"] > data["login_at"]


async def test_logout_acknowledges(client, bearer):
    """§3.3：无状态 JWT，服务端只确认登出（真正清 token 在前端）。"""
    r = await client.post(LOGOUT_URL, headers=await bearer("reviewer"))
    assert r.status_code == 200
    assert r.json()["data"]["logged_out"] is True


# ============================================================ V-01-02 错误统一
async def test_wrong_password_and_unknown_user_are_indistinguishable(login):
    """BR-01-02：不区分"账号不存在"与"密码错误"（防账号枚举）。

    这条**只断言文案一致是不够的**：若账号不存在时直接返回，响应会快一个 bcrypt
    量级，攻击者用时间差照样能枚举。因此同时断言两者都返回同一个错误码与文案。
    """
    s1, b1 = await login(STRATEGIST[0], "definitely-wrong-password")
    s2, b2 = await login("nosuchuser99", "definitely-wrong-password")
    assert s1 == s2 == 401
    assert b1["code"] == b2["code"] == "AUTH-4001"
    assert b1["message"] == b2["message"] == "账号或密码错误"


async def test_password_must_not_leak_into_response(login):
    status, body = await login(*STRATEGIST)
    assert status == 200
    assert STRATEGIST[1] not in str(body), "响应里绝不能出现明文口令"


# ============================================================ V-01-03 停用账号
async def test_disabled_account_cannot_login_even_with_correct_password(login):
    """BR-01-03：status=disabled 时即使密码正确也拒绝，返回 AUTH-4010。"""
    await db.get_db()[COLL_SYS_USERS].update_one(
        {"_id": REVIEWER[0]}, {"$set": {"status": "disabled"}}
    )
    status, body = await login(*REVIEWER)
    assert status == 403
    assert body["code"] == "AUTH-4010"


async def test_disabled_account_existing_token_is_rejected(client, bearer):
    """已签发的令牌在账号被停用后必须**立即失效**（回源查库的意义所在）。"""
    headers = await bearer("reviewer")
    assert (await client.get(ME_URL, headers=headers)).status_code == 200

    await db.get_db()[COLL_SYS_USERS].update_one(
        {"_id": REVIEWER[0]}, {"$set": {"status": "disabled"}}
    )
    r = await client.get(ME_URL, headers=headers)
    assert r.status_code == 403
    assert r.json()["code"] == "AUTH-4010"


# ============================================================ V-01-04 bcrypt 存储
async def test_password_stored_as_bcrypt_hash(login):
    """BR-01-01：库中绝不存明文，且形如 `$2b$...`。"""
    await login(*ADMIN)
    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": ADMIN[0]})
    stored = doc["password_hash"]
    assert stored.startswith("$2b$"), stored
    assert stored != ADMIN[1]
    assert ADMIN[1] not in str(doc), "整条文档里都不应出现明文口令"
    assert verify_password(ADMIN[1], stored) is True


async def test_password_verification_rejects_overlong_input():
    """bcrypt 只取前 72 字节：超长口令必须**拒绝**而不是静默截断。

    若静默截断，两个前 72 字节相同的不同口令会互相通过——等于多了一把钥匙。
    """
    long_password = "A" * (MAX_PASSWORD_BYTES + 10)  # noqa: secret - 故意超长，用于验证哈希拒绝
    with pytest.raises(Exception):
        hash_password(long_password)
    assert verify_password(long_password, hash_password("A" * 10)) is False


# ============================================================ V-01-05 令牌过期
async def test_expired_token_rejected(client):
    """BR-01-09：必须同时验签名与过期时间。"""
    token, _ = create_access_token(
        STRATEGIST[0], "strategist", now=int(time.time()) - 100_000, expire_minutes=1
    )
    r = await client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH-4004"


async def test_token_without_exp_is_rejected(client):
    """缺少 `exp` 的令牌等于永不过期，必须拒绝（而非默认放行）。"""
    import jwt as pyjwt

    token = pyjwt.encode({"sub": STRATEGIST[0], "role": "strategist", "iat": int(time.time())},
                         config.JWT_SECRET, algorithm="HS256")
    r = await client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["code"] in ("AUTH-4002", "AUTH-4003")


# ============================================================ V-01-06 改密失效
async def test_password_change_invalidates_old_token(client, login):
    """BR-01-10：改密后旧令牌立即失效（比对 password_changed_at 与 token.iat）。"""
    status, body = await login(*STRATEGIST)
    assert status == 200
    old_headers = {"Authorization": f"Bearer {body['data']['access_token']}"}
    assert (await client.get(ME_URL, headers=old_headers)).status_code == 200

    r = await client.post(PASSWORD_URL, json={
        "old_password": STRATEGIST[1], "new_password": "newpass123",
    }, headers=old_headers)
    assert r.status_code == 200 and r.json()["data"]["changed"] is True

    r2 = await client.get(ME_URL, headers=old_headers)
    assert r2.status_code == 401, "改密后旧令牌必须失效"
    assert r2.json()["code"] == "AUTH-4008"

    # 新口令可用
    s3, b3 = await login(STRATEGIST[0], "newpass123")
    assert s3 == 200 and b3["ok"] is True


async def test_change_password_validations(client, bearer):
    """AUTH-4007 原密码错；AUTH-4006 新密码不合规。"""
    headers = await bearer("strategist")
    r = await client.post(PASSWORD_URL, json={
        "old_password": "wrong-old", "new_password": "whatever123",
    }, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "AUTH-4007"

    r2 = await client.post(PASSWORD_URL, json={
        "old_password": STRATEGIST[1], "new_password": "abc",
    }, headers=headers)
    assert r2.status_code == 422 and r2.json()["code"] == "AUTH-4006"

    r3 = await client.post(PASSWORD_URL, json={
        "old_password": STRATEGIST[1], "new_password": STRATEGIST[1],
    }, headers=headers)
    assert r3.status_code == 422 and r3.json()["code"] == "AUTH-4006"


# ============================================================ V-01-07 三角色权限差异
async def test_three_roles_have_expected_permissions_and_menus(client, bearer):
    """§2.2 矩阵落地：权限差异与菜单差异（前端菜单即由此渲染）。"""
    expected_menus = {
        "reviewer": ["#/dashboard", "#/cases", "#/sim"],
        "strategist": ["#/dashboard", "#/rules", "#/sim"],
        "admin": ["#/dashboard", "#/audit", "#/settings"],
    }
    for role, menus in expected_menus.items():
        r = await client.get(ME_URL, headers=await bearer(role))
        perms = r.json()["data"]["permissions"]
        assert perms == permissions_for(role), f"{role} 的权限串与矩阵不一致"
        assert menu_for(perms) == menus, f"{role} 的可见菜单不符：{menu_for(perms)}"


async def test_menu_permissions_cover_all_routes():
    """菜单表引用的权限必须在矩阵里定义（防止路由声明了不存在的权限）。"""
    assert set(MENU_PERMISSIONS.values()) <= set(ALL_PERMISSIONS)


# ============================================================ V-01-09 后端越权 403
async def test_reviewer_cannot_write_lists(client):
    """BR-01-14：后端必须独立校验——即使绕过前端也拿不到写权限。"""
    r = await client.post(LISTS_URL, json={
        "list_type": "black", "entity_type": "device",
        "entity_value": "AUTH-PROBE-1", "reason": "越权测试",
    }, headers=READER)
    assert r.status_code == 403
    assert r.json()["code"] == "AUTH-4020"


async def test_admin_cannot_touch_rules_or_lists(client):
    """矩阵裁定（D26）：admin 不具备 rule:write / list:write。"""
    for perm in ("rule:write", "list:write"):
        assert roles_for(perm) == ("strategist",), f"{perm} 应只属于 strategist"
    r = await client.post(LISTS_URL, json={
        "list_type": "black", "entity_type": "device",
        "entity_value": "AUTH-PROBE-2", "reason": "越权测试",
    }, headers={"X-Test-Role": "admin"})
    assert r.status_code == 403 and r.json()["code"] == "AUTH-4020"


# ============================================================ V-01-10 越权留痕
async def test_denied_write_is_audited(client):
    """BR-01-15 / BR-12-19：权限不足的**写**操作必须留痕（action=auth.denied）。

    审计写入是"入队不等待"（BR-12-07 的 `strict=False`），因此断言前必须先
    `flush()` 把队列排空——这不是测试技巧，而是该契约的真实语义：
    业务请求返回时，审计只保证**已入队**，不保证已落库。
    """
    await client.post(LISTS_URL, json={
        "list_type": "black", "entity_type": "device",
        "entity_value": "AUDIT-PROBE", "reason": "越权留痕测试",
    }, headers=READER)
    assert await audit_service.flush(), "审计队列未能在超时内排空"

    doc = await db.get_db()[COLL_AUDIT_LOGS].find_one({"action": "auth.denied"})
    assert doc is not None, "越权写操作必须留下审计记录"
    assert doc["actor"] == REVIEWER[0]
    assert doc["actor_role"] == "reviewer"
    assert doc["target_id"] == "list:write", "BR-12-19：必须带目标信息，否则看不出他想干什么"
    assert doc["after"]["method"] == "POST"
    assert doc["prev_hash"] == GENESIS_HASH, "首条记录的 prev_hash 必须是创世哈希"
    assert len(doc["hash"]) == 64 and doc["_id"].startswith("LOG")


async def test_permitted_read_is_not_audited(client):
    """**被允许**的读操作不留痕：否则正常运行会把审计表刷满，淹没真正的越权尝试。

    注意与"越权读要留痕"区分（BR-12-24 要求越权访问审计接口本身也记
    `auth.denied`）——两者的判据是"是否被拒绝"，而不是"是不是写操作"。
    """
    r = await client.get("/api/v1/lists", params={"list_type": "black"}, headers=READER)
    assert r.status_code == 200, "list:read 对审核员开放，本用例只确认正常读取不写审计"
    assert await audit_service.flush()
    assert await db.get_db()[COLL_AUDIT_LOGS].count_documents({"action": "auth.denied"}) == 0


async def test_audit_hash_chain_is_linked(client):
    """E16 / BR-12-01/05：连续两条记录的 prev_hash 必须指向前一条的 hash。

    若链断了，"不可篡改"这个卖点就不成立——单条记录改了也对不上任何后续。
    """
    for i in range(2):
        await client.post(LISTS_URL, json={
            "list_type": "black", "entity_type": "device",
            "entity_value": f"CHAIN-{i}", "reason": "链测试",
        }, headers=READER)
    assert await audit_service.flush()

    rows = await db.get_db()[COLL_AUDIT_LOGS].find({}).sort("$natural", 1).to_list(length=10)
    assert len(rows) == 2
    assert rows[0]["prev_hash"] == GENESIS_HASH, "第一条必须接创世哈希（BR-12-01）"
    assert rows[1]["prev_hash"] == rows[0]["hash"], "第二条必须接在第一条之后"


async def test_login_events_are_audited(client, login):
    """BR-12-17：`auth.login` / `auth.logout` 必须记录（`strict=False`）。

    成功与失败都记：动作名保持 `auth.login`，用 `after.result` 区分——
    这样"有人在爆破这个账号"在审计里看得见，而动作清单不会膨胀。
    """
    await login(*STRATEGIST)
    await login(STRATEGIST[0], "wrong-password")
    await client.post(LOGOUT_URL, headers=WRITER)
    assert await audit_service.flush()

    col = db.get_db()[COLL_AUDIT_LOGS]
    assert await col.count_documents({"action": "auth.login", "after.result": "success"}) >= 1
    assert await col.count_documents({"action": "auth.login", "after.result": "failed"}) == 1
    assert await col.count_documents({"action": "auth.logout"}) >= 1


# ============================================================ V-01-11 职责分离
async def test_duties_are_separated():
    """BR-01-13：除共享权限外，每项权限只能属于**一个**角色。

    这是"三权分立"不被悄悄破坏的守卫：将来有人给 admin 顺手加上 rule:write，
    这条断言立刻变红，而不是等到验收时才发现权限边界已经糊了。
    """
    for perm, roles in PERMISSION_MATRIX.items():
        if perm in SHARED_PERMISSIONS:
            continue
        assert len(roles) == 1, f"{perm} 被授予多个角色：{roles}"

    # 三个角色的"独占能力"两两无交集
    exclusive = {
        role: {p for p in permissions_for(role) if p not in SHARED_PERMISSIONS}
        for role in ("reviewer", "strategist", "admin")
    }
    for a in exclusive:
        for b in exclusive:
            if a < b:
                assert not (exclusive[a] & exclusive[b]), f"{a} 与 {b} 的独占能力有交集"
    assert all(exclusive.values()), "每个角色都应至少有一项独占能力"


# ============================================================ V-01-12 依赖故障 fail-closed
class _BoomCollection:
    """所有操作都抛 PyMongoError 的假集合（模拟用户库不可用）。"""

    async def find_one(self, *args, **kwargs):
        raise PyMongoError("simulated user db down")

    async def update_one(self, *args, **kwargs):
        raise PyMongoError("simulated user db down")

    async def count_documents(self, *args, **kwargs):
        raise PyMongoError("simulated user db down")


class _BoomDb:
    def __getitem__(self, _name):
        return _BoomCollection()


def _break_user_db(monkeypatch):
    """让"拿数据库"这件事直接失败。

    **刻意替换底层数据库句柄、而不是替换 `UserRepo.find_by_username`**：
    仓储的 try/except 正是把 PyMongoError 转成 `AUTH-5001` 的地方，若把整个方法
    替换掉，就等于把被测的容错逻辑一起删掉了——测试会变成"验证 mock 会抛异常"。
    这里让真实代码路径跑起来，只是底层 IO 失败。
    """
    monkeypatch.setattr(db, "get_db", lambda: _BoomDb())
    # auth_api 在 import 时把 get_db 绑定进了自己的命名空间，需一并替换
    import app.api.auth_api as auth_api

    monkeypatch.setattr(auth_api, "get_db", lambda: _BoomDb())


async def test_auth_dependency_failure_is_503_not_bypass(client, monkeypatch):
    """AUTH-5001：用户库查询失败时**拒绝**，绝不放行。"""
    _break_user_db(monkeypatch)
    r = await client.get(ME_URL, headers=WRITER)
    assert r.status_code == 503, "鉴权依赖不可用必须 503，不能放行"
    assert r.json()["code"] == "AUTH-5001"


async def test_login_fails_closed_when_user_db_down(login, monkeypatch):
    """登录同样 fail-closed：库挂了不能因为"查不到用户"就当成"可以登录"。"""
    _break_user_db(monkeypatch)
    status, body = await login(*STRATEGIST)
    assert status == 503 and body["code"] == "AUTH-5001"


# ============================================================ V-01-14 密钥缺失
async def test_missing_or_short_jwt_secret_fails_fast():
    """AUTH-5002：JWT 密钥缺失或过短都必须在启动期失败并打印键名。"""
    code = (
        "import dotenv;dotenv.load_dotenv=lambda *a,**k: None;"
        "import app.config as c;"
        "c.validate();print('SHOULD_NOT_REACH')"
    )
    for secret in (None, "tooshort"):
        env = {**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONIOENCODING": "utf-8"}
        if secret is None:
            env.pop("JWT_SECRET", None)
        else:
            env["JWT_SECRET"] = secret
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(config.ROOT), env=env,
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=90)
        assert proc.returncode != 0, f"JWT_SECRET={secret!r} 时不应启动成功"
        output = (proc.stdout or "") + (proc.stderr or "")
        assert "JWT_SECRET" in output and "SHOULD_NOT_REACH" not in output


# ============================================================ 安全回归：旧后门已关闭
async def test_legacy_identity_headers_are_ignored(client):
    """阶段一的 `X-Operator` / `X-Role` 自称身份必须**彻底失效**。

    这是最关键的一条安全回归：如果这两个头还能决定身份，任何人都能通过改一个
    请求头冒充系统管理员，整套权限体系形同虚设。
    """
    r = await client.get(ME_URL, headers={"X-Operator": "admin01", "X-Role": "admin"})
    assert r.status_code == 401, "自称身份的头必须被忽略"
    assert r.json()["code"] == "AUTH-4002"

    r2 = await client.post(LISTS_URL, json={
        "list_type": "black", "entity_type": "device",
        "entity_value": "BACKDOOR-PROBE", "reason": "后门测试",
    }, headers={"X-Operator": "admin01", "X-Role": "admin"})
    assert r2.status_code == 401, "写接口同样不能接受自称身份"


async def test_whitelist_paths_need_no_token(client):
    """§3.5 白名单：健康检查与公共枚举无需登录；业务接口则必须登录。"""
    for path in ("/health", "/api/v1/common/enums", "/api/v1/common/meta"):
        assert (await client.get(path)).status_code == 200, path
    for path in (ME_URL, "/api/v1/lists?list_type=black"):
        assert (await client.get(path)).status_code == 401, path


async def test_malformed_authorization_header(client):
    """Authorization 头格式非法 -> AUTH-4002。"""
    for value in ("", "Token abc", "Bearer", "Basic dXNlcjpwYXNz"):
        r = await client.get(ME_URL, headers={"Authorization": value})
        assert r.status_code == 401, value
        assert r.json()["code"] == "AUTH-4002", value


# ============================================================ BR-01-05 登录锁定
async def test_login_lockout_after_configured_failures(login):
    """BR-01-05：连续失败达阈值即锁定，且锁定期内**即使口令正确也拒绝**。

    阈值从 `config` 读取而不是写死数字：写死会让"调整阈值"变成"顺手改测试"，
    而阈值恰恰是被安全策略决定的东西，不该被顺手改掉。
    """
    limit = config.LOGIN_MAX_FAILURES
    assert limit >= 2, "阈值低于 2 时下面的边界推断不成立"

    for i in range(limit - 1):
        status, body = await login(REVIEWER[0], "wrong-password")
        assert status == 401 and body["code"] == "AUTH-4001", f"第 {i + 1} 次应为普通失败"

    status_n, body_n = await login(REVIEWER[0], "wrong-password")
    assert status_n == 429 and body_n["code"] == "AUTH-4005", f"第 {limit} 次失败应触发锁定"

    status_after, body_after = await login(*REVIEWER)
    assert status_after == 429 and body_after["code"] == "AUTH-4005", \
        "锁定期内正确口令也必须拒绝"


async def test_successful_login_clears_failure_count(login):
    """BR-01-05 只针对"连续"失败：成功一次即清零。"""
    probes = min(4, config.LOGIN_MAX_FAILURES - 1)
    for _ in range(probes):
        await login(REVIEWER[0], "wrong-password")
    status, _ = await login(*REVIEWER)
    assert status == 200, "成功登录必须清零失败计数"
    for i in range(probes):   # 清零后应还能再来若干次普通失败
        s, b = await login(REVIEWER[0], "wrong-password")
        assert s == 401 and b["code"] == "AUTH-4001", f"清零后第 {i + 1} 次应仍是普通失败"


# ============================================================ 请求模型边界
async def test_login_request_validation(client):
    """§3.1：账号形态与密码长度由模型层校验 -> COM-4001（422）。"""
    for payload in (
        {"username": "ab", "password": "123456"},            # 账号过短
        {"username": "has space", "password": "123456"},      # 账号含非法字符
        {"username": "okname", "password": "123"},            # 密码过短
    ):
        r = await client.post(LOGIN_URL, json=payload)
        assert r.status_code == 422, payload
        assert r.json()["code"] == "COM-4001", payload
