# -*- coding: utf-8 -*-
"""模块 13 账号与角色管理的验收测试（Spec 13 §4.2 / §7 的 V-13-11 ~ V-13-17、V-13-20）。

覆盖四个成品交付物之一（`usCard`）背后的**全部业务不变量**，重点是任务书 §2①
与 §2② 的两个高危点：

* **`sys_users` 是认证表**：口令只能走 01 的 bcrypt 实现，改密后旧令牌必须失效，
  任何响应（含审计）都不许出现 `password_hash`；
* **不能把自己锁在门外**：不能停用/删除自己（`SYS-4005`）、不能把最后一个可用
  管理员降级（`SYS-4006`）——两者的**判定顺序**也被钉住。

| 用例 | 验收项 |
|---|---|
| `test_v13_11_account_crud_writes_exactly_one_audit_each` | V-13-11 全流程 + 每步恰好一条审计 |
| `test_created_account_can_login_and_reset_password_takes_effect` | 新建即可登录、改密后新口令生效 |
| `test_v13_12_duplicate_username` | V-13-12 `SYS-4004` |
| `test_v13_13_cannot_disable_or_delete_self` | V-13-13 `SYS-4005` |
| `test_v13_14_cannot_demote_last_admin` | V-13-14 `SYS-4006` |
| `test_v13_15_pending_cases_block_disable_and_can_be_transferred` | V-13-15 `SYS-4007` + 转交 |
| `test_v13_16_reset_password_invalidates_old_token` | V-13-16 旧令牌 401 |
| `test_v13_17_no_response_ever_leaks_password_hash` | V-13-17 / BR-13-20 |
| `test_delete_requires_disabled_and_is_soft` | BR-13-17 `SYS-4008` + 软删除 |
| `test_concurrent_modification_is_reported` | D42 乐观锁 `SYS-4009` |

运行：  .venv\\Scripts\\python.exe -m pytest tests/test_user_admin.py -q
"""
from __future__ import annotations

import pytest

from app import config, db
from app.constants import (
    COLL_AUDIT_LOGS,
    COLL_RISK_CASES,
    COLL_SYS_USERS,
    USER_STATUS_DELETED,
)
from app.repos.user_repo import UserRepo
from app.services import audit_service, user_admin_service
from app.services.user_admin_service import (
    build_user_admin_service,
    generate_password,
    public_fields,
)
from app.utils.timeutil import now_ms
from tests.conftest import ADMIN, READER, WRITER

pytestmark = pytest.mark.anyio

USERS_URL = f"{config.API_PREFIX}/system/users"
AUTH_URL = f"{config.API_PREFIX}/auth"

#: 本文件自建自清的测试账号（**绝不碰** admin01 / strategy01 / reviewer01）
T_REVIEWER = "t_rev_13"
T_ADMIN = "t_admin_13"
T_STRATEGIST = "t_str_13"
GOOD_PW = "initpw123456"


async def create_account(client, username: str, role: str = "reviewer",
                         password: str = GOOD_PW, real_name: str = "测试账号") -> dict:
    r = await client.post(USERS_URL, headers=ADMIN, json={
        "username": username, "real_name": real_name, "role": role,
        "initial_password": password,
    })
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def login(client, username: str, password: str) -> tuple[int, dict]:
    """登录并返回 `(status_code, body)`（body 一定是 JSON）。"""
    r = await client.post(f"{AUTH_URL}/login",
                          json={"username": username, "password": password})
    return r.status_code, r.json()


async def audit_rows(action: str) -> list[dict]:
    assert await audit_service.flush()
    return await db.get_db()[COLL_AUDIT_LOGS].find({"action": action}).to_list(50)


async def seed_pending_case(case_no: str, assignee: str) -> None:
    """给某审核员名下插一条"待处置"案件（`reviewing`，即已被他认领）。"""
    await db.get_db()[COLL_RISK_CASES].insert_one({
        "_id": case_no, "status": "reviewing", "assignee": assignee,
        "risk_level": "high", "user_id": "U000132", "created_at": now_ms(),
        "decision": "review", "final_score": 70,
    })


# ============================================================
# V-13-11 全流程 + 审计恰好一条
# ============================================================
async def test_v13_11_account_crud_writes_exactly_one_audit_each(client):
    """V-13-11 / BR-13-18 / D41：新增→改角色→停用→启用→重置→删除，每步一条审计。"""
    created = await create_account(client, T_REVIEWER, role="reviewer")
    assert created["username"] == T_REVIEWER
    assert created["generated_password"] is None, "传了初始密码就不该再生成一个"
    assert created["status"] == "active"
    assert (await audit_rows("account.create"))[0]["target_id"] == T_REVIEWER

    # 列表：新账号出现在里面，且带中文角色标签与 is_self 标记
    listing = (await client.get(USERS_URL, headers=ADMIN)).json()["data"]
    row = next(i for i in listing["items"] if i["username"] == T_REVIEWER)
    assert row["role_label"] == "风控审核员" and row["status_label"] == "启用"
    assert row["is_self"] is False
    me = next(i for i in listing["items"] if i["username"] == "admin01")
    assert me["is_self"] is True, "页面据此禁用「停用 / 删除」按钮"

    # 改姓名（account.update）
    r = await client.put(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN,
                         json={"real_name": "测试账号改名"})
    assert r.status_code == 200 and r.json()["data"]["changed"] is True
    assert r.json()["data"]["user"]["real_name"] == "测试账号改名"
    assert len(await audit_rows("account.update")) == 1

    # 改角色（account.role_change）
    r = await client.put(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN,
                         json={"role": "strategist"})
    assert r.status_code == 200 and r.json()["data"]["user"]["role"] == "strategist"
    assert len(await audit_rows("account.role_change")) == 1

    # 停用 / 启用（各一次）
    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "disabled"
    assert len(await audit_rows("account.disable")) == 1

    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/enable", headers=ADMIN)
    assert r.status_code == 200 and r.json()["data"]["status"] == "active"
    assert len(await audit_rows("account.enable")) == 1

    # 重置密码（account.reset_password）
    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/reset-password", headers=ADMIN,
                          json={"new_password": "resetpw123456"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["tokens_invalidated"] is True
    assert len(await audit_rows("account.reset_password")) == 1

    # 删除必须先停用（BR-13-17）
    r = await client.delete(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)
    assert r.status_code == 409 and r.json()["code"] == "SYS-4008"
    assert (await client.post(f"{USERS_URL}/{T_REVIEWER}/disable",
                              headers=ADMIN)).status_code == 200
    r = await client.delete(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["soft_deleted"] is True
    assert len(await audit_rows("account.delete")) == 1

    # 幂等：重复停用不写第二条审计
    await create_account(client, T_STRATEGIST, role="strategist")
    assert (await client.post(f"{USERS_URL}/{T_STRATEGIST}/disable",
                              headers=ADMIN)).status_code == 200
    before_count = len(await audit_rows("account.disable"))
    again = await client.post(f"{USERS_URL}/{T_STRATEGIST}/disable", headers=ADMIN)
    assert again.json()["data"]["changed"] is False
    assert len(await audit_rows("account.disable")) == before_count, (
        "重复点击不该产生第二条审计（account.disable 的数量必须原地不动）"
    )


async def test_created_account_can_login_and_reset_password_takes_effect(client):
    """**关键实测**：新建账号能用初始口令登录；重置后新口令可登录、旧口令不可。"""
    await create_account(client, T_REVIEWER, role="reviewer", password=GOOD_PW)

    code, body = await login(client, T_REVIEWER, GOOD_PW)
    assert code == 200, body
    assert body["data"]["user"]["role"] == "reviewer"

    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/reset-password", headers=ADMIN,
                          json={"new_password": "brandnew123456"})
    assert r.status_code == 200 and r.json()["data"]["generated_password"] is None

    assert (await login(client, T_REVIEWER, "brandnew123456"))[0] == 200
    code, _ = await login(client, T_REVIEWER, GOOD_PW)
    assert code == 401, "旧口令必须失效"


async def test_generated_password_is_returned_once_and_works(client):
    """BR-13-19：后端生成的口令 ≥12 位、只在响应里返回一次，且**真的能登录**。"""
    r = await client.post(USERS_URL, headers=ADMIN, json={
        "username": T_REVIEWER, "real_name": "自动口令", "role": "reviewer",
    })
    assert r.status_code == 200, r.text
    generated = r.json()["data"]["generated_password"]
    assert generated and len(generated) >= 12

    # 再查一次列表/详情：口令不会再出现
    detail = (await client.get(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)).json()["data"]
    assert "generated_password" not in detail
    assert generated not in str(detail)
    assert (await login(client, T_REVIEWER, generated))[0] == 200

    assert len(generate_password()) == 16
    assert len({generate_password() for _ in range(20)}) == 20, "必须是随机的"


async def test_short_password_is_rejected_with_auth_code(client):
    """BR-13-19：口令规则与 01 一致（过短 -> `AUTH-4006`，复用 01 的实现）。"""
    r = await client.post(USERS_URL, headers=ADMIN, json={
        "username": T_REVIEWER, "real_name": "短口令", "role": "reviewer",
        "initial_password": "12345",
    })
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "AUTH-4006"
    assert await db.get_db()[COLL_SYS_USERS].count_documents({"_id": T_REVIEWER}) == 0


# ============================================================
# V-13-12 唯一性
# ============================================================
async def test_v13_12_duplicate_username(client):
    """V-13-12：新增同名账号 -> `SYS-4004`（含与演示账号重名）。"""
    await create_account(client, T_REVIEWER)
    r = await client.post(USERS_URL, headers=ADMIN, json={
        "username": T_REVIEWER, "real_name": "重名", "role": "reviewer",
    })
    assert r.status_code == 409 and r.json()["code"] == "SYS-4004"

    r = await client.post(USERS_URL, headers=ADMIN, json={
        "username": "admin01", "real_name": "重名", "role": "admin",
    })
    assert r.status_code == 409 and r.json()["code"] == "SYS-4004"


@pytest.mark.parametrize("payload", [
    {"username": "ab", "real_name": "太短", "role": "reviewer"},          # 账号过短
    {"username": "bad name", "real_name": "非法字符", "role": "reviewer"},  # 账号含空格
    {"username": "ok_name1", "real_name": "角色非法", "role": "superman"},  # BR-13-12
    {"username": "ok_name2", "real_name": "", "role": "reviewer"},        # 姓名空白
])
async def test_username_role_validation(client, payload):
    """账号形态与角色取值域在模型层拦下（`COM-4001`，不新增自定义角色）。"""
    r = await client.post(USERS_URL, headers=ADMIN, json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "COM-4001"


# ============================================================
# V-13-13 / V-13-14 护栏
# ============================================================
async def test_v13_13_cannot_disable_or_delete_self(client):
    """V-13-13 / BR-13-13：当前管理员停用/删除自己 -> `SYS-4005`。

    判定**先于**"最后一个管理员"：种子环境下 `admin01` 是唯一管理员，
    两条规则同时成立，而契约（V-13-13）要求的是身份类的 `SYS-4005`。
    """
    r = await client.post(f"{USERS_URL}/admin01/disable", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "SYS-4005"
    assert "不能停用或删除当前登录账号" in r.json()["message"]

    r = await client.delete(f"{USERS_URL}/admin01", headers=ADMIN)
    assert r.status_code == 403 and r.json()["code"] == "SYS-4005"

    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": "admin01"})
    assert doc["status"] == "active" and doc["role"] == "admin"


async def test_v13_14_cannot_demote_last_admin(client):
    """V-13-14 / BR-13-14：把**最后一个可用管理员**降级 -> `SYS-4006`。

    ## 为什么用"降级"而不是"停用别人"来验这一条

    操作者本身必须是一个可用管理员，因此"停用/删除**别人**"在数学上不可能
    破坏"至少一个可用管理员"这条不变量（操作者就是那一个）。该码真正可达的
    形态有两种：**对自己降级**（任务书 §2② 明确列出"把最后一个管理员降级"），
    以及并发场景（见下一条用例）。前者正是这里验的。
    """
    r = await client.put(f"{USERS_URL}/admin01", headers=ADMIN, json={"role": "reviewer"})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "SYS-4006"
    assert r.json()["data"]["active_admins_after"] == 0

    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": "admin01"})
    assert doc["role"] == "admin", "被拒绝的降级绝不能写库"
    assert len(await audit_rows("account.role_change")) == 0


async def test_last_admin_guard_allows_demotion_when_another_admin_exists(client):
    """有第二个可用管理员时，降级是**允许**的（护栏不能宽到挡住正常操作）。

    验的是"自己降级自己"这一形态（唯一会被 BR-13-14 挡住的形态）。降级成功后
    **无法再用接口把自己改回来**（那一刻已经不是 admin 了，这正是护栏存在的
    意义），因此测试末尾用一次直接的库写入还原，并顺带断言这一事实。
    """
    await create_account(client, T_ADMIN, role="admin")
    r = await client.put(f"{USERS_URL}/admin01", headers=ADMIN, json={"role": "strategist"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["user"]["role"] == "strategist"

    # 降级后自己立刻失去 account:manage（权限以库中角色为准）
    denied = await client.get(USERS_URL, headers=ADMIN)
    assert denied.status_code == 403 and denied.json()["code"] == "AUTH-4020"

    # 还原（测试数据卫生；业务上应由另一个管理员来做这件事）
    await db.get_db()[COLL_SYS_USERS].update_one(
        {"_id": "admin01"}, {"$set": {"role": "admin"}}
    )
    assert (await client.get(USERS_URL, headers=ADMIN)).status_code == 200


async def test_last_admin_guard_also_checks_after_the_write(client, monkeypatch):
    """**写入后的复核**：并发下前置检查会双双通过，只有复核能兜住。

    造法：让"统计可用管理员"在**前置检查**时返回 1（假装还有别人），
    写入之后再返回 0——模拟"两个管理员同时停用对方"。此时必须把刚写入的
    停用**补偿回滚**并报 `SYS-4006`，否则系统会真的一个管理员都不剩。
    """
    await create_account(client, T_ADMIN, role="admin")
    real = UserRepo.count_active_admins
    calls = {"n": 0}

    async def _flaky(self, exclude=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return 1          # 前置检查：假装还有另一个管理员
        return 0              # 写入后复核：其实一个都不剩了

    monkeypatch.setattr(UserRepo, "count_active_admins", _flaky)
    r = await client.post(f"{USERS_URL}/{T_ADMIN}/disable", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "SYS-4006"

    monkeypatch.setattr(UserRepo, "count_active_admins", real)
    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": T_ADMIN})
    assert doc["status"] == "active", "复核失败必须把停用补偿回滚（不能留下半个结果）"


# ============================================================
# V-13-15 待处置案件与转交
# ============================================================
async def test_v13_15_pending_cases_block_disable_and_can_be_transferred(client):
    """V-13-15 / BR-13-15：有待处置案件 -> `SYS-4007` 且返回清单；转交后方可停用。"""
    await create_account(client, T_REVIEWER, role="reviewer")
    await seed_pending_case("CASE-13-0001", T_REVIEWER)
    await seed_pending_case("CASE-13-0002", T_REVIEWER)

    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "SYS-4007"
    assert body["data"]["pending_count"] == 2
    assert {c["case_no"] for c in body["data"]["cases"]} == {"CASE-13-0001", "CASE-13-0002"}

    # 接收人必须是**启用的 reviewer**：策略师与服务账号都不行
    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN,
                          json={"transfer_to": "strategy01"})
    assert r.status_code == 422 and r.json()["code"] == "COM-4001"
    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN,
                          json={"transfer_to": "no_such_user"})
    assert r.status_code == 422

    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN,
                          json={"transfer_to": "reviewer01", "reason": "离职交接"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["transferred_cases"] == 2
    assert r.json()["data"]["status"] == "disabled"

    rows = await db.get_db()[COLL_RISK_CASES].find({"assignee": "reviewer01"}).to_list(10)
    assert len(rows) == 2, "案件必须真的转交出去"
    assert all(row["status"] == "reviewing" for row in rows), (
        "转交只改归属，**不改状态**——退回待认领会让正在处置的人一刷新就丢了案件"
    )
    assert all(row["transferred_from"] == T_REVIEWER for row in rows)

    # 审计恰好一条，且把转交事实写在 after 里（BR-13-18 的可追溯）
    rows = await audit_rows("account.disable")
    assert len(rows) == 1
    assert rows[0]["after"]["transferred_cases"] == 2
    assert rows[0]["after"]["transfer_to"] == "reviewer01"


async def test_delete_with_pending_case_is_blocked_then_transferable(client):
    """BR-13-17：删除同样受"无待处置案件"限制（转交后即可删）。"""
    await create_account(client, T_REVIEWER, role="reviewer")
    await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    await seed_pending_case("CASE-13-0003", T_REVIEWER)   # 停用之后才出现的待办

    r = await client.delete(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)
    assert r.status_code == 409 and r.json()["code"] == "SYS-4007"

    # DELETE 带请求体：httpx 的 `delete()` 没有 `json=` 参数，用 request() 发
    r = await client.request("DELETE", f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN,
                             json={"transfer_to": "reviewer01"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == USER_STATUS_DELETED
    case = await db.get_db()[COLL_RISK_CASES].find_one({"_id": "CASE-13-0003"})
    assert case["assignee"] == "reviewer01"


# ============================================================
# V-13-16 改密后旧令牌失效
# ============================================================
async def test_v13_16_reset_password_invalidates_old_token(client):
    """V-13-16 / BR-13-16：重置后立即用旧 token 请求 -> 401。"""
    await create_account(client, T_REVIEWER, role="reviewer")
    token = (await login(client, T_REVIEWER, GOOD_PW))[1]["data"]["access_token"]
    old = {"Authorization": f"Bearer {token}"}

    assert (await client.get(f"{AUTH_URL}/me", headers=old)).status_code == 200

    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/reset-password", headers=ADMIN,
                          json={"new_password": "anotherpw12345"})
    assert r.status_code == 200
    assert r.json()["data"]["tokens_invalidated"] is True

    r = await client.get(f"{AUTH_URL}/me", headers=old)
    assert r.status_code == 401, r.text
    assert r.json()["code"] in ("AUTH-4008", "AUTH-4004"), (
        "改密导致令牌失效必须由 01 的既有机制报出（AUTH-4008），不能静默通过"
    )


async def test_delete_removes_account_from_auth_path_but_keeps_the_document(client):
    """BR-13-17：软删除后**登录链路等同于不存在**，但文档仍在库里可追溯。"""
    await create_account(client, T_REVIEWER, role="reviewer")
    token = (await login(client, T_REVIEWER, GOOD_PW))[1]["data"]["access_token"]
    await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    assert (await client.delete(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)).status_code == 200

    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": T_REVIEWER})
    assert doc is not None and doc["status"] == USER_STATUS_DELETED
    assert doc["deleted"] is True and doc["deleted_at"] and doc["deleted_by"] == "admin01"
    assert doc["password_hash"], "文档保留（哈希也在），否则审计无法回答「这个账号当时存在过」"

    # 登录：等同"账号不存在"（AUTH-4001，不泄露"这个账号存在但被删了"）
    code, body = await login(client, T_REVIEWER, GOOD_PW)
    assert code == 401
    assert body["code"] == "AUTH-4001"
    # 旧令牌：账号已不存在 -> 401（01 的既有处置）
    old = {"Authorization": f"Bearer {token}"}
    assert (await client.get(f"{AUTH_URL}/me", headers=old)).status_code == 401
    # 详情与列表：默认查不到（不再占着页面）
    assert (await client.get(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)).status_code == 404
    listing = (await client.get(USERS_URL, headers=ADMIN)).json()["data"]
    assert T_REVIEWER not in [i["username"] for i in listing["items"]]
    # 显式筛选软删除态仍能查到（审计可追溯）
    deleted = (await client.get(USERS_URL, params={"status": "deleted"},
                                headers=ADMIN)).json()["data"]
    assert T_REVIEWER in [i["username"] for i in deleted["items"]]


async def test_operations_on_unknown_or_deleted_account_are_404(client):
    """对不存在/已删除的账号做写操作 -> `COM-4004`（不制造幽灵账号的写入）。"""
    assert (await client.post(f"{USERS_URL}/ghost_1301/disable",
                              headers=ADMIN)).status_code == 404
    assert (await client.post(f"{USERS_URL}/ghost_1301/enable",
                              headers=ADMIN)).status_code == 404
    assert (await client.post(f"{USERS_URL}/ghost_1301/reset-password",
                              headers=ADMIN)).status_code == 404
    assert (await client.put(f"{USERS_URL}/ghost_1301", headers=ADMIN,
                             json={"real_name": "x"})).status_code == 404

    await create_account(client, T_REVIEWER)
    await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    await client.delete(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)
    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/enable", headers=ADMIN)
    assert r.status_code == 404, "已删除的账号不能再被启用（那会造出一个没人记得的活账号）"
    assert r.json()["code"] == "COM-4004"


# ============================================================
# V-13-17 响应不含口令哈希
# ============================================================
async def test_v13_17_no_response_ever_leaks_password_hash(client):
    """V-13-17 / BR-13-20：遍历账号相关接口，响应与审计里都不得出现 `password_hash`。"""
    await create_account(client, T_REVIEWER, role="reviewer")
    texts: list[str] = []
    texts.append((await client.get(USERS_URL, headers=ADMIN)).text)
    texts.append((await client.get(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)).text)
    texts.append((await client.put(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN,
                                   json={"real_name": "改名"})).text)
    texts.append((await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)).text)
    texts.append((await client.post(f"{USERS_URL}/{T_REVIEWER}/enable", headers=ADMIN)).text)
    texts.append((await client.post(f"{USERS_URL}/{T_REVIEWER}/reset-password",
                                    headers=ADMIN)).text)
    texts.append((await client.delete(f"{USERS_URL}/{T_REVIEWER}", headers=ADMIN)).text)

    for text in texts:
        assert "password_hash" not in text
        assert "$2b$" not in text, "bcrypt 哈希前缀同样不能出现"

    # 审计是对外可读界面：变更记录里同样不许出现哈希
    assert await audit_service.flush()
    rows = await db.get_db()[COLL_AUDIT_LOGS].find(
        {"target_id": T_REVIEWER}).to_list(50)
    assert rows, "本用例的操作必须留下了审计"
    for row in rows:
        assert "password_hash" not in str(row)
        assert "$2b$" not in str(row)
    assert "password_hash" not in str(public_fields(
        {"_id": "x", "password_hash": "$2b$12$abc", "role": "admin"}
    ))


# ============================================================
# D42 乐观锁
# ============================================================
async def test_concurrent_modification_is_reported(client, monkeypatch):
    """D42 / `SYS-4009`：条件更新匹配 0 条 -> 明确报冲突，不做后写覆盖。"""
    await create_account(client, T_REVIEWER)

    real = UserRepo.update_fields
    calls = {"n": 0}

    async def _lose_race(self, username, patch, *, expected=None):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return 0      # 模拟"我读到之后，别人先改了"
        return await real(self, username, patch, expected=expected)

    monkeypatch.setattr(UserRepo, "update_fields", _lose_race)
    r = await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "SYS-4009"

    monkeypatch.setattr(UserRepo, "update_fields", real)
    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": T_REVIEWER})
    assert doc["status"] == "active", "冲突时不得写入任何东西"
    assert len(await audit_rows("account.disable")) == 0


# ============================================================
# V-13-20 权限（账号接口仅 admin）
# ============================================================
@pytest.mark.parametrize("headers", [READER, WRITER])
async def test_v13_20_account_endpoints_are_admin_only(client, headers):
    """V-13-20：reviewer/strategist 调账号接口一律 403 `AUTH-4020`。"""
    for method, url, payload in [
        ("get", USERS_URL, None),
        ("get", f"{USERS_URL}/admin01", None),
        ("post", USERS_URL, {"username": "nobody02", "real_name": "x", "role": "reviewer"}),
        ("put", f"{USERS_URL}/reviewer01", {"role": "admin"}),
        ("post", f"{USERS_URL}/reviewer01/disable", None),
        ("post", f"{USERS_URL}/reviewer01/enable", None),
        ("post", f"{USERS_URL}/reviewer01/reset-password", {"new_password": "hack123456"}),
        ("delete", f"{USERS_URL}/reviewer01", None),
    ]:
        if method == "get":
            r = await client.get(url, headers=headers)
        elif method == "post":
            r = await client.post(url, headers=headers, json=payload)
        elif method == "put":
            r = await client.put(url, headers=headers, json=payload)
        else:
            r = await client.delete(url, headers=headers)
        assert r.status_code == 403, f"{method.upper()} {url} 竟然放行了：{r.text}"
        assert r.json()["code"] == "AUTH-4020"

    # 演示账号在任何情况下都不得被改动（E2E 全程依赖它们登录）
    doc = await db.get_db()[COLL_SYS_USERS].find_one({"_id": "reviewer01"})
    assert doc["status"] == "active" and doc["role"] == "reviewer"


# ============================================================
# 分页与筛选
# ============================================================
async def test_list_filters_and_pagination(client):
    """§3.4：`role`/`status` 筛选与分页契约（与全项目一致，模块 00 §3.2）。"""
    await create_account(client, T_REVIEWER, role="reviewer")
    await create_account(client, T_STRATEGIST, role="strategist")

    all_users = (await client.get(USERS_URL, headers=ADMIN)).json()["data"]
    # 演示账号三个（admin01/strategy01/reviewer01）+ 本用例新建的两个
    assert all_users["total"] == 5
    assert all_users["pages"] == 1
    assert all_users["role_counts"] == {"admin": 1, "reviewer": 2, "strategist": 2}

    only_reviewer = (await client.get(USERS_URL, params={"role": "reviewer"},
                                      headers=ADMIN)).json()["data"]
    assert {i["username"] for i in only_reviewer["items"]} == {T_REVIEWER, "reviewer01"}

    await client.post(f"{USERS_URL}/{T_REVIEWER}/disable", headers=ADMIN)
    disabled = (await client.get(USERS_URL, params={"status": "disabled"},
                                 headers=ADMIN)).json()["data"]
    assert [i["username"] for i in disabled["items"]] == [T_REVIEWER]
    assert disabled["items"][0]["status_label"] == "已停用"

    paged = (await client.get(USERS_URL, params={"page_size": 2, "page": 3},
                              headers=ADMIN)).json()["data"]
    assert paged["page"] == 3 and len(paged["items"]) == 1 and paged["pages"] == 3

    bad = await client.get(USERS_URL, params={"role": "superman"}, headers=ADMIN)
    assert bad.status_code == 422 and bad.json()["code"] == "COM-4001"
    bad = await client.get(USERS_URL, params={"page": 0}, headers=ADMIN)
    assert bad.status_code == 422
    bad = await client.get(USERS_URL, params={"page_size": 101}, headers=ADMIN)
    assert bad.status_code == 422


async def test_service_level_guards_are_reachable_without_http(client):
    """服务层护栏不依赖接口层（可单测，Spec §6 的"可单测"要求）。"""
    service = build_user_admin_service(db.get_db())

    # 自己 -> SYS-4005（服务层同样拦得住）
    from app.errors import SelfOperationForbiddenError

    with pytest.raises(SelfOperationForbiddenError):
        await service.disable_user("admin01", operator="admin01")

    # 口令过长（bcrypt 的 72 字节上限）由 01 的实现拒绝
    from app.errors import WeakPasswordError

    with pytest.raises(WeakPasswordError):
        await service.create_user(
            type("P", (), {"username": "t_longpw1", "real_name": "长口令",
                           "role": "reviewer", "initial_password": "字" * 30})(),
            operator="admin01",
        )
    assert await db.get_db()[COLL_SYS_USERS].count_documents({"_id": "t_longpw1"}) == 0


async def test_role_label_helper_reads_enum():
    """角色中文标签取自枚举（BR-00-18 的单一来源），不是各处硬编码。"""
    assert user_admin_service.role_label("admin") == "系统管理员"
    assert user_admin_service.role_label("strategist") == "风控策略师"
    assert user_admin_service.role_label("weird") == "weird", "脏数据回落成原值便于排查"
    assert user_admin_service.actor_of({"_id": "u1", "role": "admin"}) == ("u1", "admin")
