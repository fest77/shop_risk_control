# -*- coding: utf-8 -*-
"""口令哈希（bcrypt）。

**为什么不用 passlib**：passlib 1.7.4 自 2020 年起未再发版，与 bcrypt >= 4.1
不兼容——它在后端自检时会用超过 72 字节的口令调用 `bcrypt.hashpw`，bcrypt 5.0
改为直接抛 `ValueError`，导致 `passlib.hash.bcrypt.hash()` **完全不可用**
（本项目实测复现，见 15_决策记录 D22）。模块 01 Spec 原文写的 `passlib[bcrypt]`
已据此更正为直接依赖 `bcrypt`。

**归属**：本文件自模块 00 的 `app/utils/password.py` 迁入——口令哈希属于鉴权域，
与 JWT、权限矩阵放在同一个包内更利于评审安全边界。

**bcrypt 的 72 字节硬约束必须显式处理**：它只取口令的前 72 字节。若默默截断，
两个不同的长口令会得到同一个哈希——即"任何一个都能登录该账号"，这是安全性
降级而不是兼容性问题。因此哈希时超长**直接拒绝**，校验时超长一律不通过。
"""
from __future__ import annotations

import hmac

import bcrypt

# bcrypt 算法的输入上限（字节，不是字符）。中文口令按 UTF-8 编码后 1 字 3 字节，
# 因此 24 个汉字就会触顶——这也是必须在录入层做长度校验的原因。
MAX_PASSWORD_BYTES = 72
# 代价因子。12 在演示机上单次约 0.2~0.3s，兼顾抗爆破与登录体验。
BCRYPT_ROUNDS = 12


class PasswordTooLongError(ValueError):
    """口令超过 bcrypt 的 72 字节上限。"""

    def __init__(self, length: int):
        super().__init__(
            f"口令过长：UTF-8 编码后 {length} 字节，bcrypt 上限为 {MAX_PASSWORD_BYTES} 字节"
        )
        self.length = length


def hash_password(plain: str) -> str:
    """生成 bcrypt 哈希（`$2b$...`），返回字符串以便直接入库。"""
    raw = (plain or "").encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(len(raw))
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验口令（BR-01-06：恒定时间比较）。

    `bcrypt.checkpw` 内部即恒定时间比较；任何异常（哈希串损坏、编码问题）都
    返回 False——校验失败必须是"不通过"，绝不允许因为异常而放行。

    另用 `hmac.compare_digest` 对长度做一次恒定时间短路：超长口令不存在与之
    匹配的哈希（因为 `hash_password` 会拒绝超长输入），直接判不通过，同时避免
    把"是否超长"变成可通过时间差探测的信息。
    """
    if not plain or not hashed:
        return False
    raw = plain.encode("utf-8")
    too_long = hmac.compare_digest(str(len(raw) > MAX_PASSWORD_BYTES), "True")
    if too_long:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def dummy_verify() -> None:
    """对不存在的账号做一次等价的哈希校验，用于**抹平登录响应时间**。

    BR-01-02 要求不区分「账号不存在」与「密码错误」。若账号不存在时直接返回，
    "不存在的账号"会比"密码错误"**快一个 bcrypt 的量级**（约 200ms），攻击者
    据此就能枚举出哪些账号真实存在——返回文案统一了并不够，时间也必须统一。
    """
    bcrypt.checkpw(b"dummy-password-for-timing", bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=4)))
