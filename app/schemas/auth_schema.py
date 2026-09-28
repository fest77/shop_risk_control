# -*- coding: utf-8 -*-
"""登录与改密的请求/响应模型（模块 01 §3.1 / §3.4）。

**约束放在哪一层是有意的**：
- `LoginRequest` 直接约束 `username` 形态与长度——登录接口的参数错误属通用校验
  失败（§3.1 的 422 参数非法），用 `COM-4001` 表达即可。
- `ChangePasswordRequest` 的 `new_password` **刻意只校验"非空"**：太短应由
  服务层抛 `AUTH-4006`「新密码不符合要求」（§5 明确指定），若在模型层写
  `min_length=6`，返回的会是通用的 `COM-4001`，丢掉契约要求的错误码。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# 账号形态（§3.1）：4~32 字符，仅字母数字与 _ . -
USERNAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"


class LoginRequest(BaseModel):
    username: str = Field(
        ..., min_length=4, max_length=32, pattern=USERNAME_PATTERN,
        description="账号（4~32 字符，字母/数字/_ . -）",
    )
    password: str = Field(..., min_length=6, max_length=64, description="密码")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=64)
    new_password: str = Field(
        ..., min_length=1, max_length=64,
        description="新密码；长度不足由服务层按 AUTH-4006 拒绝",
    )


__all__ = ["LoginRequest", "ChangePasswordRequest", "USERNAME_PATTERN"]
