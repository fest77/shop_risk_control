# -*- coding: utf-8 -*-
"""认证编排（模块 01 §4.1 / §4.2）。

本文件把"登录、查当前用户、改密"三件事编排起来，**不依赖 FastAPI**，
因此可以用假仓储直接单测。
"""
from __future__ import annotations

from typing import Any

from app import config
from app.enums import Role, label_of
from app.errors import (
    AccountDisabledError,
    AuthDependencyError,
    BadCredentialsError,
    LoginLockedError,
    PasswordChangedError,
    WeakPasswordError,
    WrongOldPasswordError,
    auth_error,
)
from app.logging import get_logger
from app.repos.user_repo import UserRepo
from app.security.jwt import TokenPayload, create_access_token
from app.security.login_guard import LoginGuard
from app.security.password import (
    MAX_PASSWORD_BYTES,
    dummy_verify,
    hash_password,
    verify_password,
)
from app.security.permissions import permissions_for
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.auth")

# 口令长度约束（§3.1 / §8：仅长度，不强制字符类型混合）
PASSWORD_MIN_LEN = 6
PASSWORD_MAX_LEN = 64


class AuthService:
    def __init__(self, repo: UserRepo, guard: LoginGuard):
        self.repo = repo
        self.guard = guard

    # ---------------- 登录 ----------------
    async def login(self, username: str, password: str) -> dict:
        """账号密码登录，返回 `{access_token, token_type, expires_in, user}`。"""
        # ① 先看锁定：锁定期内连口令都不校验，避免为攻击者消耗 bcrypt 算力
        remain = self.guard.is_locked(username)
        if remain:
            raise LoginLockedError(remain)

        user = await self.repo.find_by_username(username)
        if user is None:
            # BR-01-02 防账号枚举：**文案统一还不够，时间也必须统一**。
            # 账号不存在时若不跑一次等价的 bcrypt 校验，响应会快约 200ms，
            # 攻击者据此即可判断账号是否存在。
            dummy_verify()
            locked = self.guard.record_failure(username)
            if locked:
                raise LoginLockedError(locked)
            raise BadCredentialsError()

        if not verify_password(password, user.get("password_hash", "")):
            locked = self.guard.record_failure(username)
            if locked:
                raise LoginLockedError(locked)
            raise BadCredentialsError()

        # ② 口令正确后再看账号状态（BR-01-03：停用账号即使密码正确也拒绝）
        if user.get("status") != "active":
            raise AccountDisabledError()

        role = str(user.get("role", ""))
        if role not in config.VALID_ROLES:
            # 数据问题而非用户问题：明确报依赖异常，避免用 401 掩盖脏数据
            raise AuthDependencyError(f"账号 {username} 的角色非法：{role!r}")

        token, expires_in = create_access_token(username, role)
        self.guard.clear(username)
        try:
            await self.repo.touch_last_login(username, now_ms())
        except Exception as e:  # noqa: BLE001 - 统计字段写失败不该挡住登录
            log.warning("更新 last_login_at 失败（%s）：%s: %s", username, type(e).__name__, e)

        log.info("登录成功 user=%s role=%s", username, role)
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "user": self.public_user(user),
        }

    # ---------------- 当前用户 ----------------
    @staticmethod
    def public_user(user: dict) -> dict:
        """对外的用户表示（**绝不包含 `password_hash`**）。"""
        role = str(user.get("role", ""))
        return {
            "username": str(user.get("_id") or user.get("username")),
            "real_name": user.get("real_name") or "",
            "role": role,
            "role_label": label_of(Role, role, role),
        }

    async def load_user_for_token(self, payload: TokenPayload) -> dict:
        """令牌通过后回源查库，完成"停用"与"改密失效"两项判定。

        BR-01-03 / BR-01-10 都要求以**库中当前状态**为准，不能只信 token 里的
        声明——否则管理员停用账号、或用户改密之后，旧 token 依然畅通无阻。
        """
        user = await self.repo.find_by_username(payload.sub)
        if user is None:
            # 签名有效但账号已不存在（被删除/改名）
            raise auth_error("AUTH-4002", "账号不存在或已被删除，请重新登录")
        if user.get("status") != "active":
            raise AccountDisabledError()
        changed_at = user.get("password_changed_at")
        if changed_at and int(changed_at) > payload.issued_at_ms():
            raise PasswordChangedError()
        return user

    @staticmethod
    def me_payload(user: dict, payload: TokenPayload) -> dict:
        """`GET /auth/me` 的 `data`（§3.2）。"""
        role = str(user.get("role", ""))
        base = AuthService.public_user(user)
        base.update({
            # 权限由后端按矩阵展开下发，前端**不得自行推导**（BR-01-12）
            "permissions": permissions_for(role),
            # 本会话的登录时刻（取自当前 token 的 iat），与库里的"上次登录"区分开
            "login_at": payload.issued_at_ms(),
            "token_expires_at": int(payload.exp) * 1000,
            "last_login_at": user.get("last_login_at"),
        })
        return base

    # ---------------- 改密 ----------------
    async def change_password(
        self, username: str, old_password: str, new_password: str
    ) -> dict:
        """本人改密（§3.4）。成功后**旧令牌立即失效**（BR-01-10）。"""
        self.validate_new_password(new_password)
        user = await self.repo.find_by_username(username)
        if user is None:
            raise auth_error("AUTH-4002", "账号不存在或已被删除，请重新登录")
        if not verify_password(old_password, user.get("password_hash", "")):
            raise WrongOldPasswordError()
        if old_password == new_password:
            # 改成一个完全相同的口令，等于"没改但令牌全失效"，用户会困惑
            raise WeakPasswordError("新密码不能与原密码相同")

        await self.repo.update_password(username, hash_password(new_password), now_ms())
        log.info("改密成功 user=%s（该用户旧令牌自此失效）", username)
        return {"changed": True}

    @staticmethod
    def validate_new_password(new_password: str) -> None:
        """校验新口令长度（§3.1 / AUTH-4006）。

        同时挡住 bcrypt 的 72 字节上限：超长口令若被静默截断，两个不同的长口令
        会得到同一个哈希——那等于任何人都能登录该账号。
        """
        raw = (new_password or "").encode("utf-8")
        if len(raw) > MAX_PASSWORD_BYTES:
            raise WeakPasswordError(f"新密码过长（UTF-8 编码后最多 {MAX_PASSWORD_BYTES} 字节）")
        text = new_password or ""
        if not (PASSWORD_MIN_LEN <= len(text) <= PASSWORD_MAX_LEN):
            raise WeakPasswordError(
                f"新密码需 {PASSWORD_MIN_LEN}~{PASSWORD_MAX_LEN} 位"
            )


def build_auth_service(db: Any, guard: LoginGuard) -> AuthService:
    """装配（供依赖注入与测试复用）。"""
    return AuthService(UserRepo(db), guard)
