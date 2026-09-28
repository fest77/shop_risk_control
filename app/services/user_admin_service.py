# -*- coding: utf-8 -*-
"""账号与角色管理服务（模块 13 §4.2）——**本模块是 E19 `sys_users` 的唯一写入方**。

## 这一层动的是"登录与权限的地基"

`sys_users` 是模块 01 的认证数据源，因此本文件的每一条规则都在防同一类事故：
**改完账号之后，要么有人进不来，要么不该进来的人进来了**。

| 编号 | 规则 | 本文件怎么落地 |
|---|---|---|
| BR-13-10 | 唯一写入方；01 只读 | 写路径只在本模块，密码一律走 `security.password.hash_password`（bcrypt，D22：**不是 passlib**） |
| BR-13-11 | `username` 全局唯一、创建后不可改 | `_id` 主键 + `insert_one` 撞键即 `SYS-4004`；`UserUpdateIn` 里**没有** username 字段 |
| BR-13-12 | 角色仅 `reviewer/strategist/admin` | 取值域来自 `app/enums.Role`（模型层 `Literal` + 服务层再验一次） |
| BR-13-13 | 不允许停用/删除自己 | `SYS-4005`（身份判断，**先于**数量判断） |
| BR-13-14 | 必须保留至少一个可用管理员 | `SYS-4006`（数量判断；含写入后的复核补偿，见 `_assert_admin_remains`） |
| BR-13-15 | 停用前名下未处置案件需转交 | `SYS-4007` + 返回案件清单；给 `transfer_to` 则先转交 |
| BR-13-16 | 重置密码后旧令牌立即失效 | 写 `password_changed_at`，01 的 `load_user_for_token` 据此判失效 |
| BR-13-17 | 删除仅限已停用且无待处置案件的账号，且是**软删除** | `SYS-4008` / `SYS-4007` / `status=deleted` |
| BR-13-18 | 所有变更写审计且 `strict=True` | 每个写路径**恰好一条**（D41）；写不进去就回滚 |
| BR-13-19 | 口令 6~64；后端生成 ≥12 位且只返回一次 | 复用 `AuthService.validate_new_password`（**不复制规则**）+ `generate_password` |
| BR-13-20 | 任何响应都不含 `password_hash` | 对外模型 `UserOut` 里没有这个字段；**审计的 `after` 里也不放哈希**（审计页同样是对外界面） |

## 三条"不能把自己锁在门外"的护栏（任务书 §2②）

1. **不能停用/删除自己**（`SYS-4005`）——`SYS-4005` 优先于 `SYS-4006`：种子里
   `admin01` 是唯一管理员，"停用自己"同时满足两条规则，而 V-13-13 要求的正是
   `SYS-4005`（身份问题比数量问题更根本）。
2. **不能把最后一个可用管理员降级/停用**（`SYS-4006`）。注意一个容易被忽略的
   事实：**操作者本身必须是一个可用管理员**，所以"停用/删除别人"永远不可能
   破坏"至少一个可用管理员"这条不变量——`SYS-4006` 唯一可达的形态是
   **对自己降级**（任务书 §2② 明确列出"把最后一个管理员降级"）。因此本服务
   既做**前置检查**（读一次计数），也在写入之后**复核一次**并把结果补偿回去：
   两个管理员互相停用（并发）时，前置检查会双双通过，只有事后复核能兜住。
3. **软删除而不是物理删除**（BR-13-17）：删除是 `status=deleted`，文档保留，
   `UserRepo.find_by_username` 会把这类账号从**鉴权链路**里过滤掉（对登录与
   令牌回源等同于"不存在"），而账号列表按需仍可查到它——审计可追溯。

## 为什么"无变化"的操作不写审计

与模块 06 的启停用幂等（BR-06-03 / V-06-20）同一条裁定：重复点击"停用"若每次
都写一条 `account.disable`，审计链会被无意义的记录淹没（而审计的价值恰恰在于
"翻得到真正改了什么"）。因此目标状态与当前一致时返回 `changed=false`，
不写库也不写审计。
"""
from __future__ import annotations

import secrets
from typing import Any, Optional

from app.constants import (
    GENERATED_PASSWORD_LEN,
    USER_STATUS_DELETED,
)
from app.enums import Role, label_of
from app.errors import (
    AppError,
    AccountVersionConflictError,
    AccountWriteFailedError,
    DisableBeforeDeleteError,
    LastAdminProtectedError,
    NotFoundError,
    PendingCasesError,
    SelfOperationForbiddenError,
    UsernameConflictError,
)
from app.logging import get_logger
from app.repos.case_transfer_repo import CaseTransferRepo
from app.repos.user_repo import UserRepo
from app.schemas.system_schema import ROLES, USER_STATUSES, UserOut
from app.security.password import hash_password
from app.services.audit_service import audit
from app.services.auth_service import AuthService
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.system.user")

#: 生成口令用的字符集。**刻意剔除易混字符**（`0/O`、`1/l/I`）：
#: 初始口令是要被管理员念给同事 / 抄进便签的，一个"看起来像 O 的 0"会让
#: 首次登录失败，而失败原因（口令错误）根本指不到真正的问题上。
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: 账号不存在（Spec §5 未给该场景分配 `SYS-` 码，因此复用模块 00 的 `COM-4004`）
_NOT_FOUND_MESSAGE = "账号不存在或已被删除"

#: 分页越界（同上：复用 `COM-4001`，与模块 12 的 `AUD-4001` 是各自模块的码）
_PAGE_ERROR = "COM-4001"

#: 不可由客户端指定、也不该出现在审计里的字段（E19 的凭据列）
SECRET_FIELDS: tuple[str, ...] = ("password_hash",)


def generate_password(length: int = GENERATED_PASSWORD_LEN) -> str:
    """生成初始口令（BR-13-19：**足够随机**且 ≥12 位）。

    用 `secrets`（CSPRNG）而不是 `random`：`random` 是可预测的 Mersenne
    Twister，用它可以据"已发出的初始口令"推断后续口令——而初始口令对应的
    是**尚未改密的活账号**。长度取 16（>12）：字符集 57 个，16 位的组合数
    远超暴力破解范围。
    """
    size = max(12, int(length))
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(size))


def public_fields(doc: dict) -> dict:
    """文档 -> 可写入审计/响应的公开字段（**绝不含 `password_hash`**）。

    审计的 `after` 与接口响应共用这一处：审计页在浏览器里是**可读界面**，
    把哈希写进 `audit_logs.after` 等于把凭据暴露给所有能看审计的人
    （BR-13-20 的"任何接口响应"应当按同一精神理解为"任何对外可见内容"）。
    """
    out = {k: v for k, v in doc.items() if k not in SECRET_FIELDS and k != "_id"}
    out["username"] = str(doc.get("_id") or doc.get("username") or "")
    return out


def actor_of(user: dict) -> tuple[str, str]:
    """从库中用户文档取 `(actor, actor_role)`（审计用）。"""
    return (
        str(user.get("_id") or user.get("username") or "unknown"),
        str(user.get("role", "")),
    )


class UserAdminService:
    """账号 CRUD + 保护规则。"""

    def __init__(self, repo: UserRepo, cases: CaseTransferRepo):
        # `cases` **不给默认值**：它是"停用前案件转交"（BR-13-15）这条安全护栏
        # 的唯一实现，允许传 `None` 就等于给了一条"静默跳过护栏"的路——
        # 那种口子不会在测试里暴露，只会在线上以"案件挂在停用账号名下"出现
        self.repo = repo
        self.cases = cases

    # ---------------- 读 ----------------
    async def list_users(
        self,
        *,
        role: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        current_user: str = "",
    ) -> dict:
        """`GET /system/users`（§3.4：列表 + `role`/`status` 筛选 + 分页）。

        默认**不返回软删除的账号**（`status=deleted` 需要显式筛），与"删掉的
        账号不该继续占着页面"一致；但它并没有消失，`status=deleted` 能查到
        （BR-13-17 的"保留审计可追溯"）。
        """
        from app import constants

        if page < 1 or page > constants.PAGE_MAX:
            raise AppError(_PAGE_ERROR, f"page 必须在 1~{constants.PAGE_MAX} 之间", 422)
        if not (1 <= page_size <= constants.PAGE_SIZE_MAX):
            raise AppError(
                _PAGE_ERROR, f"page_size 必须在 1~{constants.PAGE_SIZE_MAX} 之间", 422
            )
        if role and role not in ROLES:
            raise AppError(_PAGE_ERROR, f"role 仅支持 {'/'.join(ROLES)}", 422)
        if status and status not in USER_STATUSES:
            raise AppError(_PAGE_ERROR, f"status 仅支持 {'/'.join(USER_STATUSES)}", 422)

        flt: dict[str, Any] = {}
        if role:
            flt["role"] = role
        if status:
            flt["status"] = status
        else:
            flt["status"] = {"$ne": USER_STATUS_DELETED}

        total = await self.repo.count_users(flt)
        rows = await self.repo.query_users(flt, (page - 1) * page_size, page_size)
        return {
            "items": [UserOut.from_doc(d, current_user=current_user) for d in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0,
            "role_counts": await self._role_counts(),
        }

    async def _role_counts(self) -> dict[str, int]:
        """各角色的账号数（不含软删除）。用一次聚合而不是三次 count。"""
        pipeline = [
            {"$match": {"status": {"$ne": USER_STATUS_DELETED}}},
            {"$group": {"_id": "$role", "cnt": {"$sum": 1}}},
        ]
        try:
            # pymongo 的异步 `aggregate()` 返回的是**协程**（要先 await 才拿到游标），
            # 与同步 API 的形状不同——直接 `.to_list()` 会得到
            # `'coroutine' object has no attribute 'to_list'`
            cursor = await self.repo.col.aggregate(pipeline)
            rows = await cursor.to_list(length=10)
        except Exception as e:  # noqa: BLE001 - 计数只是页面上的辅助信息
            log.warning("统计角色分布失败（不影响列表）：%s: %s", type(e).__name__, e)
            return {}
        return {str(r.get("_id") or "unknown"): int(r.get("cnt") or 0) for r in rows}

    async def get_user(self, username: str) -> dict:
        """取单个账号（不存在/已删除 → `COM-4004`）。"""
        return await self._load(username)

    # ---------------- 新增 ----------------
    async def create_user(
        self,
        payload: Any,
        *,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`POST /system/users`（§3.4）。返回中的 `generated_password` **只出现这一次**。"""
        username = str(payload.username).strip()
        role = self._validate_role(payload.role)

        existing = await self.repo.find_raw(username)
        if existing is not None:
            # 软删除的账号**仍然占用账号名**（文档还在，`_id` 唯一）：把它当成
            # "可用"会让新建出的账号与历史账号同名，审计里两个同名人指代不清。
            raise UsernameConflictError(username)

        generated: Optional[str] = None
        password = payload.initial_password
        if password is None:
            generated = generate_password()
            password = generated
        # 口令规则**只由 01 的实现判定**（BR-13-19「与 01 一致」）：
        # 在这里再抄一份 6~64 的规则，迟早与 01 漂移成两套
        AuthService.validate_new_password(password)

        ts = now_ms()
        doc = {
            "_id": username,
            "username": username,
            "password_hash": hash_password(password),
            "real_name": str(payload.real_name).strip(),
            "role": role,
            "status": "active",
            "last_login_at": None,
            # 新建账号**不写** password_changed_at：它是"改密导致旧令牌失效"的锚，
            # 而新账号在创建之前不存在任何令牌
            "password_changed_at": None,
            "created_at": ts, "updated_at": ts,
            "created_by": operator, "updated_by": operator,
        }
        await self.repo.insert_user(doc)

        try:
            await audit(
                actor=operator, actor_role=actor_role, action="account.create",
                target_type="user", target_id=username,
                before=None, after=public_fields(doc),
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            # 补偿：物理删除刚插入的文档。这次写入在业务上等于**从未发生**，
            # 留一个"已创建但无痕"的账号等于给系统留了一个没人知道的口子
            removed = await self.repo.hard_delete(username)
            log.error("account.create 审计写入失败，已撤销新增 user=%s：%s", username, e)
            hint = "" if removed else "；且补偿删除失败，请人工核对账号表"
            raise AccountWriteFailedError(f"{type(e).__name__}{hint}") from e

        return {
            "username": username,
            "real_name": doc["real_name"],
            "role": role,
            "status": "active",
            "generated_password": generated,
            "user": UserOut.from_doc(doc, current_user=operator),
        }

    # ---------------- 改姓名 / 改角色 ----------------
    async def update_user(
        self,
        username: str,
        payload: Any,
        *,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`PUT /system/users/{username}`（§3.4：只改姓名与角色）。"""
        current = await self._load(username)
        patch: dict[str, Any] = {}

        real_name = getattr(payload, "real_name", None)
        if real_name is not None and str(real_name).strip() != str(current.get("real_name") or ""):
            patch["real_name"] = str(real_name).strip()

        new_role = getattr(payload, "role", None)
        role_changed = False
        if new_role is not None:
            new_role = self._validate_role(new_role)
            if new_role != str(current.get("role") or ""):
                # BR-13-14 的"降级"分支：把最后一个**可用**管理员改成别的角色，
                # 系统就再也没有人能进系统设置了
                if (str(current.get("role")) == Role.ADMIN.value
                        and str(current.get("status")) == "active"):
                    await self._assert_admin_remains(username, action="降级")
                patch["role"] = new_role
                role_changed = True

        if not patch:
            return {"changed": False, "user": UserOut.from_doc(current, current_user=operator)}

        ts = now_ms()
        patch.update({"updated_at": ts, "updated_by": operator})
        matched = await self.repo.update_fields(
            username, patch,
            # 乐观锁（D42）：条件带上"我读到的角色与状态"，被别人改过则匹配 0 条
            expected={"role": str(current.get("role") or ""),
                      "status": str(current.get("status") or "")},
        )
        if matched == 0:
            raise AccountVersionConflictError(username, "角色或状态已变")

        after = {**current, **patch}
        # 写入之后**再复核一次**管理员数量：并发下两个管理员互相降级时，
        # 前置检查会双双通过，只有这一步能兜住
        if role_changed and str(current.get("role")) == Role.ADMIN.value:
            try:
                await self._assert_admin_remains(username, action="降级")
            except AppError:
                reverted = await self.repo.restore_fields(
                    username, {"role": current.get("role"), "updated_at": current.get("updated_at"),
                               "updated_by": current.get("updated_by")}
                )
                log.error("降级后复核发现已无可用管理员，已回滚 user=%s（reverted=%s）",
                          username, reverted)
                raise

        try:
            await audit(
                actor=operator, actor_role=actor_role,
                action="account.role_change" if role_changed else "account.update",
                target_type="user", target_id=username,
                before=public_fields(current), after=public_fields(after),
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            restored = await self.repo.restore_fields(username, current)
            log.error("account.update 审计写入失败，已回滚 user=%s：%s", username, e)
            hint = "" if restored else "；且回滚亦失败，请人工核对账号"
            raise AccountWriteFailedError(f"{type(e).__name__}{hint}") from e

        return {"changed": True, "user": UserOut.from_doc(after, current_user=operator)}

    # ---------------- 停用 / 启用 ----------------
    async def disable_user(
        self,
        username: str,
        *,
        transfer_to: Optional[str] = None,
        reason: Optional[str] = None,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`POST /system/users/{username}/disable`（§3.4 + BR-13-15）。"""
        current = await self._load(username)
        self._assert_not_self(username, operator, action="停用")

        if str(current.get("status")) == "disabled":
            # 幂等：已经是停用态就不再写库/写审计（见模块 docstring）
            return {"changed": False, "username": username, "status": "disabled",
                    "transferred_cases": 0,
                    "user": UserOut.from_doc(current, current_user=operator)}

        # ① 身份/数量护栏（前置检查）
        if (str(current.get("role")) == Role.ADMIN.value
                and str(current.get("status")) == "active"):
            await self._assert_admin_remains(username, action="停用")

        # ② 未处置案件必须先转交（BR-13-15）
        transferred, transfer_ts = await self._handle_pending_cases(
            current, transfer_to=transfer_to, operator=operator
        )

        ts = now_ms()
        patch = {
            "status": "disabled", "updated_at": ts, "updated_by": operator,
            "disabled_reason": reason or None,
        }
        written = False
        try:
            matched = await self.repo.update_fields(
                username, patch,
                expected={"status": str(current.get("status") or "")},
            )
            if matched == 0:
                raise AccountVersionConflictError(username, "状态已变")
            written = True

            # ③ 写入之后复核"至少一个可用管理员"。**并发下这一步是唯一防线**：
            #    两个管理员同时停用对方时，前置检查会双双通过（各自都看到
            #    对方仍是可用管理员），只有复核能发现"现在一个都不剩了"。
            #    复核失败必须把刚写入的停用**补偿回滚**，否则护栏只是"报了错，
            #    但该发生的还是发生了"。
            if str(current.get("role")) == Role.ADMIN.value:
                await self._assert_admin_remains(username, action="停用")
        except AppError:
            if written:
                restored = await self.repo.restore_fields(username, current)
                if not restored:
                    log.error("停用后复核失败且回滚未成功，请人工核对 user=%s", username)
            if transferred:
                await self._revert_transfer(current, transfer_to, transfer_ts)
            raise

        after = {**current, **patch}
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="account.disable",
                target_type="user", target_id=username,
                before=public_fields(current), after={**public_fields(after),
                                                     "transferred_cases": transferred,
                                                     "transfer_to": transfer_to or None,
                                                     "reason": reason or None},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            # 审计落不下去 → 账号状态与案件转交**一起**回滚：
            # "停用了一半"（账号停了、案件没转）会让待审案件挂在一个进不来的人名下
            restored = await self.repo.restore_fields(username, current)
            if transferred:
                await self._revert_transfer(current, transfer_to, transfer_ts)
            log.error("account.disable 审计写入失败，已回滚 user=%s：%s", username, e)
            hint = "" if restored else "；且回滚亦失败，请人工核对账号与案件"
            raise AccountWriteFailedError(f"{type(e).__name__}{hint}") from e

        return {
            "changed": True, "username": username, "status": "disabled",
            "transferred_cases": transferred,
            "user": UserOut.from_doc(after, current_user=operator),
        }

    async def enable_user(
        self,
        username: str,
        *,
        reason: Optional[str] = None,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`POST /system/users/{username}/enable`（§3.4）。

        **启用自己是被允许的**：能调用本接口的人本来就是启用状态的管理员，
        因此这个分支实际不可达；不做特判是为了避免"把自己排除在护栏之外"
        这种看似安全、实则增加分支的写法。
        """
        current = await self._load(username)
        if str(current.get("status")) == "active":
            return {"changed": False, "username": username, "status": "active",
                    "user": UserOut.from_doc(current, current_user=operator)}

        ts = now_ms()
        patch = {"status": "active", "updated_at": ts, "updated_by": operator}
        matched = await self.repo.update_fields(
            username, patch, expected={"status": str(current.get("status") or "")}
        )
        if matched == 0:
            raise AccountVersionConflictError(username, "状态已变")

        after = {**current, **patch}
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="account.enable",
                target_type="user", target_id=username,
                before=public_fields(current), after={**public_fields(after),
                                                     "reason": reason or None},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            restored = await self.repo.restore_fields(username, current)
            log.error("account.enable 审计写入失败，已回滚 user=%s：%s", username, e)
            hint = "" if restored else "；且回滚亦失败，请人工核对账号"
            raise AccountWriteFailedError(f"{type(e).__name__}{hint}") from e

        return {"changed": True, "username": username, "status": "active",
                "user": UserOut.from_doc(after, current_user=operator)}

    # ---------------- 重置密码 ----------------
    async def reset_password(
        self,
        username: str,
        *,
        new_password: Optional[str] = None,
        reason: Optional[str] = None,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`POST /system/users/{username}/reset-password`（§3.4 + BR-13-16）。

        **明文口令绝不进审计、绝不进日志**：`after` 里只有"换了、什么时候换的、
        是系统生成的还是管理员指定的"。审计页是对外界面，写进去等于公开凭据。
        """
        current = await self._load(username)

        generated: Optional[str] = None
        password = new_password
        if password is None:
            generated = generate_password()
            password = generated
        AuthService.validate_new_password(password)

        ts = now_ms()
        patch = {
            "password_hash": hash_password(password),
            # BR-13-16 的落点：01 的 load_user_for_token 用它与 token 的 iat 比较，
            # 因此该账号**所有**旧令牌立即失效，无需黑名单
            "password_changed_at": ts,
            "updated_at": ts, "updated_by": operator,
        }
        matched = await self.repo.update_fields(
            username, patch, expected={"status": str(current.get("status") or "")}
        )
        if matched == 0:
            raise AccountVersionConflictError(username, "角色或状态已变")

        try:
            await audit(
                actor=operator, actor_role=actor_role, action="account.reset_password",
                target_type="user", target_id=username,
                before={"password_changed_at": current.get("password_changed_at")},
                after={"password_changed_at": ts, "generated": generated is not None,
                       "self_operation": username == operator, "reason": reason or None},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            restored = await self.repo.restore_fields(
                username, {"password_hash": current.get("password_hash"),
                           "password_changed_at": current.get("password_changed_at"),
                           "updated_at": current.get("updated_at"),
                           "updated_by": current.get("updated_by")}
            )
            log.error("account.reset_password 审计写入失败，已回滚 user=%s：%s", username, e)
            hint = "" if restored else "；且回滚亦失败，请人工核对账号"
            raise AccountWriteFailedError(f"{type(e).__name__}{hint}") from e

        return {
            "username": username,
            "generated_password": generated,
            "password_changed_at": ts,
            "tokens_invalidated": True,
            "note": ("该账号是你自己：重置后当前会话立即失效，请用新口令重新登录"
                     if username == operator else
                     "该账号的所有登录令牌已立即失效，需用新口令重新登录"),
        }

    # ---------------- 删除（软删除） ----------------
    async def delete_user(
        self,
        username: str,
        *,
        transfer_to: Optional[str] = None,
        reason: Optional[str] = None,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`DELETE /system/users/{username}`（§3.4 + BR-13-17：软删除）。"""
        current = await self._load(username)
        self._assert_not_self(username, operator, action="删除")

        if str(current.get("status")) != "disabled":
            # 先停用再删除：停用是**可逆**的（一键启用回来），删除不是。
            # 多这一步能挡住绝大多数误点（BR-13-17）
            raise DisableBeforeDeleteError(username, current.get("status"))

        if str(current.get("role")) == Role.ADMIN.value:
            await self._assert_admin_remains(username, action="删除")

        transferred, transfer_ts = await self._handle_pending_cases(
            current, transfer_to=transfer_to, operator=operator
        )

        ts = now_ms()
        patch = {
            "status": USER_STATUS_DELETED,
            "deleted": True, "deleted_at": ts, "deleted_by": operator,
            "updated_at": ts, "updated_by": operator,
            "deleted_reason": reason or None,
        }
        try:
            matched = await self.repo.update_fields(
                username, patch,
                expected={"status": str(current.get("status") or "")},
            )
            if matched == 0:
                raise AccountVersionConflictError(username, "状态已变")
        except AppError:
            if transferred:
                await self._revert_transfer(current, transfer_to, transfer_ts)
            raise

        after = {**current, **patch}
        try:
            await audit(
                actor=operator, actor_role=actor_role, action="account.delete",
                target_type="user", target_id=username,
                before=public_fields(current), after={**public_fields(after),
                                                     "transferred_cases": transferred,
                                                     "reason": reason or None},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            restored = await self.repo.restore_fields(username, current)
            if transferred:
                await self._revert_transfer(current, transfer_to, transfer_ts)
            log.error("account.delete 审计写入失败，已回滚 user=%s：%s", username, e)
            hint = "" if restored else "；且回滚亦失败，请人工核对账号"
            raise AccountWriteFailedError(f"{type(e).__name__}{hint}") from e

        return {
            "changed": True, "username": username, "status": USER_STATUS_DELETED,
            "soft_deleted": True, "transferred_cases": transferred,
            "user": UserOut.from_doc(after, current_user=operator),
        }

    # ---------------- 内部：护栏 ----------------
    async def _load(self, username: str) -> dict:
        """取账号；不存在或已软删除 → `COM-4004`。

        **软删除的账号按"不存在"处理**（而不是"已停用"）：对它做任何写操作都
        没有意义，而把它当成"已停用"会让"删除后还能停用/重置密码"这种荒唐路径
        留在接口上（重置一个已删除账号的口令，等于给幽灵账号留了钥匙）。
        """
        doc = await self.repo.find_raw(username)
        if doc is None or str(doc.get("status")) == USER_STATUS_DELETED:
            raise NotFoundError(_NOT_FOUND_MESSAGE)
        return doc

    @staticmethod
    def _assert_not_self(username: str, operator: str, *, action: str) -> None:
        """BR-13-13：不能停用/删除自己（`SYS-4005`）。

        **先于** `SYS-4006`：种子环境下 `admin01` 是唯一管理员，"停用自己"
        同时满足两条规则，而 V-13-13 要求的是身份类的 `SYS-4005`。
        """
        if username == operator:
            raise SelfOperationForbiddenError(action, username)

    async def _assert_admin_remains(self, username: str, *, action: str) -> None:
        """BR-13-14：操作之后必须仍存在至少一个**可用**（`status=active`）管理员。

        不变量之所以能成立，靠的正是"操作者本身是可用管理员"这一事实；一旦
        把它放到并发场景里就不成立了（两个管理员同时停用/降级对方），因此本
        函数既被用作**前置检查**，也在写入之后被**再调用一次**做复核。
        """
        remaining = await self.repo.count_active_admins(exclude=username)
        if remaining <= 0:
            raise LastAdminProtectedError(action, username, remaining)

    def _validate_role(self, role: Any) -> str:
        """BR-13-12：角色只能是 `reviewer/strategist/admin`（不允许自定义角色）。"""
        value = str(role or "").strip()
        if value not in ROLES:
            raise AppError(
                _PAGE_ERROR,
                f"角色仅支持 {'/'.join(ROLES)}，收到：{value!r}",
                422,
            )
        return value

    # ---------------- 内部：案件转交（BR-13-15） ----------------
    async def _handle_pending_cases(
        self, current: dict, *, transfer_to: Optional[str], operator: str
    ) -> tuple[int, int]:
        """停用/删除之前处理"名下未处置案件"，返回 `(转交条数, 转交时间戳)`。

        没有待办 → `(0, 0)`，不做任何写入。
        有待办且未给接收人 → `SYS-4007`（并把案件清单回给页面）。
        给了接收人 → 校验它是**启用的 reviewer 账号**后转交。
        """
        if self.cases is None:
            return 0, 0
        username = str(current.get("_id") or "")
        pending = await self.cases.count_pending(username)
        if pending <= 0:
            return 0, 0

        if not transfer_to:
            raise PendingCasesError(username, await self.cases.list_pending(username))

        target = await self.repo.find_raw(str(transfer_to))
        if (target is None
                or str(target.get("status")) != "active"
                or str(target.get("role")) != Role.REVIEWER.value):
            # Spec §8 的"新增"行：接收人必须是启用的 reviewer。
            # 该场景 Spec §5 未分配 `SYS-` 码，按通用参数错误拒绝（`COM-4001`），
            # 并在文案里写清"为什么不能是他"——只说"参数不合法"用户无从改起
            raise AppError(
                _PAGE_ERROR,
                f"接收人 {transfer_to} 必须是状态为启用的 reviewer 账号",
                422,
                {"transfer_to": transfer_to},
            )
        if str(transfer_to) == username:
            raise AppError(_PAGE_ERROR, "接收人不能是待停用的账号自己", 422)

        ts = now_ms()
        moved = await self.cases.transfer_pending(username, str(transfer_to), ts)
        log.info("账号 %s 的 %d 个待处置案件已转交 %s（操作人 %s）",
                 username, moved, transfer_to, operator)
        return moved, ts

    async def _revert_transfer(self, current: dict, transfer_to: Optional[str], ts: int) -> None:
        """补偿：把本次转交的案件还回去（只回滚本次那一批，见 repo 的条件）。"""
        if self.cases is None or not transfer_to or not ts:
            return
        username = str(current.get("_id") or "")
        ok = await self.cases.revert_transfer(username, str(transfer_to), ts)
        if not ok:
            log.error("案件转交回滚失败（user=%s -> %s, ts=%s），请人工核对案件归属",
                      username, transfer_to, ts)


def build_user_admin_service(db: Any) -> UserAdminService:
    """装配（供依赖注入与测试复用）。"""
    return UserAdminService(UserRepo(db), CaseTransferRepo(db))


def role_label(role: str) -> str:
    """角色中文标签（数据驱动，供日志与响应复用）。"""
    return label_of(Role, role, role)


__all__ = [
    "SECRET_FIELDS", "UserAdminService", "actor_of", "build_user_admin_service",
    "generate_password", "public_fields", "role_label",
]
