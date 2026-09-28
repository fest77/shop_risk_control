# -*- coding: utf-8 -*-
"""JWT 签发与解析（模块 01 §3.5 / §4.2）。

**Claims**（与 §3.5 逐字段一致）

| Claim | 值 |
|---|---|
| `sub` | `username` |
| `role` | `reviewer` / `strategist` / `admin` |
| `iat` / `exp` | 签发与过期时间 |
| `jti` | 唯一 id（为将来黑名单预留） |

**为什么把角色写进 token 却仍要查库**：token 里的 `role` 只用于**快速判断**，
真正的权限判定与"账号是否已停用/密码是否已改"必须回源查库（BR-01-03 /
BR-01-10）——否则管理员停用账号或用户改密后，旧 token 依然畅通，这是最常见的
鉴权漏洞之一。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt

from app import config

# 令牌无效的三类原因，分别对应不同错误码，便于前端与日志区分处置：
#   格式/缺失 -> AUTH-4002（请先登录）
#   签名无效  -> AUTH-4003（凭证无效，可能是篡改，要记安全日志）
#   已过期    -> AUTH-4004（登录已失效）
_TOKEN_ERRORS = (jwt.ExpiredSignatureError, jwt.InvalidSignatureError,
                 jwt.InvalidAlgorithmError, jwt.DecodeError, jwt.InvalidTokenError)


class TokenError(Exception):
    """令牌不可用。子类携带 `code`，由鉴权中间件映射为统一响应。"""

    code = "AUTH-4002"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TokenMalformedError(TokenError):
    """AUTH-4002：缺失、格式非法、段数不对。"""

    code = "AUTH-4002"


class TokenInvalidError(TokenError):
    """AUTH-4003：签名无效 / 算法被换（疑似篡改）。"""

    code = "AUTH-4003"


class TokenExpiredError(TokenError):
    """AUTH-4004：已过期。"""

    code = "AUTH-4004"


@dataclass(frozen=True)
class TokenPayload:
    """解析后的令牌内容。"""

    sub: str
    role: str
    iat: int
    exp: int
    jti: str

    def issued_at_ms(self) -> int:
        """签发时刻（毫秒），用于与 `password_changed_at` 比较（BR-01-10）。"""
        return int(self.iat) * 1000


def create_access_token(
    username: str, role: str, *, now: int | None = None, expire_minutes: int | None = None
) -> tuple[str, int]:
    """签发令牌，返回 `(token, 有效期秒数)`。

    `now` 可注入，是为了让"过期令牌"的测试不必真的等待 8 小时。
    """
    issued = int(time.time()) if now is None else int(now)
    minutes = config.JWT_EXPIRE_MINUTES if expire_minutes is None else expire_minutes
    expires_in = minutes * 60
    payload = {
        "sub": username,
        "role": role,
        "iat": issued,
        "exp": issued + expires_in,
        # jti 为将来的服务端黑名单（登出真正失效）预留，当前不校验
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> TokenPayload:
    """解析并校验令牌。

    **必须显式指定 `algorithms`**：不传的话 PyJWT 会接受 token 头部声明的算法，
    攻击者可伪造 `alg=none` 或换成非对称算法来绕过签名（BR-01-09 明确禁止）。
    """
    if not token or not isinstance(token, str):
        raise TokenMalformedError("未提供令牌")
    if token.count(".") != 2:
        raise TokenMalformedError("令牌格式非法")
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            # 强制要求关键 claim 存在：缺少 exp 的令牌等于永不过期
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError("令牌已过期") from e
    except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError) as e:
        raise TokenInvalidError("令牌签名无效") from e
    except _TOKEN_ERRORS as e:
        raise TokenMalformedError("令牌格式非法") from e

    role = payload.get("role")
    if role not in config.VALID_ROLES:
        # 令牌里的角色不在允许集合内（例如角色被移除后旧 token 仍在流通）
        raise TokenMalformedError("令牌中的角色无效")
    return TokenPayload(
        sub=str(payload["sub"]),
        role=str(role),
        iat=int(payload["iat"]),
        exp=int(payload["exp"]),
        jti=str(payload.get("jti", "")),
    )


def bearer_token(authorization: str | None) -> str:
    """从 `Authorization` 头取出令牌，格式非法时抛 AUTH-4002。"""
    raw = (authorization or "").strip()
    if not raw:
        raise TokenMalformedError("未提供令牌")
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise TokenMalformedError("Authorization 头格式非法，应为 `Bearer <token>`")
    return parts[1].strip()
